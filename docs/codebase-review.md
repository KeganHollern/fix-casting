# fix-casting — Codebase Review & Roadmap

*Drafted 2026-07-04. Reviewed at commit `48b908c` (main). ~2,600 lines of Python in `cast_tab/`, plus test harnesses in `tools/` and a vendored Swift AudioTee.*

## Executive summary

The codebase is in good shape for its size and age: the pipeline architecture (CDP screencast → paced sampler → bounded queue → ffmpeg → HLS → Chromecast) is sound, the A/V-sync design decisions are unusually well documented in comments, and the hardest problems (per-tab audio, judder-free pacing, bounded audio lead) have been solved and verified with purpose-built measurement tools. The main gaps are **operational robustness** (a handful of real bugs around shutdown, ffmpeg stderr, and temp-dir hygiene), **structure** (a 230-line `main()` that duplicates orchestration also needed by the tools harnesses), and **sustainability** (no automated tests, no CI, no lint/type tooling, dead code accumulating from past experiments). The product itself works; the highest-leverage next steps are hardening long-run behavior and lowering the install/CPU cost.

Priorities at a glance:

1. **Fix now (bugs):** ffmpeg stderr can deadlock the encoder; error exits report success (exit code 0); Chrome profile temp dirs are never cleaned up; fixed `/tmp/cast-tab-stream` breaks concurrent runs.
2. **Refactor next:** extract a `CastSession` lifecycle object; split `streamer.py`; delete dead knobs/stats; single source of dependency truth.
3. **Invest after:** pytest + CI around the existing pipeline-skew harness; then product work (device flag, resilience/auto-reconnect, prebuilt AudioTee, lower-CPU capture).

---

## 1. Architecture snapshot

```
cli.py          orchestration: discovery → browser → audio → streamer → caster → stats loop
browser.py      Playwright/Chrome + CDP Page.startScreencast (paint-driven JPEG push)
audio.py        AudioTee subprocess; its stdout fd is handed directly to ffmpeg (no relay)
streamer.py     paced sampler → bounded frame queue → ffmpeg (image2pipe + PCM fd) → HLS + HTTP
caster.py       pychromecast: load HLS URL, verify playback, poll status
devices.py      mDNS discovery + interactive picker
stats.py        thread-safe counters → StatsSnapshot (shared by --stats text and --tui)
tui.py          Textual dashboard + live audio-offset knob
adblocking.py   uBO/Peter-Lowe domain list → CDP Network.setBlockedURLs
```

**What's genuinely good and should be preserved:**

- The **first-frame anchor** (hold ffmpeg spawn until the first captured frame so audio and video PTS=0 coincide) and the **bounded frame queue** ("queue depth IS the audio lead") are the two insights that make sync work. They're documented where they live. Don't lose these comments in any refactor.
- Audio fd passed **directly** to ffmpeg via `pass_fds` — no Python relay thread on the real-time path. Correct call, well justified in `audio.py:193`.
- Even-paced sampling decoupled from the (stalling) ffmpeg write via the sampler/writer thread pair — this is what killed the judder.
- Ad blocking done natively in Chrome via CDP instead of per-request Python interception — the commit history shows this was learned the hard way.
- `tools/measure_source_skew.py` and `tools/test_pipeline_skew.py` are real measurement instruments, not toys. They're the seed of a test suite (§4).

---

## 2. Bugs and robustness issues (prioritized)

### P0 — ffmpeg stderr is never drained during normal operation
`streamer.py:645` spawns ffmpeg with `stderr=subprocess.PIPE`, but the pipe is only ever read in `wait_until_ready()`'s early-exit path. During a long cast, anything ffmpeg writes to stderr (even at `-loglevel error`, e.g. repeated audio-input hiccups or HLS write errors) accumulates in a ~64 KB pipe buffer. If it fills, **ffmpeg blocks on stderr and the whole encode stalls** — which would present as exactly the kind of unexplained long-run stall this project has spent weeks chasing. Fix: a daemon thread that drains stderr into a ring buffer, surfaced via stats/TUI (which also gives you encoder error visibility you don't have today).

