"""Capture system audio for tab casting on macOS via BlackHole."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str


@dataclass(frozen=True)
class AudioSetup:
    device: AudioDevice
    previous_output: str | None


class AudioCaptureError(RuntimeError):
    """Raised when tab audio cannot be captured."""


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=check,
    )


def list_avfoundation_audio_devices() -> list[AudioDevice]:
    """Return audio input devices visible to ffmpeg avfoundation."""
    if shutil.which("ffmpeg") is None:
        return []

    result = _run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        check=False,
    )
    output = result.stderr

    devices: list[AudioDevice] = []
    in_audio = False
    for line in output.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if in_audio and "AVFoundation video devices" in line:
            break
        if not in_audio:
            continue

        match = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if match:
            devices.append(AudioDevice(index=int(match.group(1)), name=match.group(2).strip()))

    return devices


def find_blackhole_device() -> AudioDevice | None:
    for device in list_avfoundation_audio_devices():
        if "blackhole" in device.name.lower():
            return device
    return None


def blackhole_installed() -> bool:
    return find_blackhole_device() is not None


def switchaudio_source_path() -> str | None:
    path = shutil.which("SwitchAudioSource")
    if path:
        return path

    for candidate in (
        "/opt/homebrew/bin/SwitchAudioSource",
        "/usr/local/bin/SwitchAudioSource",
    ):
        if shutil.which(candidate) or _path_exists(candidate):
            return candidate
    return None


def _path_exists(path: str) -> bool:
    from pathlib import Path

    return Path(path).exists()


def current_output_device() -> str | None:
    switchaudio = switchaudio_source_path()
    if not switchaudio:
        return None
    result = _run([switchaudio, "-c", "output"], check=False)
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def set_output_device(name: str) -> None:
    switchaudio = switchaudio_source_path()
    if not switchaudio:
        raise AudioCaptureError(
            "SwitchAudioSource is required to route tab audio. "
            "Install with: brew install switchaudio-osx"
        )
    result = _run([switchaudio, "-s", name, "-t", "output"], check=False)
    if result.returncode != 0:
        raise AudioCaptureError(
            f"Failed to switch audio output to {name}: {result.stderr.strip()}"
        )


def ffmpeg_audio_input(device: AudioDevice) -> str:
    """avfoundation audio-only input string for ffmpeg."""
    return f"none:{device.name}"


def setup_tab_audio() -> AudioSetup:
    """Route system audio into BlackHole so ffmpeg can capture browser tab sound."""
    device = find_blackhole_device()
    if device is None:
        raise AudioCaptureError(
            "BlackHole is not installed. Tab audio capture requires a virtual audio device.\n"
            "Install with: brew install blackhole-2ch\n"
            "Then restart `cast` — audio from Chrome will be routed through BlackHole."
        )

    previous_output = current_output_device()
    if previous_output and "blackhole" in previous_output.lower():
        previous_output = None

    set_output_device(device.name)
    return AudioSetup(device=device, previous_output=previous_output)


def restore_audio_output(setup: AudioSetup | None) -> None:
    if setup is None or not setup.previous_output:
        return
    try:
        set_output_device(setup.previous_output)
    except AudioCaptureError:
        pass


def install_hint() -> str:
    return (
        "To enable TV audio:\n"
        "  brew install blackhole-2ch switchaudio-osx\n"
        "Then run `cast` again (omit --no-audio)."
    )