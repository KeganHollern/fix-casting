# CPU-reduction spike plan (roadmap item 10)

*Scoped 2026-07-05 on `fable-refactor`. This is the design note for the L-size
spike; execute in a dedicated session, not the review loop.*

## Why

CPU/heat is the suspected cause of the long-run encoder stalls (VideoToolbox
throttling under thermal pressure → stdin-write stalls → dropped frames →
stutter). Halving the pipeline's CPU is the most promising fix, and it also
lowers fan noise and battery burn for laptop use.

## Baseline (measured)

40 s clapboard run through the real pipeline (`tools/measure_source_skew.py`,
1080p30, JPEG q75, VideoToolbox H.264), steady-state `ps` samples on this M-series Mac:

| Process | CPU (steady) | What it's doing |
|---|---|---|
| **ffmpeg** | **~170–210%** (peaks 350%) | software MJPEG decode @30fps 1080p, yuv420p convert, HW H.264 encode, HLS mux |
| Chrome (all processes) | ~15–50% | page render + per-frame JPEG encode (screencast) |
| Python | ~6–20% | base64-decode frames, pacing, pipe writes |
| AudioTee | ~0% | PCM passthrough |

The dominant cost is the **JPEG round-trip**: Chrome encodes every frame to
JPEG → Python base64-decodes → ffmpeg software-decodes MJPEG → VideoToolbox
encodes H.264. The H.264 encode itself is hardware and nearly free; the
software JPEG decode in ffmpeg is the bottleneck, with Chrome's JPEG encode
second. Note the production default is q92 (this run used q75), so real-world
numbers skew higher.

## Candidate approaches

### A. Chrome-side H.264 via `getDisplayMedia` + `MediaRecorder` (primary)

A companion page (or extension-less script injected into a control tab)
captures the cast tab and hands MediaRecorder an H.264 track; Chrome's own
hardware encoder produces the bitstream. Python then only remuxes
(`-c copy`) into HLS — ffmpeg's software decode disappears entirely.

- Expected win: ffmpeg drops to mux-only (~10–20%); Chrome loses the JPEG
  encode but gains HW encode (net ~flat); Python stops decoding base64 frames.
  **Total estimate: ~250% → ~60–80%.**
- Launch flags to trial: `--auto-accept-this-tab-capture`,
  `--use-fake-ui-for-media-stream`, `--enable-usermedia-screen-capturing`.
- **Risks:**
  1. **The A/V sync model changes completely.** Today's sync rests on
     image2pipe arrival-time stamping (first-frame anchor, bounded queue =
     audio lead, adelay trim). A MediaRecorder stream carries its own PTS;
     AudioTee audio must be aligned to it. This invalidates the hardest-won
     code in the repo — budget most of the spike here, and gate on the
     existing skew harness.
  2. MediaRecorder H.264 support/keyframe cadence: HLS needs ~2 s IDR spacing;
     MediaRecorder gives limited keyframe control (`videoKeyFrameIntervalDuration`
     in newer Chrome). If unavailable → segments can't split cleanly.
  3. Container: MediaRecorder emits fragmented WebM/MP4; remux path needs
     validation (`ffmpeg -i pipe -c copy -f hls`).
  4. Variable frame rate output vs the TV-buffer-draining concern that
     motivated constant-rate sampling — needs a long-run TV test.

### B. WebCodecs `VideoEncoder` in the page (fallback to A)

Same capture, but explicit `VideoEncoder` control (exact keyframe cadence,
bitrate, low-latency tuning), shipping EncodedVideoChunks out via WebSocket to
Python. More moving parts than A but removes A's risks 2 and 4. Choose B only
if A's keyframe control proves inadequate.

### C. Capture-resolution reduction + encoder upscale (cheap, orthogonal)

Capture at 0.75× (1440×810) and let VideoToolbox upscale during encode
(`-vf scale=1920:1080`): JPEG encode+decode cost scales with pixels (~44%
saving on the dominant stage) for a modest sharpness loss. No sync-model
change. Worth an afternoon regardless of A/B; could ship as `--capture-scale`.

### D. Tuning-only floor (already available)

`--jpeg-quality 60 --fps 24` cuts meaningful CPU today with zero code. Document
as the thermal-mitigation stopgap.

## Experiment plan (gates in order)

1. **Prototype A capture:** minimal Playwright script; verify tab capture
   auto-accepts headfully and MediaRecorder produces H.264 (check
   `MediaRecorder.isTypeSupported('video/mp4; codecs="avc1.42E01E"')` and real
   chunks). *Gate: H.264 chunks with controllable ~2 s keyframes.*
2. **Remux path:** pipe chunks to `ffmpeg -c copy` → HLS; play on the actual
   Chromecast for 10 min. *Gate: clean playback, no segment errors.*
3. **Sync:** adapt `measure_source_skew.py` to path A (flash/beep through
   MediaRecorder + AudioTee). *Gate: |median offset| ≤ 133 ms and stable over
   30 min (use track-duration drift check, not looping clapboard).*
4. **CPU + thermal:** repeat the baseline table; run 2 h buffered cast watching
   for encoder-stall stutter. *Gate: total CPU ≤ half of baseline, zero
   sustained-backpressure restarts.*
5. Only then: productionize behind `--capture h264|screencast` with screencast
   as fallback (some pages break under tab capture).

If gate 1 or 2 fails → try B; if B is too complex → ship C + D and revisit.

## Out of scope for the spike

HEVC/AV1 output, LL-HLS, cross-platform audio — separate roadmap lines.
