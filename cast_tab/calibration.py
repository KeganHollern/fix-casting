"""Measure the pipeline's A/V offset by clapperboard-testing the real output.

A calibration page fires a flash (a full-screen noise field = a bright video
frame) and a 1kHz beep at the same instant on a fixed cadence. We run that page
through the *whole* real pipeline (capture -> frame queue -> ffmpeg -> HLS),
then decode the HLS output and find where the flash and beep actually land in
it. Their separation is the true A/V skew the TV would show -- it includes the
frame-queue and encoder latency that a capture-stage measurement misses.

The offset is a property of the pipeline config, not the page, so the value
measured here stays valid once the user's real page is loaded.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from cast_tab.audio import (
    AudioCaptureError,
    audiotee_available,
    stop_audio_capture,
    try_start_chrome_audio_capture,
)
from cast_tab.browser import CaptureMethod, TabScreencaster
from cast_tab.streamer import HLSStreamer

# Each pulse cycles through N_LEVELS distinct intensities: a flash brightness
# and a beep amplitude that encode the same sequence index. We match a flash to
# a beep by that shared identity, not by proximity in time, so the measurement
# survives missing early pulses (the tap is latent on startup) and large skews.
PULSE_PERIOD_S = 1.5
FLASH_GRAYS = [52, 104, 156, 208]   # video flash luma per cycle step
BEEP_AMPS = [0.15, 0.30, 0.45, 0.60]  # audio beep amplitude per cycle step
N_LEVELS = len(FLASH_GRAYS)
_CALIBRATION_HTML = """<!doctype html><html><head><meta charset=utf-8>
<style>html,body{margin:0;background:#000;overflow:hidden}canvas{display:block}</style>
</head><body><canvas id=c></canvas><script>
const cv=document.getElementById('c');const W=cv.width=innerWidth,H=cv.height=innerHeight;
const ctx=cv.getContext('2d');
const grays=%GRAYS%,amps=%AMPS%,N=grays.length;
let k=0,flashUntil=0,flashGray=0,mark=0,actx;
function ensureCtx(){
  if(!actx){actx=new (window.AudioContext||window.webkitAudioContext)();
    const bg=actx.createOscillator(),bgg=actx.createGain();
    bg.frequency.value=120;bgg.gain.value=0.01;bg.connect(bgg);bgg.connect(actx.destination);bg.start();
  }
  if(actx.state==='suspended')actx.resume();
}
function beep(amp){try{
  ensureCtx();
  const o=actx.createOscillator(),g=actx.createGain();
  o.type='square';o.frequency.value=1000;o.connect(g);g.connect(actx.destination);
  g.gain.setValueAtTime(amp,actx.currentTime);
  o.start();o.stop(actx.currentTime+0.10);
}catch(e){}}
// Continuous marker keeps the paint-driven capture emitting frames; the flash
// overlays a full-screen gray whose luma encodes the cycle step.
function frame(){
  if(performance.now()<flashUntil){const c='rgb('+flashGray+','+flashGray+','+flashGray+')';ctx.fillStyle=c;ctx.fillRect(0,0,W,H);}
  else{ctx.fillStyle='#000';ctx.fillRect(0,0,W,H);mark=(mark+7)%(W-8);ctx.fillStyle='#fff';ctx.fillRect(mark,0,6,6);}
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
function pulse(){flashGray=grays[k];flashUntil=performance.now()+130;beep(amps[k]);k=(k+1)%N;}
setTimeout(()=>{pulse();setInterval(pulse,%PERIOD_MS%);},1500);
</script></body></html>"""


def _calibration_url() -> str:
    html = (
        _CALIBRATION_HTML
        .replace("%GRAYS%", str(FLASH_GRAYS))
        .replace("%AMPS%", str(BEEP_AMPS))
        .replace("%PERIOD_MS%", str(int(PULSE_PERIOD_S * 1000)))
    )
    return "data:text/html," + urllib.parse.quote(html)


def _ffprobe_frame_values(lavfi_input: str, tag: str) -> list[tuple[float, float]]:
    """Return (pts_time, value) per frame for a lavfi-tagged metric."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", lavfi_input,
                "-show_entries", f"frame=pts_time:frame_tags={tag}",
                "-of", "csv=p=0",
            ],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    series: list[tuple[float, float]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            series.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return series


def probe_video_brightness(mkv: Path) -> list[tuple[float, float]]:
    return _ffprobe_frame_values(f"movie={mkv},signalstats", "lavfi.signalstats.YAVG")


def probe_audio_rms(mkv: Path) -> list[tuple[float, float]]:
    raw = _ffprobe_frame_values(
        f"amovie={mkv},astats=metadata=1:reset=1", "lavfi.astats.Overall.RMS_level"
    )
    # astats reports dBFS; convert to linear so ratio detection works (-inf -> 0).
    return [(t, 10 ** (db / 20) if db > -200 else 0.0) for t, db in raw]


def _detect_pulses(
    series: list[tuple[float, float]], *, ratio: float
) -> list[tuple[float, float]]:
    """Return (onset_time, peak_value) for each spike above the baseline."""
    if len(series) < 4:
        return []
    ordered = sorted(v for _, v in series)
    median = ordered[len(ordered) // 2] or 1e-9
    threshold = median * ratio
    refractory = PULSE_PERIOD_S * 0.6
    pulses: list[tuple[float, float]] = []
    in_event = False
    onset = 0.0
    peak = 0.0
    last_end = -1e9
    for t, value in series:
        if value >= threshold:
            if not in_event and (t - last_end) > refractory:
                in_event, onset, peak = True, t, value
            elif in_event:
                peak = max(peak, value)
        elif in_event:
            in_event, last_end = False, t
            pulses.append((onset, peak))
    if in_event:
        pulses.append((onset, peak))
    return pulses


def _classify(pulses: list[tuple[float, float]]) -> list[tuple[float, int]]:
    """Tag each pulse with its cycle step from its peak level (scale-free)."""
    if not pulses:
        return []
    top = max(p for _, p in pulses) or 1e-9
    tagged: list[tuple[float, int]] = []
    for t, peak in pulses:
        norm = peak / top
        step = min(range(N_LEVELS), key=lambda i: abs(norm - (i + 1) / N_LEVELS))
        tagged.append((t, step))
    return tagged


def _identity_offset(
    video: list[tuple[float, int]], audio: list[tuple[float, int]]
) -> tuple[float, int] | None:
    """Median (flash - beep) over pulses matched by their cycle-step identity."""
    offsets: list[float] = []
    for vt, vstep in video:
        same = [at for at, astep in audio if astep == vstep]
        if not same:
            continue
        at = min(same, key=lambda a: abs(a - vt))
        if abs(vt - at) < PULSE_PERIOD_S * N_LEVELS * 0.5:
            offsets.append(vt - at)
    if len(offsets) < 2:
        return None
    offsets.sort()
    return offsets[len(offsets) // 2], len(offsets)


def remux_to_mkv(playlist: Path) -> Path | None:
    """Mark the live HLS playlist complete, then remux it to a seekable mkv.

    The streamer writes the playlist with omit_endlist (no #EXT-X-ENDLIST), so
    ffmpeg would treat it as an ongoing live stream and block forever waiting
    for more segments. Append the end marker first so the remux reads the
    segments that exist and exits.
    """
    try:
        text = playlist.read_text()
    except OSError:
        return None
    if "#EXT-X-ENDLIST" not in text:
        try:
            playlist.write_text(text.rstrip() + "\n#EXT-X-ENDLIST\n")
        except OSError:
            return None
    mkv = playlist.parent / "calib.mkv"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(playlist), "-c", "copy", str(mkv)],
            check=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return mkv if mkv.exists() else None


def offset_from_output(
    playlist: Path, *, log: Callable[[str], None] = print
) -> float | None:
    """Remux the HLS output and measure the flash-vs-beep skew in it."""
    if not playlist.exists():
        return None
    mkv = remux_to_mkv(playlist)
    if mkv is None:
        return None

    video = _classify(_detect_pulses(probe_video_brightness(mkv), ratio=3.0))
    audio = _classify(_detect_pulses(probe_audio_rms(mkv), ratio=4.0))
    log(f"Calibration: output had {len(video)} flashes / {len(audio)} beeps.")
    result = _identity_offset(video, audio)
    if result is None:
        return None
    offset_s, matched = result
    log(f"Calibration: matched {matched} flash/beep pairs by identity.")
    return offset_s


def run_calibration_pipeline(
    work_dir: Path,
    *,
    capture_method: CaptureMethod,
    width: int,
    height: int,
    fps: int,
    jpeg_quality: int,
    codec: str,
    buffered: bool,
    headless: bool,
    duration_s: float,
) -> Path | None:
    """Run the flash+beep page through the full pipeline; return the playlist."""
    frames_screencaster = TabScreencaster(
        _calibration_url(),
        width=width, height=height, fps=fps, jpeg_quality=jpeg_quality,
        on_frame=lambda _f: None, headless=headless, capture_audio=True,
        capture_method=capture_method,
    )
    audio_capture = None
    streamer = None
    try:
        frames_screencaster.start()
        frames_screencaster.wait_until_ready()
        frames_screencaster.enable_capture()
        try:
            audio_capture = try_start_chrome_audio_capture(
                frames_screencaster.user_data_dir,
                on_retry=frames_screencaster.nudge_playback,
            )
        except AudioCaptureError:
            return None
        streamer = HLSStreamer(
            width=width, height=height, fps=fps, codec=codec, buffered=buffered,
            audio_fd=audio_capture.read_fd, audio_format=audio_capture.audio_format,
            work_dir=work_dir,
        )
        frames_screencaster.on_frame = streamer.publish_frame
        streamer.start()
        streamer.wait_until_ready()
        time.sleep(duration_s)
        return work_dir / "stream.m3u8"
    finally:
        if streamer is not None:
            streamer.stop()
        frames_screencaster.stop()
        stop_audio_capture(audio_capture)


def measure_av_offset(
    *,
    capture_method: CaptureMethod,
    width: int,
    height: int,
    fps: int,
    jpeg_quality: int,
    codec: str,
    buffered: bool,
    headless: bool,
    duration_s: float = 20.0,
    log: Callable[[str], None] = print,
) -> float | None:
    """Measure how far audio leads video (seconds) through the real pipeline.

    Returns the amount to delay audio by (positive == audio is ahead), or None
    if calibration could not run or detect a clean signal.
    """
    if not audiotee_available():
        return None
    work_dir = Path(tempfile.mkdtemp(prefix="cast-calib-"))
    playlist = run_calibration_pipeline(
        work_dir,
        capture_method=capture_method, width=width, height=height, fps=fps,
        jpeg_quality=jpeg_quality, codec=codec, buffered=buffered,
        headless=headless, duration_s=duration_s,
    )
    if playlist is None:
        return None
    return offset_from_output(playlist, log=log)
