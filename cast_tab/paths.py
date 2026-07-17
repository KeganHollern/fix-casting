"""Stable per-user paths shared by the installed CLI and native helpers."""

from __future__ import annotations

import os
from pathlib import Path


def user_data_dir() -> Path:
    """Return fix-casting's persistent, checkout-independent data directory."""
    data_home = os.environ.get("XDG_DATA_HOME")
    default = Path.home() / ".local" / "share"
    base = Path(data_home).expanduser() if data_home else default
    # The XDG spec requires an absolute path. Avoid resolving a malformed
    # relative value against whatever directory `cast` happens to start in.
    if not base.is_absolute():
        base = default
    return base / "fix-casting"


INSTALL_DATA_DIR = user_data_dir()
AUDIOTEE_INSTALL_PATH = INSTALL_DATA_DIR / "bin" / "audiotee"
AUDIOTEE_PROVENANCE_PATH = INSTALL_DATA_DIR / "audiotee.sha256"
INSTALL_PROVENANCE_PATH = INSTALL_DATA_DIR / "revision"
