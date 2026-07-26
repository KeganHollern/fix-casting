# Review log — fable-refactor loop

Running notes from the automated review/implement loop. Newest entry first.
Roadmap: [codebase-review.md](codebase-review.md) §6.

## 2026-07-06 — Full-codebase review fixes (post-PR#2 review)

An 8-angle multi-agent review of the whole branch confirmed 12 correctness
bugs and 12 cleanup findings (1 candidate refuted empirically). Fixed in this
pass — chosen triage: everything that can hang, orphan, crash a thread, or
silently degrade quality; consolidation over point-patches; 2 one-liners.

**Shutdown/threading correctness:**
- Ctrl+C stats deadlock: `PipelineStats` lock → RLock, and the exit summary
  reads a new `totals()` accessor instead of a resetting `snapshot()`.
- Writer-lock deadlock: the writer now snapshots the ffmpeg instance under
  the lock but writes **outside** it, with an instance-identity check before
  restarting (a relaunch mid-write no longer gets its fresh ffmpeg killed).
- `FfmpegProcess.kill()`: terminate-first (killing the reader is what
  unblocks a wedged writer; closing the buffered stdin first can block on
  the writer's own buffer lock), with `graceful=True` EOF-close used by
  normal `stop()` after the writer has joined — preserving the EOF error
  flush the stderr-drain test caught me regressing. Post-SIGKILL wait
  guarded. Verified live both ways with a wedged `sleep` child.
- Relaunch-after-stop race: `_relaunch_ffmpeg` re-checks `_stopped` under
  the lock — no more orphan ffmpeg spawned into a deleted work dir.
- `TabCaster`: all control/status methods snapshot `self._chromecast` into
  a local (stop() nulling it mid-call can no longer AttributeError a
  daemon thread); `stop()` nulls before disconnecting.
- TUI poller: `call_from_thread` wrapped — a quit mid-iteration exits the
  poller instead of tracebacking over the restored terminal.
- Browser: profile dir removed only when Chrome is known dead
  (`context.close()` succeeded or launch failed) — no rmtree under a
  possibly-live Chrome.
- Encoder probe: hand memo replaces `lru_cache` so a transient probe
  failure is re-probed instead of pinning libx264 for the whole cast.

**Consolidation (fixes 4 findings at once):** the TV re-cast watchdog now
lives in `TabCaster.start_watchdog()` (grace 15s, 5s cadence, background
thread, stopped by `caster.stop()`); the CLI's blocking inline check and the
TUI's per-poll thread-spawner are both gone. `FfmpegProcess` gained `pid`,
un-breaking `tools/test_pipeline_skew.py --inject-stall`.

**One-liners:** `--audio-offset-ms` help no longer promises negative offsets;
`SessionConfig.jpeg_quality` uses `DEFAULT_JPEG_QUALITY`.

**Verified:** ruff + mypy clean; 43 tests (4 new: watchdog recovery/grace/
stop-safety, probe-cache); both kill orderings exercised against a live
wedged writer; bad `--device` exits 1 with a one-line error; full slow suite
incl. the 30 s A/V-sync regression passes after the writer-lock restructure.

**Deferred (logged, not merge blockers):** stderr-drain and AudioTee-JSON
parse dedup, install.sh path unification with `AUDIOTEE_CANDIDATES`, volume
keypress coalescing, unused `port` param, `CAST_TEST_WRITE_DELAY_MS`,
`__all__` drift, status-poll idiom dedup in TabCaster.

## 2026-07-05 — Iteration 12: roadmap item 10 scoped (CPU spike plan)

**Reviewed:** iteration 11 (`4ff5c78`). The priority space binding means space
never presses a focused button (enter/click still do) — intended; the lockfile
is a universal resolution so CI's interpreter choice is safe. No bugs.

**Implemented — roadmap item 10, as a scoped plan (the spike itself is L-size
and belongs in a dedicated session): `docs/cpu-spike-plan.md`.**

- **Measured the baseline first** (40 s live run, steady-state `ps` samples):
  ffmpeg ~170–210% CPU (peaks 350%) doing the *software MJPEG decode* — the
  H.264 encode itself is VideoToolbox hardware and nearly free; Chrome 15–50%
  (JPEG encode); Python 6–20%; AudioTee ~0%. The JPEG round-trip is the
  dominant cost, which sharpens the spike's thesis considerably.
- Plan covers: primary approach (getDisplayMedia + MediaRecorder H.264,
  ffmpeg becomes mux-only, est. ~250% → ~60–80%), WebCodecs fallback, a cheap
  orthogonal `--capture-scale` option (~44% off the dominant stage, no
  sync-model change), and the tuning-only stopgap. Biggest named risk: path A
  replaces the arrival-time A/V sync model this repo spent weeks perfecting —
  gated on the existing skew harness plus a 30-min drift check per the
  established methodology (track durations, non-looping source).
- Five ordered go/no-go gates so the spike fails fast and cheap.

**Verified:** the measurement run stayed synced (+40 ms median) — also a free
confirmation that the branch's pipeline is healthy end-to-end.

**Roadmap state: all 10 items now done or scoped.** The loop has drained its
backlog; future iterations will do upkeep (review, small fixes) unless
redirected.

## 2026-07-05 — Iteration 11: uv.lock + Python floor fix (+ space-key bug)

**Reviewed:** iteration 10 (`79c1015`, TV controls). Found one real bug, not a
known gap → fixed per loop policy: after clicking a knob Button it keeps focus,
and a focused Textual Button consumes `space` to press itself — so `space`
re-nudged the audio offset instead of pausing. Fix: the space binding is now
`Binding(..., priority=True)`. Verified with a pilot test that focuses the
`+10` button, presses space, and asserts the TV paused and the offset didn't
move. Also noted (accepted): `_recovering` is an unlocked bool — a worst-case
race double-spawns one recovery thread, harmless.

**Implemented — reproducible installs (`uv.lock`).**

- `uv lock` immediately surfaced a real packaging lie: `pychromecast>=14`
  requires Python ≥3.11, so our declared `>=3.10` was never installable on
  3.10. Floor bumped honestly: `requires-python >=3.11`, ruff `py311`, mypy
  `3.11`, README requirements row.
- `uv.lock` committed (35 packages); CI now installs with
  `uv sync --frozen --group dev` in both jobs, so PRs fail loudly when the
  lock is stale instead of silently resolving something new.

**Verified:** `uv sync --frozen --dry-run` resolves cleanly against the lock;
ruff + mypy clean; 35 unit tests pass; space-key pilot regression test passes.

**Next up:** scope roadmap item 10 (Chrome-side encoding spike) as a written
design note, or non-TUI volume keys — else the loop's roadmap is drained.

## 2026-07-05 — Iteration 10: TV controls in the TUI + reconnect-freeze fix

**Reviewed:** iteration 9 (`08357f5`, prebuilt AudioTee). Installer edges hold:
`curl -f` prevents 404 bodies landing on disk, the Mach-O check catches wrong
content, curl doesn't quarantine-flag so the binary runs, re-tagging fails
loudly at `gh release create`. No bugs.

**Implemented — §5 near-term leftovers: TV playback controls + TUI polish.**

- `TabCaster`: `toggle_pause()` (PAUSED is already exempt from the watchdog;
  a pause longer than the HLS window resumes as a jump to live via re-cast),
  `volume_step(delta)`, `toggle_mute()` — all None-safe when disconnected.
- TUI keys: `space` pause/resume, `,`/`.` volume ±5%, `m` mute. All network
  calls run on worker threads (never the UI thread); results flash in the
  knob-status line ("TV: volume 55%"). Footer + README key table updated.
- **Fixed iteration 8's logged gap:** the re-cast watchdog now runs on its own
  guarded thread in the TUI (`_ensure_playing_bg`), so a dead-TV recovery
  attempt (~30 s) no longer freezes the metrics poller.

**Verified:** ruff + mypy clean; 35 unit tests (4 new: pause/resume transitions
with call counts, idle/disconnected no-ops, volume step + device clamp, mute
flip); binding sanity check (every BINDINGS entry resolves to an action); and a
**headless Textual pilot run** pressing `space , . m` against a fake caster —
all four controls fired, confirming the key names.

**Next up:** remaining small items: commit a `uv.lock`, consider volume keys in
the plain (non-TUI) CLI, or start scoping roadmap item 10 (CPU spike) as notes.

## 2026-07-05 — Iteration 9: roadmap item 9 (prebuilt AudioTee)

**Reviewed:** iteration 8 (`d48e2d8`). Watchdog failure paths hold: a failed
re-cast resets the counter (retry ~10 s later); the exit summary reads only
cumulative totals; duplicate device names fall through to the ambiguity error.
The TUI-poller-freeze-during-recovery note stands as a known cosmetic gap. No
new bugs.

**Implemented — roadmap item 9: prebuilt AudioTee + install.sh download (M).**

Publishing a GitHub release is outward-facing, so it stays user-triggered:

- `.github/workflows/release-audiotee.yml` — on pushing an `audiotee-v*` tag,
  builds the vendored fork (stereo-mixdown patch included) on a macOS arm64
  runner and attaches `audiotee-macos-<arch>` + SHA-256 checksums to a GitHub
  release. **To publish: `git tag audiotee-v1 && git push origin audiotee-v1`.**
- `install.sh` resolution order: existing binary → prebuilt download from
  `releases/latest/download/audiotee-macos-$(uname -m)` (verified as Mach-O
  before install, partial files cleaned up; URL overridable via
  `AUDIOTEE_RELEASE_URL`) → `swift build` fallback → actionable warning when
  neither is possible. Downloads land in `bin/` (already first in
  `AUDIOTEE_CANDIDATES`, already gitignored).
- README: Swift is now "only when no prebuilt is available"; maintainer tag
  instructions included.

**Verified:** `bash -n`; the AudioTee section exercised standalone — happy
download via a `file://` Mach-O (installed to `bin/audiotee`, executable),
failed download + swift-less PATH shim (graceful warning, no partial file
left); full `./install.sh` run is idempotent (section skipped, binaries
already present); both workflow YAMLs parse.

**Note:** until the user pushes the `audiotee-v1` tag, the download 404s and
installs behave exactly as before (source build) — no regression window.

**Next up:** roadmap item 10 (CPU-reduction spike) is an L-size research task —
better suited to a deliberate session than this loop. Remaining smaller
candidates: `uv.lock` for reproducible installs, TUI reconnect-freeze polish,
volume/pause passthrough (§5 near-term leftovers).

## 2026-07-05 — Iteration 8: roadmap item 8 (--device, TV watchdog, summary)

**Reviewed:** iteration 7 (`973377b`, tooling/tests/CI). CI mechanics re-checked:
`-m slow` on the CLI overrides the addopts deselection, slow tests need no
display on the macOS runner, and the pipeline test uses `sys.executable` so it
runs under the CI venv. No bugs.

**Implemented — roadmap item 8: product polish tier 1 (M).**

- `--device NAME` (`devices.find_device`): case-insensitive exact match first,
  then unique-substring; ambiguous/no-match errors list the discovered names.
  Also unblocks non-TTY scripting (the picker's `input()` can't run there).
- **TV watchdog** (`caster.ensure_playing`): after two consecutive idle polls
  (state not PLAYING/BUFFERING/PAUSED), re-issues `play_media` with the stored
  playlist URL. Wired into the CLI main loop (every 5 s after a 15 s startup
  grace → recovers in ~10 s, printed as `[recover] …`) and the TUI poll loop.
  Transport drops are left to pychromecast's own socket reconnect; status
  exceptions are swallowed while it does.
- **Exit summary** on stop: duration, TV re-casts, frames dropped, ffmpeg
  restarts (stats-dependent parts only with --stats/--tui).
- README: --device documented, watchdog + summary noted.

**Verified:** ruff + mypy clean; 31 unit tests (11 new: name matching incl.
exact-beats-substring and ambiguity errors; watchdog counter reset, re-cast
after threshold with FINISHED reason surfaced, BUFFERING/PAUSED not idle,
pre-connect no-ops, status-exception swallow). CLI smoke: `--device living`
matches "Living Room TV", error path exits 1, summary prints.

**Note:** the watchdog's `play_hls` blocks its caller up to ~30 s if the TV is
truly gone (`block_until_active` + verify loop). In the CLI loop that's fine;
in the TUI it runs on the poller thread, freezing metric refresh during a
dead-TV recovery attempt. Cosmetic; revisit if it annoys.

**Next up:** roadmap item 9 — prebuilt AudioTee release + install.sh download.

## 2026-07-05 — Iteration 7: roadmap item 7 (tests, lint, types, CI)

**Reviewed:** iteration 6 (`3e242e6`, streamer split). Strongest possible check
already ran (identical −67 ms baseline). One drift found and fixed this
iteration: the banner now derives ~48 s but README/`--buffered` help still said
~45 s — updated all four spots to ~48 s. No functional bugs.

**Implemented — roadmap item 7: ruff + mypy + pytest + CI (M).**

- `pyproject.toml`: dev dependency group (pytest/ruff/mypy), ruff (line 100,
  isort), mypy (`check_untyped_defs`, stub-less libs ignored), pytest config —
  slow tests deselected by default, opt in with `-m slow`.
- Ruff found 2 issues (unsorted imports, unused `pychromecast` import) — fixed.
- Mypy found 6 real ones — all fixed: the sloppy `audiotee_path()` return (a
  roadmap P3), untyped `_apply_timer`, sync `action_quit` overriding textual's
  async one, and three Optional-flow gaps in `cli.py` now pinned with asserts.
- `tests/` (24 tests): encoder arg tables incl. a consistency test that the
  advertised TV delay == hls_time × list_size; pacing (queue bound drops
  oldest, get/stop semantics); AudioTee metadata parsing; adblock rule
  filtering (exceptions/element-hiding/scoped rules skipped) with `_cached_list`
  stubbed; stats snapshot/reset + drift math. Slow (`-m slow`): the garbage-
  MJPEG stderr-drain regression, work-dir lifecycle, and the **A/V-sync
  regression** — wraps `tools/test_pipeline_skew.py --seconds 30` and asserts
  the median flash/beep offset within ±133 ms (4 frames; guards the historical
  ~700 ms-class skews, not harness noise).
- `.github/workflows/ci.yml`: ubuntu job (ruff, mypy, unit tests), macOS job
  (brew ffmpeg + `pytest -m slow`) since VideoToolbox is the production path.

**Verified locally:** ruff clean, mypy clean (14 files), 20 unit tests pass in
0.1 s, 4 slow tests pass in 42 s (A/V offset within tolerance). No `uv.lock`
committed yet — CI resolves fresh; consider locking later for reproducibility.

**Next up:** roadmap item 8 — `--device` flag, TV auto-reconnect, exit summary.

## 2026-07-05 — Iteration 6: roadmap item 6 (streamer split)

**Reviewed:** iteration 5 (`494d2f3`, CastSession). Failure paths traced: signal
during `start()` unwinds cleanly, `stop()` is safe on partial init,
`require_audio` preserves both callers' semantics; the real 30 s harness run
validated sync. No bugs.

**Implemented — roadmap item 6: split `streamer.py` (M).**

- `encoder.py` — encoder detection (now `lru_cache`d: one subprocess probe per
  run instead of two per ffmpeg launch), bitrate/GOP tables, HLS args with the
  segment×list constants factored out, `tv_delay_s()` so the CLI banner derives
  "~48s TV delay" instead of asserting "~45s", and `FfmpegProcess` — spawn +
  kill + a **per-instance** stderr tail/drain (closes iteration 2's stale-tail
  note).
- `pacing.py` — `LatestFrame` + `BoundedFrameQueue` (put/get/clear/wake_all);
  the "depth IS the audio lead" analysis moved onto the queue's docstring.
- `server.py` — `get_local_ip` + `HLSHTTPServer`.
- `streamer.py` (789 → ~490 lines) keeps `HLSStreamer` as the orchestrator:
  sampler/writer threads, backpressure watchdog, audio-fd args/drain, and the
  A/V anchor ordering stay put, delegating to the new modules. Public imports
  (`DEFAULT_JPEG_QUALITY`, `codec_label`, `default_fps_for_resolution`)
  re-exported so cli/session/tools are untouched apart from the banner.

**Verified:** compile; garbage-MJPEG drain regression through `FfmpegProcess`
(13 lines recorded in stats); per-instance tail confirmed on a bad-flag spawn;
cli stub error path returns 1; full 30 s pipeline harness: **−67 ms median,
33 ms spread — identical to the pre-refactor baseline from iteration 1**, queue
depth 1, no drops.

**Next up:** roadmap item 7 — ruff + mypy + pytest unit tests + CI.

## 2026-07-05 — Iteration 5: roadmap item 5 (CastSession extraction)

**Reviewed:** iteration 4 (`697cfe8`, dead code). The fps collapse preserves
behavior — the `behind` threshold used `pace_fps` (30) before and `fps` (30)
now at both call sites; nothing imports the removed names. No bugs.

**Implemented — roadmap item 5: extract `CastSession` (M).**

- New `cast_tab/session.py`: `SessionConfig` (one dataclass instead of 12
  kwargs) + `CastSession` owning the browser → audio tap → streamer lifecycle.
  `start()` blocks until the HLS stream is ready; `stop()` is idempotent and
  handles partial init. The AudioTee stderr filter/noise-suppression closure
  moved here from `cli.py`. New `require_audio` flag: the CLI degrades to
  video-only, the measurement harness hard-fails (its old behavior).
- `cli.py` `main()` shrinks ~130 lines: parse → discover → `session.start()`
  → banner → cast → stats/TUI loop. Banner now prints after the stream is
  ready (it used to print before `streamer.start()`); message text unchanged.
- `tools/measure_source_skew.py` drops its hand-rolled duplicate of the same
  wiring (~45 lines) and uses the session — it now also gets the audio-stderr
  warning surfacing it previously lacked.

**Verified:** stub test — cli through session error path returns 1,
`session.stop()` handles partial init; full real 30 s run of
`measure_source_skew.py` through the session (visible Chrome, AudioTee
attached, HLS archived): median offset +50 ms / spread 53 ms — within the
±1–2-frame noise floor at 30 fps, queue depth 1, in sync ~33 ms.

**Next up:** roadmap item 6 — split `streamer.py` into encoder / pacing /
server (fold the per-instance stderr tail from iteration 2's note into it).

## 2026-07-05 — Iteration 4: roadmap item 4 (dead code / doc drift)

**Reviewed:** iteration 3 (`e673e51`, temp-dir cleanup). Teardown ordering is
sound: sampler/writer joined → ffmpeg killed (waited) → HTTP down → rmtree, so
nothing writes into a removed dir; the browser rmtrees only after
`context.close()` returns. Rare edge (close() raising with Chrome alive →
rmtree under a live process) accepted — close() raising implies the browser is
already gone. No bugs.

**Implemented — roadmap item 4: delete dead code/knobs, fix doc drift.**

- Removed the inert capture-oversampling knob: `TabScreencaster` collapses
  `fps`/`pace_fps` into one `fps` (capture is paint-driven; the old capture
  `fps` was stored and never used). `cli.py` drops the `1.5×` computation;
  `tools/measure_source_skew.py` updated to the new signature.
- `stats.py`: removed never-called `record_capture_timeout` (+ its always-zero
  `timeouts` report field) and write-only `record_publish`/`_publish`.
- `default_jpeg_quality(w, h)` (ignored its args) → `DEFAULT_JPEG_QUALITY`.
- `--adblock` help text and `build_block_patterns` docstring said "+ EasyList";
  the implementation deliberately uses uBO network lists + Peter Lowe's. Fixed.
- Dropped `requirements.txt` (duplicated `pyproject.toml`, would drift) and the
  stale `bin/cast` venv shim (superseded by `uv tool install`).

**Verified:** all modules + tools compile; stats report renders with the
trimmed fields (incl. the new ffmpeg line); cli stub confirms `fps=encode_fps`
passes through and the error path still returns 1; 15 s pipeline-harness smoke
run unchanged (queue peak 1, in sync ~33 ms).

**Next up:** roadmap item 5 — extract `CastSession`, de-duplicate the tools
harness setup.

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
