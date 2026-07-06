"""Unit tests for --device name matching."""

import pytest

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
