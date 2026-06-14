"""Chromecast device discovery and interactive selection."""

from __future__ import annotations

import time
from dataclasses import dataclass

import pychromecast
import zeroconf
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo


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


def discover_devices(timeout: float = 5.0) -> list[CastDevice]:
    """Discover Chromecast devices on the local network."""
    zconf = zeroconf.Zeroconf()
    browser = CastBrowser(SimpleCastListener(), zconf)
    browser.start_discovery()
    time.sleep(timeout)
    devices = [CastDevice.from_cast_info(info) for info in browser.devices.values()]
    browser.stop_discovery()
    zconf.close()
    return sorted(devices, key=lambda d: d.name.lower())


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