### P0 — error exits report success
`cli.py:214` `shutdown()` ends with `sys.exit(0)`. The `except Exception` handler at `cli.py:392` calls `shutdown()` and then `return 1` — but the `sys.exit(0)` inside `shutdown()` raises `SystemExit(0)` first, so **failures exit with code 0**. Fix: `shutdown()` should only stop components; let callers decide the exit code (and the signal handler can `sys.exit` itself).

### P1 — Chrome profile temp dirs are never removed
`browser.py:51` creates `tempfile.mkdtemp(prefix="cast-tab-chrome-")` per run and nothing deletes it. Each abandoned profile is tens-to-hundreds of MB; these accumulate until the OS clears `/tmp`. Fix: remove the tree in `stop()` after `context.close()` (best-effort).

### P1 — fixed work dir breaks concurrent runs and is a shared-/tmp hazard
`streamer.py:240` defaults to `/tmp/cast-tab-stream` and deletes `seg*.ts` on init. Two simultaneous casts silently corrupt each other's stream. Fix: `mkdtemp` per run (cleaned on stop), keeping `work_dir` as an override for the tools.

### P2 — silent fallback launch swallows the real Chrome error
`browser.py:98-116`: any exception from the `channel="chrome"` launch triggers a bare retry with bundled Chromium. When the real problem is something else (bad viewport, profile lock), the first error is discarded and the user sees only the second, unrelated one. Fix: catch narrowly, log the first error before falling back.

