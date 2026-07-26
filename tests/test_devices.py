"""Unit tests for --device name matching."""

import threading
import time

import pytest

import cast_tab.devices as devices_module
from cast_tab.devices import CastDevice, find_device


def _dev(name: str) -> CastDevice:
    return CastDevice(name=name, host="192.168.1.10", port=8009,
                      model="Chromecast", uuid="u-" + name, cast_info=None)


DEVICES = [_dev("Living Room TV"), _dev("Bedroom TV"), _dev("Kitchen display")]


def test_exact_match_case_insensitive():
    assert find_device(DEVICES, "living room tv").name == "Living Room TV"


def test_unique_substring_match():
    assert find_device(DEVICES, "kitchen").name == "Kitchen display"
    assert find_device(DEVICES, "bed").name == "Bedroom TV"


def test_exact_beats_substring():
    devices = [_dev("TV"), _dev("TV upstairs")]
    assert find_device(devices, "tv").name == "TV"


def test_ambiguous_substring_raises():
    with pytest.raises(RuntimeError, match="ambiguous"):
        find_device(DEVICES, "tv")


def test_no_match_raises_listing_devices():
    with pytest.raises(RuntimeError, match="matches no device.*Living Room TV"):
        find_device(DEVICES, "garage")


def test_empty_discovery_raises():
    with pytest.raises(RuntimeError, match="No Chromecast devices"):
        find_device([], "anything")


def test_discovery_filters_audio_and_group_targets(monkeypatch):
    infos = {
        "speaker": type(
            "Info",
            (),
            {
                "friendly_name": "Nest Audio",
                "host": "192.0.2.1",
                "port": 8009,
                "model_name": "Nest Audio",
                "uuid": "speaker",
                "cast_type": "audio",
            },
        )(),
        "group": type(
            "Info",
            (),
            {
                "friendly_name": "Whole House",
                "host": "192.0.2.2",
                "port": 8009,
                "model_name": "Group",
                "uuid": "group",
                "cast_type": "group",
            },
        )(),
        "tv": type(
            "Info",
            (),
            {
                "friendly_name": "Den TV",
                "host": "192.0.2.3",
                "port": 8009,
                "model_name": "Chromecast",
                "uuid": "tv",
                "cast_type": "cast",
            },
        )(),
        "unknown": type(
            "Info",
            (),
            {
                "friendly_name": "Unresolved display",
                "host": "192.0.2.4",
                "port": 8009,
                "model_name": "Unknown",
                "uuid": "unknown",
                "cast_type": None,
            },
        )(),
    }

    class Zeroconf:
        def close(self) -> None:
            pass

    class Browser:
        def __init__(self, _listener, _zconf) -> None:
            self.devices = infos

        def start_discovery(self) -> None:
            pass

        def stop_discovery(self) -> None:
            pass

    monkeypatch.setattr(devices_module.zeroconf, "Zeroconf", Zeroconf)
    monkeypatch.setattr(devices_module, "CastBrowser", Browser)
    monkeypatch.setattr(devices_module.time, "sleep", lambda _timeout: None)

    found = devices_module.discover_devices(timeout=0.01)

    assert [device.name for device in found] == ["Den TV", "Unresolved display"]


def test_discovery_snapshots_under_lock_and_closes_resources(monkeypatch):
    events: list[str] = []
    info = type(
        "Info",
        (),
        {
            "friendly_name": "Den TV",
            "host": "192.0.2.3",
            "port": 8009,
            "model_name": "Chromecast",
            "uuid": "tv",
            "cast_type": "cast",
        },
    )()

    class Zeroconf:
        def close(self) -> None:
            events.append("close")

    class ObservedLock:
        held = False

        def __enter__(self):
            self.held = True

        def __exit__(self, *_args):
            self.held = False

    class Browser:
        def __init__(self, _listener, _zconf) -> None:
            self._services_lock = ObservedLock()

        @property
        def devices(self):
            assert self._services_lock.held
            events.append("snapshot")
            return {"tv": info}

        def start_discovery(self) -> None:
            events.append("start")

        def stop_discovery(self) -> None:
            events.append("stop")

    monkeypatch.setattr(devices_module.zeroconf, "Zeroconf", Zeroconf)
    monkeypatch.setattr(devices_module, "CastBrowser", Browser)
    monkeypatch.setattr(devices_module.time, "sleep", lambda _timeout: None)

    assert [device.name for device in devices_module.discover_devices()] == ["Den TV"]
    assert events == ["start", "snapshot", "close", "stop"]


