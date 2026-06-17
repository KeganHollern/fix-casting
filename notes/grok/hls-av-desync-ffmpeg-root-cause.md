# HLS A/V Desync (~1s audio leads video) — Root Cause Hypothesis & Plan

**Date**: 2026-06-16 (updated with new probe data)
**Workspace**: /Users/kegan/git/fix-casting  
**Status**: Strong evidence now points to the HLSStreamer bridging + spawn/feeding logic (not raw source or "ffmpeg after the pipes magically on its own").

## Executive Summary (updated with fresh measurements)
New tooling run during research:

- `tools/serve_synced_hls.py --offset-ms 1000`: Fed a *known +0 synced* clip (clapboard_clip.mp4 with baked-in flash+beep) through the *exact production* HLSStreamer path (image2pipe MJPEG video + raw PCM fd audio, same sampler/writer/queue, same HLS args, same adelay mechanism). Required manually applying 1000 ms audio delay (plus the normal pre-roll drain) to have any hope of sync in the output HLS.

- `tools/probe_input_skew.py --seconds 30` (on `clapboard_av_page.html`, which plays the synced clip unmuted so both CDP and AudioTee see the same source events):
  - Captures the two inputs *separately* (no streamer, no mux): CDP screencast JPEGs → .mkv (stamping real wall-time on arrival in the on_frame callback) + AudioTee PCM fd (anchored t0 on first post-drain byte).
  - Finds flashes in video (via signalstats) and beeps in audio (via silencedetect), maps both to absolute wall arrival times.
  - Result: `flash_arrival - beep_arrival` median **-119 ms** (spread 71 ms). Printed conclusion: "Inputs arrive within ~200ms — NOT meaningfully skewed at the boundary. The skew would be elsewhere; my earlier claim is wrong."

- `tools/measure_source_skew.py --page tools/clapboard_av_page.html --seconds 30` (full real path: TabScreencaster + real AudioTee attach + real HLSStreamer + publish_frame wiring):
  - End-to-end through publish → LatestFrame → sampler (even 30 fps cadence) → queue → writer to stdin + audio fd at spawn.
  - In the produced `all.ts`: median (beep - flash) **-762 ms** (audio ahead by ~762 ms, spread 102 ms). "use --audio-offset-ms 760".

**Conclusion from the data**: The raw inputs at the Python boundary (CDP frames + AudioTee bytes) are nearly aligned (~120 ms differential, video slightly earlier). The ~640–760 ms of *extra* audio-lead skew is induced *after* `publish_frame` / audio attach, by the HLSStreamer feeding machinery + `_start_ffmpeg` timing + the ffmpeg command line as currently constructed. The earlier "encode frame age avg 0 ms, stdin write 0.2 ms, queue peak 1" metrics only measured latency *after* publish; they did not measure the relative wall-time delta between "first valid post-drain audio bytes being readable by the child" vs. "first JPEG bytes actually being read by the mjpeg demuxer".

This matches the ~700 ms that `test_pipeline_skew.py`'s docstring has been warning about for a long time. The 1 s the user originally saw is the same phenomenon.

The mistake is in *our* code (primarily `cast_tab/streamer.py`), not "FFmpeg itself after the data is in the pipes in a vacuum."

## Revised Detailed Hypothesis (incorporating the new probe + end-to-end numbers)

The skew is a **systematic offset between the two input clocks as seen by ffmpeg**, created by the exact sequence and decoupling in `HLSStreamer`.

### How the two inputs get their timestamps (unchanged)
- Video via image2pipe: purely synthetic. First JPEG that ffmpeg's demuxer *actually reads* from stdin → video PTS=0. Then +1/fps per subsequent read. No wall-clock involvement.
- Audio via raw fd (or lavfi): first sample(s) ffmpeg *actually reads* from the passed fd → audio PTS=0. Then sample-count / rate.

The muxer simply aligns packets by those PTS values. If the wall moment when ffmpeg reads the first video frame is D later than the wall moment when it read the first audio samples, you get D ms of audio lead in every segment, forever.

