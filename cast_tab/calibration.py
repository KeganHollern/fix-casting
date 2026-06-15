"""Measure the pipeline's A/V capture offset with a flash+beep calibration page.

We load a page that, on a fixed cadence, draws a high-detail "flash" (a large
JPEG when captured) and plays a 1kHz "beep" at the same instant. By detecting
when each shows up in the captured video (JPEG size spike) vs the captured
audio (RMS spike) and taking the gap, we measure exactly how far video lags
audio through the real capture path — no need to know what causes the lag.

The offset is a property of the capture pipeline, not the page content, so the
value measured here stays valid once the user's real page is loaded.
"""

from __future__ import annotations

import math
import os
import time
import urllib.parse
from array import array
from collections.abc import Callable

from cast_tab.audio import (
    AudioCaptureError,
    audiotee_available,
    stop_audio_capture,
    try_start_chrome_audio_capture,
)
from cast_tab.browser import CaptureMethod, TabScreencaster

# Flash + beep fired together every PULSE_PERIOD_S. The flash is a pre-rendered
# noise field (a big, high-entropy JPEG) shown briefly over an otherwise black
# page, so the captured frame size jumps clearly when it appears.
PULSE_PERIOD_S = 3.0
_CALIBRATION_HTML = """<!doctype html><html><head><meta charset=utf-8>
<style>html,body{margin:0;background:#000;overflow:hidden}canvas{display:block}</style>
</head><body><canvas id=c></canvas><script>
const cv=document.getElementById('c');const W=cv.width=innerWidth,H=cv.height=innerHeight;
const ctx=cv.getContext('2d');
const pat=document.createElement('canvas');pat.width=W;pat.height=H;
const pc=pat.getContext('2d');const img=pc.createImageData(W,H);
for(let i=0;i<img.data.length;i+=4){img.data[i]=Math.random()*255;img.data[i+1]=Math.random()*255;img.data[i+2]=Math.random()*255;img.data[i+3]=255;}
pc.putImageData(img,0,0);
function black(){ctx.fillStyle='#000';ctx.fillRect(0,0,W,H);}
black();
let actx;
function ensureCtx(){
  if(!actx){actx=new (window.AudioContext||window.webkitAudioContext)();
    // A continuous quiet tone keeps the process tap producing PCM between
    // beeps, so the captured audio timeline has no gaps to misalign on.
    const bg=actx.createOscillator(),bgg=actx.createGain();
    bg.frequency.value=120;bgg.gain.value=0.01;bg.connect(bgg);bgg.connect(actx.destination);bg.start();
  }
  if(actx.state==='suspended')actx.resume();
}
function beep(){try{
  ensureCtx();
  const o=actx.createOscillator(),g=actx.createGain();
  o.type='square';o.frequency.value=1000;o.connect(g);g.connect(actx.destination);
  g.gain.setValueAtTime(0.6,actx.currentTime);
  o.start();o.stop(actx.currentTime+0.08);
}catch(e){}}
// Paint-driven capture (screencast) only emits frames when the page paints, so
// drive a continuous animation: a moving marker keeps frames flowing (small
// JPEGs) and the flash overlays a full-screen noise field (a big JPEG spike).
let flashUntil=0,mark=0;
function frame(){
  if(performance.now()<flashUntil){ctx.drawImage(pat,0,0);}
  else{black();mark=(mark+7)%(W-8);ctx.fillStyle='#fff';ctx.fillRect(mark,0,6,6);}
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
function pulse(){flashUntil=performance.now()+130;beep();}
setTimeout(()=>{pulse();setInterval(pulse,%PERIOD_MS%);},1500);
</script></body></html>"""


def _calibration_url() -> str:
    html = _CALIBRATION_HTML.replace("%PERIOD_MS%", str(int(PULSE_PERIOD_S * 1000)))
    return "data:text/html," + urllib.parse.quote(html)


