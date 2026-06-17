# A/V sync investigation — audio plays ~1s ahead of video on the cast

_Status: root cause found and confirmed by measurement. Fix proposed, not yet
applied._

## The system (what we're debugging)

`cast <url>` mirrors a Chrome tab to a Chromecast:

1. **Video**: CDP `Page.startScreencast` pushes JPEG frames as the page paints.
2. **Audio**: AudioTee taps the cast Chrome's process audio → raw PCM
   (`f32le`, 48 kHz, stereo), handed to ffmpeg on a dedicated fd.
3. **Mux**: ffmpeg takes MJPEG (image2pipe) + raw PCM → H.264/AAC → HLS (mpegts).
4. **Play**: pychromecast loads the HLS playlist (buffered ~45 s on the TV).

Internal video path inside `HLSStreamer` (streamer.py):

```
publish_frame(jpeg) → LatestFrame holder → sampler thread (even 30fps tick,
   repeats latest if nothing new) → bounded deque queue → writer thread →
   ffmpeg stdin (image2pipe)
```

Audio path: AudioTee → OS pipe → ffmpeg reads the fd directly (NO Python queue
in the path).

## The symptom

On the cast, **audio plays ~1 s ahead of the video** (lip-sync off; sound
leads picture). A manual `--audio-offset-ms 1000` made it look synced, but we
wanted the root cause, not a magic constant.

## How ffmpeg aligns the two inputs (important background)

Both inputs are **headerless / timestamp-free**: raw PCM is just sample bytes;
the MJPEG pipe is just concatenated JPEGs. ffmpeg assigns PTS **positionally**:

- audio PTS = samples_read / 48000 (anchored to the first sample it reads)
- video PTS = frames_read / 30 (`-framerate 30`, anchored to the first frame)

We do **not** pass `-use_wallclock_as_timestamps`. So alignment depends only on
*when ffmpeg starts reading each stream* + the declared rates. There are no
timestamps in the data for ffmpeg to honor or corrupt — which means ffmpeg
cannot be "mis-aligning by timestamp." It just stamps stale-video-byte-0 and
fresh-audio-byte-0 both as PTS 0.

## The investigation (each test and what it proved)

We measured with a "clapboard": a clip whose white **flash** (video) and 1 kHz
**beep** (audio) are encoded together every 3 s (`tools/clapboard_clip.mp4`,
verified internally `+0 ms` synced). Convention below: **"audio ahead"** = beep
appears before the matching flash.

