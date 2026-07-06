# Review log — fable-refactor loop

Running notes from the automated review/implement loop. Newest entry first.
Roadmap: [codebase-review.md](codebase-review.md) §6.

## 2026-07-05 — Iteration 3: roadmap item 3 (temp-dir cleanup)

**Reviewed:** iteration 2 (`21220b5`, exit codes). Clean: error path returns 1
(verified last iteration), signal path exits 0 explicitly, `SystemExit` is not
swallowed by `except Exception`. The Textual/Ctrl+C interaction inside `--tui`
is pre-existing and untouched. No bugs.

**Implemented — roadmap item 3: temp-dir cleanup (P1).**

- `browser.py`: the whole post-launch flow now sits in `try/finally
  context.close()` (previously only the screencast was guarded — a `goto`
  failure leaked a running Chrome), and the browser thread removes its
  `cast-tab-chrome-*` mkdtemp profile dir on exit. Split `_run` /
  `_run_browser` so cleanup wraps all paths, including launch failures.
- `streamer.py`: the HLS work dir is now a unique `cast-tab-stream-*` mkdtemp
  per run instead of the fixed `/tmp/cast-tab-stream` (two simultaneous casts
  previously served each other's segments). Removed on `stop()` only when we
  created it — an explicit `work_dir` (tools harnesses) is the caller's.
- README updated (no more fixed-path mention).

**Verified:** streamer dirs are unique, owned dirs removed on stop, explicit
dirs preserved; real headless Chrome run against about:blank confirms the
profile dir is gone after `stop()`; 15 s pipeline-harness smoke run passes
through the changed streamer (short-run flash/beep pairing limitation is the
known one from iteration 1).

**Note:** pre-existing `cast-tab-chrome-*` dirs from older runs still sit in
$TMPDIR; left alone (user data, OS clears them periodically).

**Next up:** roadmap item 4 — delete dead code/knobs, fix `--adblock` help-text
drift, drop `requirements.txt`.

## 2026-07-05 — Iteration 2: roadmap item 2 (shutdown / exit codes)

**Reviewed:** iteration 1 (`9c48afb`, ffmpeg stderr drain). Holds up: deque appends
are atomic, one drain thread per ffmpeg instance exiting on EOF, early-exit path
guarded. One nuance found, not fix-worthy: `_ffmpeg_stderr_tail` is not cleared on
relaunch, so an early-exit report could in principle include lines from a previous
ffmpeg instance. Harmless today (`wait_until_ready` runs once, at first startup);
worth folding into the encoder split (roadmap item 6) as a per-instance tail.

**Implemented — roadmap item 2: fix `shutdown()` / exit-code handling (P0).**

- `cli.py`: `shutdown()` no longer calls `sys.exit(0)` — it only stops components
  (idempotent). The embedded exit was raising `SystemExit(0)` from inside the
  `except Exception` handler before its `return 1`, so **failures exited 0**.
- Signals (SIGINT/SIGTERM) go through a new `handle_signal` wrapper that does
  `shutdown(); sys.exit(0)` — an intentional stop is still a success exit.
- Error path (`except Exception`) and TUI path now control their own exit codes.

**Verified:** stub harness (fake device/screencaster raising during startup):
`main()` returns 1 and teardown still runs. Before the fix the same harness
died with exit code 0.

**Next up:** roadmap item 3 — temp-dir cleanup (Chrome profile dirs + per-run
HLS work dir).

## 2026-07-05 — Iteration 1: roadmap item 1 (ffmpeg stderr drain)

**Reviewed:** `docs/codebase-review.md` (the previous deliverable). No implementation
existed yet to review; the bugs it lists are all tracked roadmap items (known gaps),
so per loop policy I implemented the top item instead of ad-hoc fixing.

**Implemented — roadmap item 1: ffmpeg stderr drain + surface in stats (P0).**

- `streamer.py`: each ffmpeg (re)launch now starts a daemon thread that drains
  stderr into a bounded 50-line tail (`_ffmpeg_stderr_tail`). Previously stderr
  was piped and never read during a cast, so a chatty-error run could fill the
  ~64KB pipe and block ffmpeg — an invisible full-pipeline stall.
- `wait_until_ready()` now reports early-exit errors from the drained tail
  instead of reading the pipe directly (also fixed its misplaced docstring,
  which sat after the first statement and was inert).
- `stats.py`: new `record_ffmpeg_stderr()`; snapshot fields `ffmpeg_errors` /
  `ffmpeg_last_error`; `--stats` prints an `ffmpeg` line when errors occurred.
- `tui.py`: new "ffmpeg errors" card in section ② (yellow when non-zero).
- Without `--stats`/`--tui`, drained lines print as `[ffmpeg] <line>` so errors
  are never silently discarded.

**Verified:**

- Garbage-MJPEG feed through the real `HLSStreamer`: 13 stderr lines captured,
  surfaced in the stats snapshot and the tail; early-exit report works.
- `tools/test_pipeline_skew.py --seconds 30` (real production path, no Chrome):
  queue depth steady at 1, no drops, A/V offset median −67 ms with 33 ms spread —
  within the harness's frame-quantization noise floor at 30 fps (±1–2 frames),
  matching the live queue-depth estimate (~33 ms). No regression.

**Observations for future iterations (not bugs introduced here):**

- ffmpeg only flushes many decode-path errors at EOF/exit, so mid-run counts can
  read 0 until a restart; the drain still prevents the pipe-fill stall either way.
- The 15 s harness run can detect flashes/beeps but fail to pair them
  ("Could not pair flash/beep events") — use ≥30 s runs; worth a guard in the
  tool when it becomes a pytest (roadmap item 7).

**Next up:** roadmap item 2 — fix `shutdown()` / exit-code handling in `cli.py`.