def _detect_events(series: list[tuple[float, float]], *, ratio: float) -> list[float]:
    """Return the onset time of each spike (value > ratio * median baseline)."""
    if len(series) < 4:
        return []
    ordered = sorted(v for _, v in series)
    median = ordered[len(ordered) // 2] or 1e-9
    threshold = median * ratio
    events: list[float] = []
    last = -1e9
    for t, value in series:
        # One event per pulse: ignore further spikes within most of a period.
        if value >= threshold and (t - last) > PULSE_PERIOD_S * 0.5:
            events.append(t)
            last = t
    return events


def _median_offset(
    video_events: list[float], audio_events: list[float]
) -> tuple[float, int] | None:
    if not video_events or not audio_events:
        return None
    offsets: list[float] = []
    for ve in video_events:
        ae = min(audio_events, key=lambda a: abs(a - ve))
        if abs(ve - ae) < PULSE_PERIOD_S * 0.5:
            offsets.append(ve - ae)
    if len(offsets) < 2:
        return None
    offsets.sort()
    return offsets[len(offsets) // 2], len(offsets)


def _read_audio_rms(
    read_fd: int,
    *,
    sample_rate: int,
    channels: int,
    sample_bytes: int,
    duration_s: float,
    window_s: float = 0.02,
) -> list[tuple[float, float]]:
    """Sample RMS amplitude over time from the raw f32le tap (little-endian)."""
    series: list[tuple[float, float]] = []
    floats_per_window = max(1, int(sample_rate * window_s)) * channels
    align = channels * sample_bytes
    prev_t = time.monotonic()
    deadline = prev_t + duration_s
    while time.monotonic() < deadline:
        chunk = os.read(read_fd, 65_536)
        now = time.monotonic()
        if not chunk:
            break
        usable = len(chunk) - (len(chunk) % align)
        samples = array("f")
        samples.frombytes(chunk[:usable])
        span = now - prev_t
        total = len(samples)
        for i in range(0, total - floats_per_window, floats_per_window):
            window = samples[i : i + floats_per_window]
            rms = math.sqrt(sum(s * s for s in window) / len(window))
            frac = (i + floats_per_window) / total if total else 1.0
            series.append((prev_t + span * frac, rms))
        prev_t = now
    return series


def measure_av_offset(
    *,
    capture_method: CaptureMethod,
    width: int,
    height: int,
    jpeg_quality: int,
    headless: bool,
    duration_s: float = 14.0,
    log: Callable[[str], None] = print,
) -> float | None:
    """Measure how far video lags audio (seconds) through the capture path.

    Returns the offset to delay audio by (positive == audio is ahead), or None
    if calibration could not run or detect a clean signal.
    """
    if not audiotee_available():
        return None

    frames: list[tuple[float, int]] = []

    def collect(jpeg: bytes) -> None:
        frames.append((time.monotonic(), len(jpeg)))

    screencaster = TabScreencaster(
        _calibration_url(),
        width=width,
        height=height,
        fps=30,
        jpeg_quality=jpeg_quality,
        on_frame=collect,
        headless=headless,
        capture_audio=True,
        capture_method=capture_method,
    )
    audio_capture = None
    try:
        screencaster.start()
        screencaster.wait_until_ready()
        screencaster.enable_capture()
        try:
            audio_capture = try_start_chrome_audio_capture(
                screencaster.user_data_dir,
                on_retry=screencaster.nudge_playback,
            )
        except AudioCaptureError:
            return None

        fmt = audio_capture.audio_format
        audio_series = _read_audio_rms(
            audio_capture.read_fd,
            sample_rate=fmt.sample_rate,
            channels=fmt.channels,
            sample_bytes=fmt.sample_bytes,
            duration_s=duration_s,
        )
    finally:
        screencaster.stop()
        stop_audio_capture(audio_capture)

    video_events = _detect_events(frames, ratio=3.0)
    audio_events = _detect_events(audio_series, ratio=4.0)
    result = _median_offset(video_events, audio_events)
    log(
        f"Calibration: detected {len(video_events)} flashes / "
        f"{len(audio_events)} beeps."
    )
    if result is None:
        return None
    offset_s, matched = result
    log(f"Calibration: matched {matched} flash/beep pairs.")
    return offset_s