| # | Test | Tool | Result | Proves |
|---|------|------|--------|--------|
| 1 | Muxer with a perfectly-synced source through the REAL `HLSStreamer` (MJPEG pipe + raw-PCM fd), no Chrome/AudioTee | `tools/test_pipeline_skew.py`, `tools/serve_synced_hls.py` | **~67 ms** audio ahead | Our ffmpeg/HLS muxing is essentially clean. Synced in → synced out. |
| 2 | Full live path (Chrome `<video>` + AudioTee → HLS) | `tools/measure_source_skew.py --page clapboard_video.html` / `clapboard_av_page.html` | **~716–867 ms** audio ahead | The skew is real end-to-end. |
| 3 | Same encoder at 1080p + production bitrate/bufsize, synced file source | `test_pipeline_skew.py --width 1920 --height 1080` | **~67 ms** | NOT resolution / bitrate / bufsize / encoder. |
| 4 | DOM freshness: capture a live DOM ms-clock via CDP, compare to the real browser | `tools/test_cdp_delay.py` + `tools/cdp_clock.html` | captured clock ~66 ms behind real (incl. MJPEG re-display) | CDP **delivery** of page content is prompt (~tens of ms), not ~1 s. |
| 5 | rVFC vs the video's burned timecode, inside the capture | `cdp_clock.html` | they match | The screencast faithfully captures whatever video Chrome **presents** (but this is blind to audio). |
| 6 | **Input boundary**: record CDP video and AudioTee audio SEPARATELY (no mux), compare flash vs beep arrival times | `tools/probe_input_skew.py` + `clapboard_av_page.html` | **−119 ms** (flash arrives ~119 ms *before* beep; stable, spread 71 ms) | The inputs arrive at our code **essentially in sync** (audio a hair behind, ≈ AudioTee's 100 ms chunk). The ~1 s is NOT present at the input. |
| 7 | Full live path WITH per-stage `--stats` instrumentation | `measure_source_skew.py` (now prints stage latencies) | output **−867 ms** + `stdin write peak 4010 ms`, `queue peak 90`, `dropped 31` | **Root cause** (below). |

### Wrong turns (for the record / intellectual honesty)

- Hypothesis "ffmpeg/HLS muxing induces it" → **refuted** by test 1 (67 ms) and
  the fact that the inputs carry no timestamps.
- Hypothesis "CDP screencast delivers frames ~1 s late" → **refuted** by test 4
  (DOM captured fresh).
- Hypothesis "Chrome presents `<video>` ~1 s behind its audio, and we faithfully
  capture the stale video" → stated too confidently; **refuted** by test 6
  (inputs arrive synced, audio actually slightly *behind* video).
- Deferred-spawn change (wait for first frame before spawning ffmpeg so both
  inputs anchor PTS=0 together): correct in principle, kept as robustness, but
  did **not** fix the skew — the startup anchor gap was a red herring for the
  magnitude.

Lesson: every time the conclusion was reached by *reasoning*, it was wrong.
The instrumented measurement (test 7) found it immediately.

## ROOT CAUSE

A startup ffmpeg backpressure stall, amplified by an asymmetry between the video
and audio paths in our own code.

1. At startup ffmpeg **stalls reading its video stdin for ~4 s** while it
   initializes the first HLS segment / videotoolbox encoder
   (`stdin write peak 4010 ms`).
2. During the stall the sampler keeps producing 30 fps, so frames pile into the
   **bounded video queue until it hits its max of `fps*3` = 90 frames (3 s)**
   and overflows (`queue peak 90`, `dropped 31`).
3. **Audio does not accumulate the same way**: it's a separate, small-bandwidth
   fd (~384 KB/s) that ffmpeg reads directly; its OS pipe only buffers ~170 ms
   (and we drain the pre-roll). So during the stall video backs up ~3 s while
   audio backs up ~0.17 s.
4. After the stall, **in-rate ≈ out-rate** (writer drains ~31 fps, sampler feeds
   30 fps), so the queue **stays deep** — the startup backlog never drains.
   Video is now permanently ~26 frames (~867 ms) behind audio.

Net: **video flows through our deep 3-second queue; audio flows through a shallow
direct fd. Any ffmpeg backpressure delays only video, and a one-time startup
stall makes the offset permanent.** → audio plays ahead by ~the queue depth.

This is why test 1 (smooth file feed, no startup stall → queue never fills) was
67 ms, but the live capture is ~867 ms. The persistent, content-independent,
resolution-independent ~1 s is the steady-state queue residence created at
startup.

## THE FIX (proposed)

`HLSStreamer._queue_maxlen = max(1, self.fps * 3)` (90 frames / 3 s) is far too
deep. It was meant to absorb brief write stalls (keyframe/segment flush) for
smooth sampling — but the TV's 45 s buffer already provides smoothness, so a
3-second *local* queue is pure latency that desyncs A/V.

Bound the queue shallow so video latency tracks audio's:

- Set `_queue_maxlen` to ~`max(2, fps // 4)` (~7 frames ≈ 230 ms).
- The existing policy already drops the **oldest** frame on overflow, so under
  backpressure it stays current instead of hoarding 3 s of video.
- A startup stall then costs a brief one-time frame drop (a momentary skip,
  invisible behind the 45 s TV buffer) instead of a permanent ~867 ms skew.

Expected after the fix (re-run test 7): `queue peak` small, `dropped` may tick up
during the startup stall, and the output offset collapses from ~867 ms toward the
~67 ms muxer floor — ideally removing the need for any `--audio-offset-ms`.

Possible refinements if a flat shallow queue drops too much in normal operation:
detect sustained queue depth and flush-to-latest once after startup, or drop
based on frame age rather than a fixed maxlen.

## Instrumentation / tools built (all under tools/)

- `clapboard_clip.mp4` / `clapboard_clip_1080.mp4` — flash+beep synced source.
- `timecode_clip.mp4` — ffmpeg `testsrc` with a built-in timecode (for CDP probe).
- `measure_source_skew.py` — full-pipeline output skew + (now) per-stage stats.
- `test_pipeline_skew.py` — synced source through the real `HLSStreamer`, no
  Chrome/AudioTee (isolates the muxer). `--clip/--width/--height`.
- `serve_synced_hls.py` — serves the synced source through the real path over
  HTTP so a human can watch the HLS output and judge sync (and `--offset-ms` to
  prove the harness is sensitive).
- `test_cdp_delay.py` + `cdp_clock.html` — live MJPEG of the CDP capture +
  saved .mkv, to eyeball DOM-vs-real and rVFC-vs-video-pixel delay.
- `probe_input_skew.py` + `clapboard_av_page.html` — records CDP video and
  AudioTee audio separately and measures flash-vs-beep skew at the input
  boundary (no mux, no subtraction).
- The streamer/stats already gained `--stats` instrumentation during this work:
  `chrome→app lag`, audio pipe backlog, and one-shot lifecycle `[trace]` lines.

## Also worth knowing (side findings)

- `metadata.timestamp` on each screencast frame is Chrome's compositor swap time
  (≈ when the frame was produced). `time.time() - metadata.timestamp` ≈ 14 ms in
  steady state → CDP wire delivery is fast. (One ~7.5 s spike at startup = the
  buffered warmup frame Chrome ships first; transient, not the cause.)
- AudioTee delivers audio in 100 ms chunks, so audio arrives ~100 ms granular.
- The source-side skew scales nothing here; the offset is content-independent
  because it's our queue depth, not anything about the media.