### P2 — `wait_until_ready` has a misplaced docstring and wall-clock timing
`streamer.py:371-374`: the docstring sits *after* the first statement (so it isn't a docstring), and the deadline uses `time.time()` (jumps on NTP/clock changes) where `time.monotonic()` is the project's own established convention.

### P3 — smaller items
- `get_local_ip()` (`streamer.py:200`) raises `OSError` on a machine with no default route; the message the user gets is opaque. Wrap with a clear "could not determine LAN IP" error. It's also re-resolved on every `playlist_url` access — resolve once.
- `audiotee_path()` (`audio.py:101`) calls `shutil.which` twice and relies on truthiness; trivial cleanup.
- `--adblock` help text in `cli.py:100` says "uBO defaults + EasyList"; the implementation (and README) deliberately use uBO + Peter Lowe's, *not* EasyList. Align the help text.
- `caster.poll_playback_stats()` and `stop()` swallow all exceptions bare — fine in intent, but at least count them into stats so a dead TV connection is visible.
- The TUI passes `refresh_s` as the snapshot interval (`tui.py:271`) rather than measured elapsed time, so per-second FPS numbers wobble under scheduler delay. Measure the actual interval.

---

## 3. Recommended refactors

### 3.1 Extract a `CastSession` from `cli.py` (highest-value refactor)
`main()` is ~230 lines mixing argument handling, device selection, browser/audio/streamer/caster lifecycle, signal handling, and the stats print-loop. The same orchestration is hand-duplicated in `tools/measure_source_skew.py` (browser + audio + streamer, no caster). Extract:

```python
class CastSession:                      # cast_tab/session.py
    def __init__(config, stats=None)    # config = one dataclass, not 12 kwargs
    def start()                         # browser → audio → streamer, wiring included
    def stop()                          # idempotent, ordered teardown
```

`cli.py` becomes: parse args → pick device → `session.start()` → `caster.play(...)` → stats/TUI loop → `session.stop()`. The tools harnesses reuse `CastSession` minus the caster, deleting their duplicated setup. This also gives `shutdown()` a single owner and fixes the exit-code bug structurally.

### 3.2 Split `streamer.py` (789 lines, three responsibilities)
- `encoder.py` — ffmpeg command construction (`_video_encoder_args`, `_audio_input_args`, `_hls_args`, bitrate tables), spawn/kill/relaunch, backpressure watchdog, **stderr drain**.
- `pacing.py` — `LatestFrame`, sampler thread, bounded queue, writer thread.
- `server.py` — the HTTP server (and `get_local_ip`).

The A/V-sync comments move with their code. Bitrate/GOP/HLS magic numbers become a small `EncoderProfile` table (buffered vs low-latency), so "~45s TV delay" is derived from `hls_time × hls_list_size` in one place instead of asserted in five README/help strings.

### 3.3 Delete dead code and dead knobs
Confirmed unused today:
- **The capture-oversampling knob is inert.** `cli.py:177` computes `capture_fps = 1.5 × encode_fps` and passes it as `TabScreencaster(fps=...)`, but `browser.py` never uses `self.fps` — CDP screencast is paint-driven and has no rate parameter. Delete the parameter and the cli computation (or, if oversampling matters, it doesn't exist and the comment is misleading).
- `PipelineStats.record_capture_timeout` / `_capture_timeouts` — never called; the `timeouts` report field can never be non-zero.
- `record_publish` / `_publish` window — written, never read.
- `default_jpeg_quality(width, height)` ignores both arguments — make it a constant.
- `bin/cast` is a stale absolute-path shim superseded by `uv tool install` (and `bin/` is gitignored anyway) — delete it.

### 3.4 One source of dependency truth
`requirements.txt` duplicates `pyproject.toml` deps and will drift. Delete it (or generate a `uv.lock` and commit that instead — `uv tool install` will honor it and installs become reproducible).

### 3.5 Vendored AudioTee hygiene
`vendor/audiotee` is a patched snapshot ("stereo mixdown patch" per the README) with upstream's `.cursorrules` etc. committed. Recommended: fork upstream to `KeganHollern/audiotee`, carry the patch there, and either subtree/submodule it or — better for users — **attach a prebuilt universal binary to a GitHub release** and have `install.sh` download it, dropping the Swift-toolchain requirement entirely (today, no Swift = silently no audio). Keep the source build as fallback.

### 3.6 Structural nits (do opportunistically)
- `PipelineStats` as a `@dataclass` with 30 underscore-private fields is awkward; a plain class with `__init__` reads better and avoids `field(default_factory=...)` noise. Grouping into per-segment sub-objects (`CaptureStats`, `EncodeStats`, …) would mirror `StatsSnapshot` and the TUI sections.
- `detected_format: dict` used as a mutable cell in `audio.py:247` — a small holder object or `queue.Queue(1)` states the intent.
- `tools/test_pipeline_skew.py` imports its sibling via `importlib.util.spec_from_file_location` — once `tools/` shares code through `cast_tab` (per 3.1), move the flash/beep analysis into `cast_tab/` or a `tools/avsync.py` module and import normally.

---

## 4. Testing, CI, and tooling (currently: none)

There are zero automated tests, no linter, no type checker, no CI. For a project whose whole value is subtle timing behavior, the harnesses in `tools/` are 80% of the way to a real suite:

1. **`tools/test_pipeline_skew.py` is CI-able today.** It exercises the *real* `HLSStreamer` (sampler, queue, ffmpeg, HLS) with no Chrome, no AudioTee, no Chromecast — just ffmpeg. Convert it into a pytest (`pytest -m slow`) that asserts flash/beep offset within a tolerance, and run it on a macOS GitHub runner. This locks in the A/V-sync fixes that took the most effort to find, including the queue-bound behavior under an injected `CAST_TEST_WRITE_DELAY_MS` stall.
2. **Unit-test the pure parts** (fast, no ffmpeg): `_parse_audio_format`, `adblocking` rule parsing (`_DOMAIN_RULE`/`_HOST_RULE` against fixture lines, including the exception/element-hiding lines that must be skipped), `_target_bitrate` tables, `PipelineStats.snapshot` reset semantics, `LatestFrame`/queue drop behavior.
3. **A long-run soak mode** for the skew harness (hours, not seconds), asserting audio continuity and drift rate via track durations on a non-looping source — matching how real regressions have actually been caught in this project. Run manually/nightly, not per-PR.
4. **Tooling:** `ruff` (lint + format) and `mypy` (the code is already fully annotated — it will nearly pass), wired into a minimal GitHub Actions workflow: ruff + mypy + unit tests on ubuntu, the pipeline-skew test on macos.

---

## 5. Product next steps

### Near term (polish what exists)
- **`--device NAME`** flag to skip the interactive picker (needed for `run.sh`-style scripting and any future scheduled/remote use; the picker also currently blocks in non-TTY contexts).
- **Resilience:** auto-reconnect / re-`play_media` when the Chromecast drops or the app is killed on the TV (poll already detects non-PLAYING; act on it). Similarly, recover if the tab crashes or navigates.
- **Surface ffmpeg errors** in `--stats`/TUI once stderr is drained (P0 above) — right now encoder failures are invisible until the restart watchdog fires.
- **Volume / pause passthrough** in the TUI (pychromecast supports both; trivial win for a "watching TV" tool).
- **Exit summary:** on Ctrl+C print a one-shot report (duration, frames dropped, restarts, final drift) so problems are visible without having run `--stats`.

### Medium term (cost + install friction)
- **Prebuilt AudioTee** (see 3.5) — removes the Swift requirement, the biggest install cliff.
- **Publish properly:** a Homebrew tap or `uv tool install fix-casting` from PyPI; the git-clone + install.sh flow is fine for you but is the ceiling on anyone else using it. Consider renaming the CLI or making it configurable — `cast` collides with Foundry's widely-installed `cast` binary.
- **CPU reduction exploration:** CDP JPEG screencast + MJPEG decode + H.264 re-encode is the dominant CPU cost. Two candidate paths worth a spike each: (a) `getDisplayMedia` + `MediaRecorder`/WebCodecs inside a companion page so Chrome's own hardware encoder produces H.264 and Python only muxes; (b) capture at 0.75× and upscale in the encoder at low motion cost. Either could halve CPU and heat (relevant to the suspected VideoToolbox thermal stalls).

### Longer term (bigger bets)
- **Codec/latency upgrades:** HEVC or AV1 for Google TV devices (half the bitrate at same quality, helps the network ceiling documented in the README); LL-HLS or fMP4 segments to shrink the unbuffered-mode latency.
- **Cross-platform audio:** the video path is already portable; per-app audio capture exists on Windows (WASAPI process loopback) and Linux (PipeWire) if the tool should ever leave macOS.
- **Local file / playlist casting** (skip the browser entirely when the input is a file or direct stream URL) — the streamer/caster halves already support it.

---

## 6. Suggested order of work

| # | Item | Size | Status |
|---|------|------|--------|
| 1 | ffmpeg stderr drain thread + surface in stats (P0) | S | ✅ `9c48afb` |
| 2 | Fix `shutdown()`/exit-code handling (P0) | S | ✅ `21220b5` |
| 3 | Temp-dir cleanup: Chrome profile + per-run HLS work dir (P1) | S | ✅ `e673e51` |
| 4 | Delete dead code/knobs (3.3), fix help-text drift, drop `requirements.txt` | S | ✅ `697cfe8` |
| 5 | Extract `CastSession`; de-duplicate tools harness setup (3.1) | M | ✅ `494d2f3` |
| 6 | Split `streamer.py` into encoder/pacing/server (3.2) | M | ✅ `3e242e6` |
| 7 | ruff + mypy + pytest unit tests + CI; pipeline-skew test on macOS runner (§4) | M | ✅ |
| 8 | `--device` flag, TV auto-reconnect, exit summary (§5 near-term) | M | ✅ |
| 9 | Prebuilt AudioTee release + install.sh download (3.5) | M | ✅ (tag `audiotee-v1` to publish) |
| 10 | CPU-reduction spike: Chrome-side encoding via MediaRecorder/WebCodecs | L | |

Items 1–4 are a day of work combined and remove the worst operational risks; 5–7 make the codebase safe to keep evolving; 8–10 are where the product gets meaningfully better for users other than its author.
