"""Unit tests for encoder arg construction (pure, no ffmpeg needed)."""

from cast_tab.encoder import (
    default_fps_for_resolution,
    hls_args,
    target_bitrate,
    tv_delay_s,
    video_encoder_args,
)


def test_target_bitrate_resolution_tiers():
    assert target_bitrate(1920, 1080, buffered=True) == ("15M", "18M", "36M")
    assert target_bitrate(1920, 1080, buffered=False) == ("15M", "16.5M", "16.5M")
    assert target_bitrate(1280, 720, buffered=True) == ("3M", "3.5M", "8M")
    assert target_bitrate(640, 480, buffered=False) == ("1.5M", "2M", "2M")


def test_target_bitrate_override_ratios():
    # Buffered: roomy VBV (1.2x maxrate, 2.4x bufsize).
    assert target_bitrate(1920, 1080, buffered=True, override_mbps=10) == (
        "10M", "12M", "24M",
    )
    # Unbuffered: tight VBV (1.1x both).
    assert target_bitrate(1920, 1080, buffered=False, override_mbps=10) == (
        "10M", "11M", "11M",
    )


def test_default_fps():
    assert default_fps_for_resolution(1920, 1080, buffered=True) == 30
    assert default_fps_for_resolution(1920, 1080, buffered=False) == 23
    assert default_fps_for_resolution(1280, 720, buffered=False) == 24


def test_video_encoder_args_gop():
    args = video_encoder_args(30, 1920, 1080, buffered=True)
    gop = args[args.index("-g") + 1]
    keyint = args[args.index("-keyint_min") + 1]
    assert gop == "60"  # 2s GOP when buffered
    assert keyint == "30"
    args = video_encoder_args(30, 1920, 1080, buffered=False)
    assert args[args.index("-g") + 1] == "30"  # 1s GOP unbuffered


def test_hls_args_and_tv_delay_consistent():
    """The advertised TV delay must equal segment length x playlist length."""
    for buffered in (True, False):
        args = hls_args(buffered=buffered)
        hls_time = int(args[args.index("-hls_time") + 1])
        list_size = int(args[args.index("-hls_list_size") + 1])
        assert tv_delay_s(buffered=buffered) == hls_time * list_size
    assert tv_delay_s(buffered=True) == 48
    assert tv_delay_s(buffered=False) == 4
