"""Unit tests for AudioTee metadata parsing."""

import json

from cast_tab.audio import (
    AUDIOTEE_PROTOCOL_VERSION,
    DEFAULT_AUDIO_FORMAT,
    _parse_audio_format,
)


def _meta(**data) -> str:
    data.setdefault("protocol_version", AUDIOTEE_PROTOCOL_VERSION)
    return json.dumps({"message_type": "metadata", "data": data})


def test_parse_valid_metadata():
    fmt = _parse_audio_format(
        _meta(sample_rate=48000, channels_per_frame=2, bits_per_channel=32,
              encoding="pcm_f32le")
    )
    assert fmt is not None
    assert fmt.sample_rate == 48000
    assert fmt.channels == 2
    assert fmt.ffmpeg_format == "f32le"  # pcm_ prefix stripped
    assert fmt.sample_bytes == 4
    assert fmt.bytes_per_second == 48000 * 2 * 4


def test_parse_float_sample_rate():
    fmt = _parse_audio_format(
        _meta(sample_rate="44100.0", channels_per_frame=2, bits_per_channel=16,
              encoding="pcm_s16le")
    )
    assert fmt is not None
    assert fmt.sample_rate == 44100
    assert fmt.ffmpeg_format == "s16le"
    assert fmt.sample_bytes == 2


def test_parse_rejects_garbage():
    assert _parse_audio_format("not json") is None
    assert _parse_audio_format(json.dumps({"data": "not a dict"})) is None
    assert _parse_audio_format(_meta(sample_rate=48000)) is None  # missing fields
    assert _parse_audio_format(_meta(sample_rate="x", channels_per_frame=2,
                                     bits_per_channel=32, encoding="pcm_f32le")) is None
    assert (
        _parse_audio_format(
            _meta(
                protocol_version=AUDIOTEE_PROTOCOL_VERSION - 1,
                sample_rate=48000,
                channels_per_frame=2,
                bits_per_channel=32,
                encoding="pcm_f32le",
            )
        )
        is None
    )


def test_default_format():
    assert DEFAULT_AUDIO_FORMAT.bytes_per_second == 48_000 * 2 * 4
