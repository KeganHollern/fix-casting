"""Chromecast device discovery and interactive selection."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import zeroconf
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_GROUP
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo

DISCOVERY_CLEANUP_TIMEOUT_S = 1.0


class _DiscoveryCleanupTimeout(TimeoutError):
    """Dependency workers were signaled but did not finish within our budget."""


@dataclass(frozen=True)
class CastDevice:
    name: str
    host: str
    port: int
    model: str
    uuid: str
    cast_info: CastInfo

    @classmethod
    def from_cast_info(cls, info: CastInfo) -> CastDevice:
        return cls(
            name=info.friendly_name or "Unknown",
            host=info.host,
            port=info.port,
            model=info.model_name or "Chromecast",
            uuid=str(info.uuid),
            cast_info=info,
        )


def _snapshot_devices(browser: CastBrowser) -> tuple[CastInfo, ...]:
    """Copy the dependency's live mapping under its mutation lock."""
    lock = getattr(browser, "_services_lock", None)

    def copy_values() -> tuple[CastInfo, ...]:
        copied: list[CastInfo] = []
        for info in browser.devices.values():
            if isinstance(info, CastInfo):
                info = CastInfo(
                    services=set(info.services),
                    uuid=info.uuid,
                    model_name=info.model_name,
                    friendly_name=info.friendly_name,
                    host=info.host,
                    port=info.port,
                    cast_type=info.cast_type,
                    manufacturer=info.manufacturer,
                )
            copied.append(info)
        return tuple(copied)

    if lock is None:
        return copy_values()
    with lock:
        return copy_values()


def _cleanup_discovery_bounded(
    browser: CastBrowser | None,
    zconf: zeroconf.Zeroconf,
    *,
    timeout_s: float | None = None,
) -> BaseException | None:
    """Signal discovery now and bound dependency teardown on the caller."""
    if timeout_s is None:
        timeout_s = DISCOVERY_CLEANUP_TIMEOUT_S
    if browser is not None:
        host_browser = getattr(browser, "host_browser", None)
        stop_event = getattr(host_browser, "stop", None)
        if stop_event is not None:
            stop_event.set()

    finished = threading.Event()
    failures: list[BaseException] = []

    def cleanup() -> None:
        try:
            # Close mDNS first so service callbacks stop even if the pinned
            # PyChromecast HostBrowser remains inside its 30-second host probe.
            try:
                zconf.close()
            except BaseException as exc:
                failures.append(exc)
            if browser is not None:
                try:
                    browser.stop_discovery()
                except BaseException as exc:
                    failures.append(exc)
        finally:
            finished.set()

    threading.Thread(
        target=cleanup,
        name="cast-discovery-cleanup",
        daemon=True,
    ).start()
    if not finished.wait(timeout_s):
        return _DiscoveryCleanupTimeout(
            "Chromecast discovery cleanup exceeded " f"{timeout_s:g}s"
        )
    if len(failures) == 1:
        return failures[0]
    if failures:
        return BaseExceptionGroup("Chromecast discovery cleanup failed", failures)
    return None


def discover_devices(timeout: float = 5.0) -> list[CastDevice]:
    """Discover Chromecast devices within a bounded wait and cleanup window."""
    zconf = zeroconf.Zeroconf()
    browser: CastBrowser | None = None
    snapshot: tuple[CastInfo, ...] = ()
    operation_failure: BaseException | None = None
    try:
        browser = CastBrowser(SimpleCastListener(), zconf)
        browser.start_discovery()
        time.sleep(timeout)
        snapshot = _snapshot_devices(browser)
    except BaseException as exc:
        operation_failure = exc

    cleanup_failure = _cleanup_discovery_bounded(browser, zconf)
    if operation_failure is not None:
        if cleanup_failure is not None:
            operation_failure.add_note(
                f"Discovery cleanup also failed: {cleanup_failure}"
            )
        if isinstance(operation_failure, (KeyboardInterrupt, SystemExit)):
            raise operation_failure
        if not isinstance(operation_failure, RuntimeError):
            raise RuntimeError(
                f"Chromecast discovery failed: {operation_failure}"
            ) from operation_failure
        raise operation_failure
    if isinstance(cleanup_failure, _DiscoveryCleanupTimeout):
        # The snapshot above is a deep copy taken under PyChromecast's mutation
        # lock. HostBrowser is a daemon and has already been signaled; a slow
        # 30-second dependency probe can unwind in the background without
        # invalidating devices that were successfully discovered.
        print(
            f"[discovery] {cleanup_failure}; continuing with the completed snapshot.",
            flush=True,
        )
    elif cleanup_failure is not None:
        if isinstance(cleanup_failure, (KeyboardInterrupt, SystemExit)):
            raise cleanup_failure
        raise RuntimeError(
            f"Chromecast discovery cleanup failed: {cleanup_failure}"
        ) from cleanup_failure

    devices = [
        CastDevice.from_cast_info(info)
        for info in snapshot
        # Some discovery records have not resolved a type yet; retain those
        # for the bounded connection-time probe. Explicit speakers and speaker
        # groups cannot render the mirrored video stream and must not be
        # offered as TVs.
        if info.cast_type not in {CAST_TYPE_AUDIO, CAST_TYPE_GROUP}
    ]
    return sorted(devices, key=lambda d: d.name.lower())


def find_device(devices: list[CastDevice], name: str) -> CastDevice:
    """Pick a device by name: case-insensitive exact, else unique substring."""
    if not devices:
        raise RuntimeError(
            "No Chromecast devices found on the network. "
            "Make sure your Chromecast is on and connected to the same LAN."
        )
    folded = name.casefold()
    exact = [d for d in devices if d.name.casefold() == folded]
    if len(exact) == 1:
        return exact[0]
    partial = [d for d in devices if folded in d.name.casefold()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(f"'{d.name}'" for d in devices)
    problem = "is ambiguous" if len(partial) > 1 else "matches no device"
    raise RuntimeError(f"--device '{name}' {problem}. Devices found: {names}.")


def select_device(devices: list[CastDevice]) -> CastDevice:
    """Prompt the user to pick a Chromecast device."""
    if not devices:
        raise RuntimeError(
            "No Chromecast devices found on the network. "
            "Make sure your Chromecast is on and connected to the same LAN."
        )

    if len(devices) == 1:
        print(f"Found 1 device: {devices[0].name}")
        return devices[0]

    print(f"Found {len(devices)} Chromecast devices:")
    for i, device in enumerate(devices, start=1):
        print(f"  {i}) {device.name} ({device.model}) @ {device.host}:{device.port}")

    while True:
        try:
            choice = input("Select device [1]: ").strip()
            if not choice:
                return devices[0]
            index = int(choice)
            if 1 <= index <= len(devices):
                return devices[index - 1]
        except ValueError:
            pass
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0) from None
        print(f"Enter a number between 1 and {len(devices)}.")
