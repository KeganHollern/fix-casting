"""Textual dashboard for the cast pipeline (enabled with --tui).

Renders the live PipelineStats broken out by pipeline segment — incoming capture
(CDP screencast + AudioTee), the internal encode pipeline, the outgoing HLS
stream, Chromecast playback, and A/V sync — each metric shown as a number, a
sparkline of its recent history, and a one-line description. Includes a live
audio-offset knob that re-applies the A/V delay without restarting the cast.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Label, Sparkline, Static

from cast_tab.stats import PipelineStats, StatsSnapshot


class MetricCard(Vertical):
    """A single metric: title, big value, sparkline of recent history, blurb."""

    def __init__(
        self,
        card_id: str,
        title: str,
        description: str,
        *,
        show_spark: bool = True,
        history: int = 60,
    ) -> None:
        super().__init__(id=f"card-{card_id}", classes="metric-card")
        self._title = title
        self._description = description
        self._show_spark = show_spark
        self._series: deque[float] = deque(maxlen=history)
        self._value = Label("—", classes="metric-value")
        self._spark = Sparkline([0.0], summary_function=max, classes="metric-spark")

    def compose(self) -> ComposeResult:
        yield Label(self._title, classes="metric-title")
        yield self._value
        if self._show_spark:
            yield self._spark
        yield Label(self._description, classes="metric-desc")

    def set(
        self,
        value_text: str,
        sample: float | None = None,
        *,
        level: str = "ok",
    ) -> None:
        """Update the value (level: 'ok' | 'warn' | 'bad' colors it) + sparkline."""
        self._value.update(value_text)
        self._value.set_class(level == "warn", "warn")
        self._value.set_class(level == "bad", "bad")
        if self._show_spark and sample is not None:
            self._series.append(float(sample))
            self._spark.data = list(self._series) or [0.0]


class Section(Vertical):
    """A titled row of metric cards for one pipeline segment."""

    def __init__(self, title: str, cards: list[MetricCard]) -> None:
        super().__init__(classes="section")
        self.border_title = title
        self._cards = cards

    def compose(self) -> ComposeResult:
        yield Horizontal(*self._cards, classes="section-body")


class CastTUI(App):
    """Live dashboard for a running cast."""

    CSS = """
    Screen { background: $surface; }
    .section {
        border: round $primary;
        height: auto;
        margin: 0 1;
        padding: 0 1;
    }
    .section-body { height: auto; }
    .metric-card {
        width: 1fr;
        height: auto;
        padding: 0 1;
        border-left: tall $panel;
    }
    .metric-title { color: $text-muted; text-style: bold; }
    .metric-value { color: $text; text-style: bold; }
    .metric-value.warn { color: $warning; }
    .metric-value.bad { color: $error; }
    .metric-spark { height: 3; color: $success; margin: 0; }
    .metric-desc { color: $text-disabled; }
    #status-bar {
        height: 1; padding: 0 2; background: $panel; color: $text;
        content-align: left middle;
    }
    #knob-row { height: auto; padding: 1 1 0 1; align-horizontal: left; }
    #knob-row Button { min-width: 8; margin: 0 1 0 0; }
    #knob-value { width: auto; min-width: 22; content-align: left middle; text-style: bold; }
    #knob-status { color: $text-disabled; content-align: left middle; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("right_square_bracket", "offset(10)", "audio +10ms"),
        ("left_square_bracket", "offset(-10)", "audio -10ms"),
        ("right_curly_bracket", "offset(100)", "audio +100ms"),
        ("left_curly_bracket", "offset(-100)", "audio -100ms"),
        ("r", "offset_reset", "audio 0"),
    ]

    def __init__(
        self,
        *,
        stats: PipelineStats,
        streamer,
        caster,
        initial_offset_ms: int = 0,
        tv_poll_interval: float = 2.0,
        refresh_s: float = 1.0,
        playlist_url: str = "",
    ) -> None:
        super().__init__()
        self._stats = stats
        self._streamer = streamer
        self._caster = caster
        self._tv_poll_interval = tv_poll_interval
        self._refresh_s = refresh_s
        self._playlist_url = playlist_url

        # Audio-offset knob state. Button presses move _pending immediately; a
        # debounce timer applies it (one ffmpeg relaunch) after presses settle.
        self._offset_applied = max(0, int(initial_offset_ms))
        self._offset_pending = self._offset_applied
        self._apply_timer = None
        self._applying = False

        self._poller_stop = threading.Event()
        self._poller: threading.Thread | None = None
        self._last_poll = 0.0
        self._tv_last_poll = 0.0

    # --- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status-bar")
        with VerticalScroll():
            yield Section(
                "①  Capture — CDP screencast + AudioTee (incoming)",
                [
                    MetricCard("cap_fps", "Capture FPS",
                               "Frames/s the tab is producing via CDP screencast."),
                    MetricCard("cap_lag", "Chrome→app lag",
                               "Frame staleness on arrival (Chrome compositor → us)."),
                    MetricCard("cap_decode", "Decode time",
                               "Per-frame JPEG handling time in the capture loop."),
                    MetricCard("aud_backlog", "Audio pipe backlog",
                               "Unread AudioTee PCM; ~0 = ffmpeg reads it in time."),
                    MetricCard("aud_warn", "Audio warnings",
                               "AudioTee under/overruns reported this interval.",
                               show_spark=False),
                ],
            )
            yield Section(
                "②  Encode pipeline — sampler → queue → ffmpeg (internal)",
                [
                    MetricCard("enc_fps", "Encode FPS",
                               "Frames/s fed into ffmpeg (target in denominator)."),
                    MetricCard("frame_age", "Frame age",
                               "Age of frames when sampled (publish → sample)."),
                    MetricCard("queue", "Queue peak",
                               "Frames buffered before ffmpeg; spikes = stall absorbed."),
                    MetricCard("write", "stdin write",
                               "Time blocked writing to ffmpeg; high = backpressure."),
                    MetricCard("repeats", "Repeats / resync",
                               "Held frames (no new capture) / clock resyncs.",
                               show_spark=False),
                ],
            )
            yield Section(
                "③  HLS stream (outgoing)",
                [
                    MetricCard("hls_segs", "Segments",
                               "Live HLS .ts segments currently on disk.",
                               show_spark=False),
                    MetricCard("hls_age", "Newest segment age",
                               "Age of the latest segment; rising = falling behind."),
                    MetricCard("hls_del", "Deleted",
                               "Segments rotated out this interval.",
                               show_spark=False),
                ],
            )
            yield Section(
                "④  TV / Chromecast (playback)",
                [
                    MetricCard("tv_state", "State",
                               "Chromecast player state.", show_spark=False),
                    MetricCard("tv_pos", "Position",
                               "Reported playback position on the TV.",
                               show_spark=False),
                    MetricCard("tv_delta", "Advance vs wall",
                               "Playback advance per interval; < interval = buffering."),
                    MetricCard("tv_stall", "Micro-stalls",
                               "Accumulated brief playback stalls.",
                               show_spark=False),
                    MetricCard("tv_nonplay", "Non-playing",
                               "Polls not in PLAYING (buffering/idle).",
                               show_spark=False),
                ],
            )
            yield Section(
                "⑤  A/V sync (cumulative)",
                [
                    MetricCard("drift", "Est. audio lead",
                               "Cumulative A/V drift from dropped frames.",
                               show_spark=False),
                    MetricCard("dropped", "Frames dropped",
                               "Total frames dropped since start (cause of drift).",
                               show_spark=False),
                    MetricCard("restarts", "ffmpeg restarts",
                               "Relaunches (each glitches + re-anchors sync).",
                               show_spark=False),
                ],
            )
            with Horizontal(id="knob-row"):
                yield Label("", id="knob-value")
                yield Button("-100", id="off_m100")
                yield Button("-10", id="off_m10")
                yield Button("+10", id="off_p10")
                yield Button("+100", id="off_p100")
                yield Label("", id="knob-status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "fix-casting"
        self.sub_title = self._playlist_url or "live cast"
        self._refresh_knob()
        # Poll the pipeline off the UI thread (TV status is a network call).
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()

    def on_unmount(self) -> None:
        self._poller_stop.set()

    # --- polling ----------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._poller_stop.wait(self._refresh_s):
            try:
                self._streamer.poll_audio_backlog()
                self._streamer.poll_hls_stats()
                now = time.monotonic()
                if now - self._tv_last_poll >= self._tv_poll_interval:
                    self._tv_last_poll = now
                    tv = self._caster.poll_playback_stats()
                    self._stats.record_tv_poll(
                        state=tv.state,
                        position_s=tv.position_s,
                        idle_reason=tv.idle_reason,
                    )
                snap = self._stats.snapshot(self._refresh_s)
            except Exception:
                continue
            self.call_from_thread(self._render, snap)

    @staticmethod
    def _lvl(warn: bool, bad: bool = False) -> str:
        return "bad" if bad else ("warn" if warn else "ok")

    def _render(self, s: StatsSnapshot) -> None:
        def card(cid: str) -> MetricCard:
            return self.query_one(f"#card-{cid}", MetricCard)

        lvl = self._lvl

        # ① Capture
        cap_lo = s.capture_fps < s.target_fps * 0.9
        card("cap_fps").set(f"{s.capture_fps:.1f} / {s.target_fps:.0f}", s.capture_fps,
                            level=lvl(cap_lo, s.capture_fps < s.target_fps * 0.5))
        if s.screencast_lag_count:
            card("cap_lag").set(f"{s.screencast_lag_ms:.0f} ms  (peak {s.screencast_lag_peak_ms:.0f})",
                                s.screencast_lag_ms,
                                level=lvl(s.screencast_lag_ms > 150, s.screencast_lag_ms > 400))
        else:
            card("cap_lag").set("—", 0.0)
        card("cap_decode").set(f"{s.capture_ms:.0f} ms  (peak {s.capture_peak_ms:.0f})", s.capture_ms)
        if s.audio_backlog_ms is not None:
            card("aud_backlog").set(f"{s.audio_backlog_ms:.0f} ms  (peak {s.audio_backlog_peak_ms:.0f})",
                                    s.audio_backlog_ms,
                                    level=lvl(s.audio_backlog_ms > 250, s.audio_backlog_ms > 500))
        else:
            card("aud_backlog").set("no audio", 0.0)
        card("aud_warn").set(str(s.audio_warnings), level=lvl(s.audio_warnings > 0))

        # ② Encode pipeline
        enc_lo = s.encode_fps < s.target_fps * 0.9
        card("enc_fps").set(f"{s.encode_fps:.1f} / {s.target_fps:.0f}", s.encode_fps,
                            level=lvl(enc_lo, s.encode_fps < s.target_fps * 0.5))
        card("frame_age").set(f"{s.frame_age_ms:.0f} ms  (peak {s.frame_age_peak_ms:.0f})", s.frame_age_ms)
        card("queue").set(f"{s.queue_peak}", float(s.queue_peak),
                          level=lvl(s.queue_peak >= s.target_fps, s.queue_dropped > 0))
        card("write").set(f"{s.write_ms:.1f} ms  (peak {s.write_peak_ms:.1f})", s.write_ms,
                          level=lvl(s.write_peak_ms > 250, s.write_peak_ms > 1000))
        card("repeats").set(f"{s.repeats} / {s.resyncs}", level=lvl(s.resyncs > 0))

        # ③ HLS
        card("hls_segs").set(str(s.hls_count))
        if s.hls_age is not None:
            card("hls_age").set(f"{s.hls_age:.1f} s", s.hls_age, level=lvl(s.hls_age > 6, s.hls_age > 12))
        else:
            card("hls_age").set("—", 0.0)
        card("hls_del").set(str(s.hls_deleted))

        # ④ TV
        tv_bad = s.tv_state not in ("PLAYING", "unknown")
        card("tv_state").set(s.tv_state, level=lvl(False, tv_bad))
        card("tv_pos").set("—" if s.tv_pos is None else f"{s.tv_pos:.0f} s")
        if s.pos_delta is not None:
            card("tv_delta").set(f"+{s.pos_delta:.0f}s / {s.interval_s:.0f}s", s.pos_delta,
                                 level=lvl(s.pos_delta + 1.0 < s.interval_s,
                                           s.pos_delta + 2.0 < s.interval_s))
        else:
            card("tv_delta").set("—", 0.0)
        card("tv_stall").set(f"~{s.stall_accum:.0f} s", level=lvl(s.stall_accum >= 2.0))
        card("tv_nonplay").set(f"{s.tv_non_playing} / {s.tv_polls}",
                               level=lvl(s.tv_non_playing > 0))

        # ⑤ Sync
        card("drift").set(f"~{s.drift_ms:.0f} ms", level=lvl(s.drift_ms >= 80, s.drift_ms >= 250))
        card("dropped").set(str(s.dropped_total), level=lvl(s.dropped_total > 0))
        card("restarts").set(str(s.restarts_total), level=lvl(s.restarts_total > 0))

        self._render_status_bar(s, cap_lo, enc_lo)

    def _render_status_bar(self, s: StatsSnapshot, cap_lo: bool, enc_lo: bool) -> None:
        """One-line at-a-glance health summary with colored dots per segment."""
        def dot(label: str, bad: bool, warn: bool = False) -> str:
            color = "red" if bad else ("yellow" if warn else "green")
            return f"[{color}]●[/] {label}"

        synced = s.dropped_total == 0
        tv_ok = s.tv_state in ("PLAYING", "unknown")
        parts = [
            dot(f"capture {s.capture_fps:.0f}fps", False, cap_lo),
            dot(f"encode {s.encode_fps:.0f}fps", False, enc_lo),
            dot(f"hls {s.hls_count}seg", False, (s.hls_age or 0) > 6),
            dot(f"tv {s.tv_state.lower()}", not tv_ok, s.stall_accum >= 2.0),
            dot(f"sync {'ok' if synced else f'~{s.drift_ms:.0f}ms'}",
                s.drift_ms >= 250, not synced),
        ]
        self.query_one("#status-bar", Static).update("   ".join(parts))

    # --- audio-offset knob ------------------------------------------------
    _OFFSET_STEP_BY_ID = {
        "off_m100": -100, "off_m10": -10, "off_p10": +10, "off_p100": +100,
    }

    def on_button_pressed(self, event: Button.Pressed) -> None:
        step = self._OFFSET_STEP_BY_ID.get(event.button.id or "")
        if step is not None:
            self._nudge_offset(step)

    def action_offset(self, step: int) -> None:
        """Keyboard: nudge the audio offset (bound to [ ] { })."""
        self._nudge_offset(step)

    def action_offset_reset(self) -> None:
        """Keyboard: snap the audio offset back to 0 (bound to r)."""
        self._nudge_offset(-self._offset_pending)

    def _nudge_offset(self, step: int) -> None:
        new_pending = max(0, self._offset_pending + step)
        if new_pending == self._offset_pending and step != 0:
            return  # already clamped at 0
        self._offset_pending = new_pending
        self._refresh_knob()
        # Debounce: apply once presses settle, so we relaunch ffmpeg only once.
        if self._apply_timer is not None:
            self._apply_timer.stop()
        self._apply_timer = self.set_timer(0.8, self._apply_offset)

    def _apply_offset(self) -> None:
        if self._offset_pending == self._offset_applied or self._applying:
            return
        target = self._offset_pending
        self._applying = True
        self._refresh_knob()
        self.run_worker(
            lambda: self._do_apply(target), thread=True, exclusive=True
        )

    def _do_apply(self, target: int) -> None:
        applied = self._streamer.set_audio_offset_ms(target)
        self._offset_applied = applied
        self._applying = False
        self.call_from_thread(self._refresh_knob)
        # If more presses happened mid-apply, apply the latest too.
        if self._offset_pending != self._offset_applied:
            self.call_from_thread(self._apply_offset)

    def _refresh_knob(self) -> None:
        pending = (
            "" if self._offset_pending == self._offset_applied
            else f"  →  {self._offset_pending} ms"
        )
        self.query_one("#knob-value", Label).update(
            f"🎚  Audio offset: {self._offset_applied} ms{pending}"
        )
        status = (
            "applying… (brief glitch)" if self._applying
            else "lip-sync — keys: [ ] ±10   { } ±100   r reset"
        )
        self.query_one("#knob-status", Label).update(status)

    def action_quit(self) -> None:
        self.exit()


def run_tui(
    *,
    stats: PipelineStats,
    streamer,
    caster,
    initial_offset_ms: int = 0,
    tv_poll_interval: float = 2.0,
    playlist_url: str = "",
) -> None:
    CastTUI(
        stats=stats,
        streamer=streamer,
        caster=caster,
        initial_offset_ms=initial_offset_ms,
        tv_poll_interval=tv_poll_interval,
        playlist_url=playlist_url,
    ).run()
