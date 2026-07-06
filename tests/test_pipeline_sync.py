"""End-to-end A/V sync regression: the real HLSStreamer path, no Chrome.

Wraps tools/test_pipeline_skew.py (paced JPEG frames + real-time PCM through
the production streamer into HLS) and asserts the measured flash/beep offset
stays within the frame-quantization noise floor. This locks in the A/V-sync
fixes (first-frame anchor, no-probe MJPEG input, bounded queue).

Runs ~45s; needs ffmpeg. Select with: pytest -m slow
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# ±4 frames at 30fps. The harness quantizes flash detection to frame PTS, so
# ~2 frames of noise is inherent; the failure mode this guards against is the
# historical ~700ms-and-growing skew, not tens of ms.
TOLERANCE_MS = 133

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]


def test_pipeline_av_offset_within_noise_floor():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "test_pipeline_skew.py"),
         "--seconds", "30"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=ROOT,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    match = re.search(r"median:\s*(-?\d+(?:\.\d+)?)\s*ms", proc.stdout)
    assert match, f"no median offset in output:\n{proc.stdout}"
    median_ms = float(match.group(1))
    assert abs(median_ms) <= TOLERANCE_MS, (
        f"A/V offset {median_ms:+.0f}ms exceeds ±{TOLERANCE_MS}ms:\n{proc.stdout}"
    )