### What the new data proves about *where* D comes from
- `probe_input_skew.py`: At the *Python boundary* (right after CDP callback does `publish_frame`, and right after AudioTee bytes are readable on the fd), differential arrival for simultaneous source events is only ~120 ms (video slightly earlier). The source clip events are +0; the capture paths (screencast + AudioTee) do not introduce the 762 ms.
- `measure_source_skew.py` (identical source page, but now wired through the real streamer): 762 ms audio ahead in the final TS.
- `serve_synced_hls.py --offset-ms 1000` (pumps the exact same +0 clip's frames + PCM through the *identical* `HLSStreamer` constructor + `publish_frame` + `start()` + internal sampler/writer + adelay path): needed ~1 s of artificial audio delay to even have a chance.

Therefore the additional ~640–760 ms is added by the code between `publish_frame` (or the pump calling it) / fd and the two demuxers inside the child seeing their first data.

### The exact sequence that opens the gap (current code)
(See `cast_tab/streamer.py:255` (start), `281` (publish_frame), `416` (_drain), `490` (_start_ffmpeg), `575` (sampler), `625` (writer), `552` (drain call), `557` (Popen).)

In production flow (cli.py):
- AudioTee starts writing (and keeps writing) when `try_start_chrome_audio_capture` succeeds — *before* the HLSStreamer is even constructed.
- First real `publish_frame` (first CDP JPEG) arrives some time later.
- `streamer.start()`:
  1. Blocks on `_first_frame.wait()` until the first publish sets it (and populates LatestFrame).
  2. `_start_ffmpeg()`:
     - Builds cmd (video input first, then audio input args with pass_fds).
     - Calls `_drain_audio_fd()` (drops whatever has accumulated in the read pipe since AudioTee started — in the run: "dropped 200ms").
     - `Popen(...)` — child is born. The fd it inherits for audio is now at "live" position. stdin pipe for video has nothing in it yet.
  3. Only *after* Popen returns: `_start_sampler_thread()` and `_start_writer_thread()`.
  4. Sampler does its first tick (using whatever is now in LatestFrame — the one from the publish that unblocked the wait), enqueues it.
  5. Writer dequeues and does the first `stdin.write(...) + flush()`.

Between the Popen and the first successful read of that JPEG by ffmpeg's mjpeg demuxer there is:
- Child init time (parsing, opening the /dev/fd/N for audio, setting up filters/encoders/HLS muxer/segmenter — especially noticeable with h264_videotoolbox).
- Python thread creation/scheduling + the sampler's `next_tick` arithmetic + condvar wake + writer lock + the actual write.
- During this entire window the audio demuxer in the child can (and does) start pulling bytes from the fd and advancing audio PTS from 0.

In the pumped tests (`serve_synced_hls.py`):
- The two pump threads are started *first* and immediately begin calling `publish_frame` on wall cadence and `os.write` to the pipe write end on wall cadence.
- Only then is `streamer.start()` called.
- `start()` still does the "wait for a publish" (which may already be satisfied), the drain (some audio the pump has already written will be dropped), the Popen, *then* starts the internal sampler.
- The first video bytes that the *internal* writer actually pushes into stdin still happen after the child has begun life and can read audio.

Result in both cases: the logical "t=0 frame" for video (the first JPEG that gets a PTS=0 label inside ffmpeg) represents a wall moment that is later than the "t=0" for audio.

The "frame age 0 ms" and "encode fps matches" stats are measured from the publish time of the frame we eventually sample. They do not capture that the publish of the *anchor* frame itself, plus the post-Popen feeding delay, is what shifts video's PTS=0 relative to audio's.

The internal "even cadence + latest frame + bounded queue + catch-up repeats" design is brilliant for eliminating judder and keeping the *output rate* locked to wall time once running. It just doesn't solve the *initial relative anchoring* of the two synthetic input timelines.

The adelay path (and MAX_AUTO_AV_OFFSET_S) exists precisely because the authors knew audio would structurally be ahead; it was always intended as a manual dial for whatever residual the anchoring left. The new measurements show the residual left by the current anchoring is ~760 ms (close to the 1 s originally observed and the 1 s manually applied in serve_synced_hls).

### Why the old "FFmpeg itself" framing was incomplete
The user's initial metrics + assumption ("frames going in to ffmpeg are coming out ~1s delayed", "audio pipe backlog 0", "capture/encode metrics clean") were true but incomplete. They measured after the publish point. The probe_input_skew (which bypasses the streamer entirely) + the delta between probe and full measure_source_skew now localize the induction to the streamer.py logic around spawn + first write.

The comments in streamer.py were remarkably prescient ("baking that whole gap in as audio-ahead skew", "video ~0.5s behind the audio", "the video frame-queue latency we compensate for"). The implementation of the anchoring just didn't close the gap tightly enough.

## Contributing (but secondary) Factors
- The post-Popen start of the sampler/writer threads + the "sample latest on a separate cadence" design.
- No `-thread_queue_size` for the video input (audio gets 4096).
- `h264_videotoolbox` path lacks the low-latency flags present in the libx264 fallback.
- No `-use_wallclock_as_timestamps 1` on either input, no `-itsoffset` (the code already notes it is ignored for raw PCM), no `-vsync cfr`, no `-muxpreload`/`-muxdelay` 0, no explicit common timebase in a filter graph.
- Encoder/decoder startup + first-keyframe latency on the video side (mjpeg decode + h264_vt encode + GOP) vs. the much lighter audio path can add a few frames once the first data is read.
- The adelay mechanism is the only runtime compensation and is purely manual (users have been carrying ~760–1000 ms of it).

The core architectural choice (decouple sampling from writing for judder-free constant-rate output, hold spawn until first publish, drain only at Popen time, feed first video bytes only via the post-spawn writer thread) is the source of the fixed D.

## Recommended Fix (targeted, minimal diff)
Keep the even-pacing / queue / catch-up / restart / direct-fd / adelay design (they solve important problems). Tighten (or re-architect) only the initial relative anchoring of the two input timelines.

**Primary change** (in `cast_tab/streamer.py`):
- Make the first video data readable by the child at (or before) the instant the child can start reading the audio fd.
  - Best: after `Popen`, immediately (synchronously in the main thread, before starting the periodic sampler/writer) do the first `stdin.write( the anchor frame from LatestFrame ) + flush()`. This is the change that was already in the earlier plan.
  - Even stronger: explore writing a priming frame *before* the Popen (the pipe write end is in the parent; data will sit in the kernel pipe buffer until the child opens its read end). This makes the first mjpeg packet available the moment the child is ready, symmetrically with the drained audio fd.
  - Add a tiny `_initial_video_fed` flag (or reuse the generation) so the later writer thread does not duplicate the anchor write.
- Consider moving the "start the cadence threads" logic so that the very first video write is not gated behind another thread scheduling quantum after the child has already exec'd.
- Re-examine the pump tests vs. real flow: in `serve_synced_hls.py` the external pumps are already running before `start()`. The internal sampler is still started *after* spawn. The first bytes the streamer actually pushes may still be post-spawn.

Secondary / high-leverage command-line additions (easy to try in parallel):
- Add `-thread_queue_size 512` (or 1024) before the video `-f image2pipe -i pipe:0`.
- Add `-use_wallclock_as_timestamps 1` for both inputs (before their -i). This makes the demuxers base PTS on *when they actually received the packet* rather than pure synthetic count. If the first video write and the first post-drain audio read happen close in wall time (thanks to the pre-feed), the resulting PTS bases will be close even if the synthetic counting would have been offset.
- In the h264_videotoolbox branch: add any low-latency / realtime options available.
- Add `-vsync cfr -muxpreload 0 -muxdelay 0` (or small values) before the output file. These are classic for reducing startup skew in live piped A/V.
- (Advanced) If the above still leaves a consistent residual, compute an automatic base offset once at startup (using the existing trace points or a one-time measurement) and apply it via adelay by default when the user passes 0. The manual `--audio-offset-ms` remains the override.

The goal is to drive the number reported by `measure_source_skew.py` / `test_pipeline_skew.py` from the current ~-760 ms down to < 100 ms (or whatever the probe_input_skew boundary already achieves) when no manual offset is supplied.

No changes needed outside `cast_tab/streamer.py` for the core fix (the new probe and serve tools can stay as permanent diagnostics).

## Files & Code to Touch
- `cast_tab/streamer.py` (the only file that needs edits):
  - `HLSStreamer._start_ffmpeg` (Popen site + immediate first write + drain call)
  - `HLSStreamer.start` (order of thread starts vs. the new sync write)
  - `HLSStreamer._start_sampler_thread` / `_start_writer_thread` (guard the very first frame so it isn't duplicated)
  - `_video_encoder_args` (low-latency additions for videotoolbox)
  - Possibly the `LatestFrame` peek or a tiny "consume initial" helper
- Tests / tools (no logic changes, just run them):
  - `tools/test_pipeline_skew.py` (the oracle — feeds perfectly synced source through the *real* streamer)
  - `tools/measure_source_skew.py`
  - `tools/clapboard*.html`, `tools/test_cdp_delay.py` (already used by the user)
- `notes/grok/...` (this file) — for the record

Existing utilities that stay 100 % reusable:
- `LatestFrame`, the queue/condvar, `_drain_audio_fd`, `_audio_input_args`, pass_fds handling, all the stats traces and `PipelineStats`, the catch-up/repeat/resync logic, the adelay path, `_ffmpeg_supports_encoder`, bitrate/gop/hls arg helpers, etc.

## Verification Steps (use these — they are now the gold standard)
Use the tools the user just exercised with Claude; they give the clearest signal:

1. **Direct boundary check (no streamer involvement)**:
   ```
   python tools/probe_input_skew.py --seconds 30
   ```
   Should continue to report ~100-150 ms or better (the current -119 ms result). This must stay small; any regression here would be a capture problem, not the streamer.

2. **Pumped known-zero source through the exact production path (fastest oracle for streamer+ffmpeg)**:
   ```
   python tools/serve_synced_hls.py --offset-ms 0     # or with compensation
   # then open the printed m3u8 in VLC and watch the 3 s flash+beep
   ```
   Or the automated version:
   ```
   python tools/test_pipeline_skew.py --seconds 30
   ```
   - Before the anchoring fix: expect median ~ -700 to -1000 ms (audio ahead), matching the 762 ms the user just measured and the 1000 ms they had to apply manually.
   - After: the median should drop dramatically (target < 150 ms or whatever the probe boundary already achieves). The script already has the "CLEAN" / "INDUCES" messages.

3. **Full real capture + real AudioTee + real streamer (end-to-end)**:
   ```
   python tools/measure_source_skew.py --page tools/clapboard_av_page.html --seconds 30
   ```
   (or the older clapboard*.html). This is what produced the -762 ms number. Same before/after target.

4. **Live cast regression**:
   ```
   cast --no-buffered --stats ... "https://..."
   ```
   Watch the new "first frame written" delta vs. spawn trace, the usual stats, and real content with audio events. The amount of manual `--audio-offset-ms` needed should be much smaller.

5. **Container PTS check** (cheap):
   After any of the above, inspect a captured `all.ts` or individual segments with the same find_flashes / find_beeps logic or `ffprobe -show_frames`.

All four tools (`probe_input_skew`, `serve_synced_hls`, `test_pipeline_skew`, `measure_source_skew`) plus the two clapboard pages should be kept and used for any change to streamer.py. They isolate exactly the seam that was previously hidden by the "frame age 0 ms" stats.

## Why Other Places Are Now Ruled Out (with the new direct measurements)
- Source page / baked-in A/V in the clip (`clapboard_clip.mp4`): known +0 (used in serve_synced_hls and the probe).
- Browser rendering + <video> playback of the clip: the av_page plays it unmuted; the probe captures the actual pixels and the actual emitted audio.
- CDP screencast delivery to Python: `probe_input_skew.py` directly stamps `time.time()` the instant the callback fires and records the JPEGs; flashes are found in the resulting mkv using the same detector as the full measure.
- AudioTee / fd arrival to Python: the probe drains then anchors the first byte read in a dedicated thread to a wall t0; beeps are found in the captured pcm using the same silencedetect path.
- Result of the above two: only -119 ms differential at the boundary.
- "Our stats showed frame age 0 / writes fast / queue=1": those stats start counting at `publish_frame`. The probe proves that publish times themselves are close to audio byte arrival times. The extra hundreds of ms appears between publish and "ffmpeg demuxer has read the corresponding packet."
- TV / Chromecast / HLS playlist buffering / segment age: the measure_source_skew and test_pipeline_skew analyze the raw concatenated .ts segments with ffprobe-style filters (signalstats + silencedetect) and look at *PTS times inside the container*, not wall playback position on a device. The offset is in the container timestamps.

The induction is localized to the window `publish_frame` (or external pump) → LatestFrame → (wait for first) → drain + Popen in _start_ffmpeg → start of sampler/writer → first actual stdin.write that ffmpeg's mjpeg demuxer consumes, vs. the audio bytes the child consumes on the passed fd starting from the same Popen.

This is exactly the area the original anchoring comments were trying (but not fully succeeding) to protect. The new tools (probe_input_skew + serve_synced_hls + the av_page) are excellent permanent additions for regression testing this exact seam.

---

Ready to implement a fix. The pre-feed of the first frame + defensive input options (`-use_wallclock_as_timestamps 1`, thread_queue_size on video, vsync/mux* flags) + possibly a small automatic base compensation are the most promising next steps. The test_pipeline_skew.py and the new serve_synced_hls.py + probe_input_skew.py give fast, repeatable oracles.

---

(End of hypothesis + plan. Ready to implement once reviewed.)