@pytest.mark.parametrize("failure_phase", ["start", "wait", "stop"])
def test_discovery_closes_every_resource_on_failure(monkeypatch, failure_phase):
    events: list[str] = []

    class Zeroconf:
        def close(self) -> None:
            events.append("close")

    class Browser:
        devices = {}

        def __init__(self, _listener, _zconf) -> None:
            pass

        def start_discovery(self) -> None:
            events.append("start")
            if failure_phase == "start":
                raise RuntimeError("start failed")

        def stop_discovery(self) -> None:
            events.append("stop")
            if failure_phase == "stop":
                raise RuntimeError("stop failed")

    def wait(_timeout) -> None:
        if failure_phase == "wait":
            raise RuntimeError("wait failed")

    monkeypatch.setattr(devices_module.zeroconf, "Zeroconf", Zeroconf)
    monkeypatch.setattr(devices_module, "CastBrowser", Browser)
    monkeypatch.setattr(devices_module.time, "sleep", wait)

    with pytest.raises(RuntimeError, match=f"{failure_phase} failed"):
        devices_module.discover_devices()

    assert "stop" in events
    assert "close" in events


def test_slow_cleanup_keeps_successful_snapshot_and_signals_host_worker(
    monkeypatch,
):
    stop_entered = threading.Event()
    release_stop = threading.Event()
    host_stop = threading.Event()
    zconf_closed = threading.Event()

    class Zeroconf:
        def close(self) -> None:
            zconf_closed.set()

    info = type(
        "Info",
        (),
        {
            "friendly_name": "Den TV",
            "host": "192.0.2.3",
            "port": 8009,
            "model_name": "Chromecast",
            "uuid": "tv",
            "cast_type": "cast",
        },
    )()

    class Browser:
        def __init__(self, _listener, _zconf) -> None:
            self.host_browser = type("Host", (), {"stop": host_stop})()
            self._services_lock = threading.Lock()
            self.devices = {"tv": info}

        def start_discovery(self) -> None:
            pass

        def stop_discovery(self) -> None:
            stop_entered.set()
            release_stop.wait(timeout=1)

    monkeypatch.setattr(devices_module.zeroconf, "Zeroconf", Zeroconf)
    monkeypatch.setattr(devices_module, "CastBrowser", Browser)
    monkeypatch.setattr(devices_module.time, "sleep", lambda _timeout: None)
    monkeypatch.setattr(devices_module, "DISCOVERY_CLEANUP_TIMEOUT_S", 0.02)

    started = time.monotonic()
    try:
        found = devices_module.discover_devices(timeout=0.01)
    finally:
        release_stop.set()

    assert time.monotonic() - started < 0.25
    assert [device.name for device in found] == ["Den TV"]
    assert host_stop.is_set()
    assert zconf_closed.is_set()
    assert stop_entered.is_set()


def test_discovery_failure_survives_cleanup_timeout_with_context(monkeypatch):
    release_stop = threading.Event()

    class Zeroconf:
        def close(self) -> None:
            pass

    class Browser:
        devices = {}

        def __init__(self, _listener, _zconf) -> None:
            self.host_browser = type(
                "Host",
                (),
                {"stop": threading.Event()},
            )()

        def start_discovery(self) -> None:
            pass

        def stop_discovery(self) -> None:
            release_stop.wait(timeout=1)

    monkeypatch.setattr(devices_module.zeroconf, "Zeroconf", Zeroconf)
    monkeypatch.setattr(devices_module, "CastBrowser", Browser)
    monkeypatch.setattr(
        devices_module.time,
        "sleep",
        lambda _timeout: (_ for _ in ()).throw(RuntimeError("wait failed")),
    )
    monkeypatch.setattr(devices_module, "DISCOVERY_CLEANUP_TIMEOUT_S", 0.02)

    try:
        with pytest.raises(RuntimeError, match="wait failed") as caught:
            devices_module.discover_devices(timeout=0.01)
    finally:
        release_stop.set()

    assert any("cleanup also failed" in note for note in caught.value.__notes__)
