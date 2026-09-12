"""`tests/fixtures/make_wav.py` が実機の WAV を再現することを固定する。

**核心の保証は純 Python で検証する**（RIFF を自分で読む）。`ffprobe` は CI の uv イメージに
無いため、そこへ依存させると **CI では黙って skip される**（§20.2 / 判断 4）。
`ffprobe` による裏取りは `needs_ffmpeg` を付ける。

期待値の出所は `docs/POC.md` §6.2 の実機実測である。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.make_wav import (
    BWF_HEADER_BYTES,
    CHANNELS,
    MINIMAL_HEADER_BYTES,
    PAD_SIZE,
    SAMPLE_RATE,
    Content,
    WavFormat,
    build_wav,
    byte_rate,
    main,
    sample_width,
    write_wav,
)

# POC §6.2 の実測チャンク順
EXPECTED_CHUNKS = ["fmt ", "bext", "iXML", "cue ", "PAD ", "data"]


def chunks(blob: bytes) -> list[tuple[str, int, int]]:
    """RIFF を読み、`(id, size, payload の開始 offset)` を並び順に返す。"""
    assert blob[:4] == b"RIFF"
    assert blob[8:12] == b"WAVE"
    assert struct.unpack("<I", blob[4:8])[0] == len(blob) - 8
    found: list[tuple[str, int, int]] = []
    offset = 12
    while offset + 8 <= len(blob):
        chunk_id = blob[offset : offset + 4].decode("ascii")
        size = struct.unpack("<I", blob[offset + 4 : offset + 8])[0]
        found.append((chunk_id, size, offset + 8))
        offset += 8 + size + (size % 2)
    return found


def chunk(blob: bytes, chunk_id: str) -> tuple[int, int]:
    """`(size, payload の開始 offset)`。"""
    for found_id, size, start in chunks(blob):
        if found_id == chunk_id:
            return size, start
    raise AssertionError(f"{chunk_id!r} が無い: {[c[0] for c in chunks(blob)]}")


def fmt_fields(blob: bytes) -> dict[str, int]:
    _size, start = chunk(blob, "fmt ")
    tag, channels, rate, brate, align, bits = struct.unpack("<HHIIHH", blob[start : start + 16])
    return {
        "tag": tag,
        "channels": channels,
        "rate": rate,
        "byte_rate": brate,
        "align": align,
        "bits": bits,
    }


# --- チャンク構成 --------------------------------------------------------


@pytest.mark.parametrize("fmt", list(WavFormat))
def test_riff_layout(fmt: WavFormat) -> None:
    """チャンクの並びが実機の実測どおりであること（POC §6.2）。"""
    blob = build_wav(seconds=0.5, fmt=fmt)
    assert [c[0] for c in chunks(blob)] == EXPECTED_CHUNKS


@pytest.mark.parametrize("fmt", list(WavFormat))
def test_header_is_32776_bytes(fmt: WavFormat) -> None:
    """`data` の開始が offset **32,776** であること。

    「`data` は offset 44 から始まる」と決め打ちした実装をここで落とす（§20.2）。
    """
    blob = build_wav(seconds=0.25, fmt=fmt)
    _size, start = chunk(blob, "data")
    assert start == BWF_HEADER_BYTES == 32776


def test_pad_size_matches_the_measurement() -> None:
    """`PAD ` は逆算で決めている。実測値と一致していること。"""
    assert PAD_SIZE == 30978


def test_minimal_header_is_44_bytes() -> None:
    blob = build_wav(seconds=0.25, minimal_header=True)
    assert [c[0] for c in chunks(blob)] == ["fmt ", "data"]
    _size, start = chunk(blob, "data")
    assert start == MINIMAL_HEADER_BYTES == 44


# --- fmt チャンク --------------------------------------------------------


def test_fmt_chunk_pcm24() -> None:
    """実機の実測: `tag=0x0001` / ch=1 / 48,000 Hz / 144,000 B/秒 / align=3 / 24 bit。"""
    assert fmt_fields(build_wav(seconds=0.25, fmt=WavFormat.PCM24)) == {
        "tag": 1,
        "channels": 1,
        "rate": 48000,
        "byte_rate": 144000,
        "align": 3,
        "bits": 24,
    }


def test_fmt_chunk_float32() -> None:
    """32 bit float（`tag=3` = IEEE float）。192,000 B/秒。"""
    assert fmt_fields(build_wav(seconds=0.25, fmt=WavFormat.FLOAT32)) == {
        "tag": 3,
        "channels": 1,
        "rate": 48000,
        "byte_rate": 192000,
        "align": 4,
        "bits": 32,
    }


def test_never_16khz() -> None:
    """**16 kHz を生成してはならない**（§20.2。§10.5 の変換バグを見逃す）。"""
    assert SAMPLE_RATE == 48000
    assert CHANNELS == 1
    for fmt in WavFormat:
        assert fmt_fields(build_wav(seconds=0.25, fmt=fmt))["rate"] != 16000


@pytest.mark.parametrize("fmt", list(WavFormat))
@pytest.mark.parametrize("seconds", [0.25, 1.0, 1.5])
def test_data_length_matches_seconds(fmt: WavFormat, seconds: float) -> None:
    blob = build_wav(seconds=seconds, fmt=fmt)
    size, _start = chunk(blob, "data")
    assert size == round(seconds * byte_rate(fmt))


# --- 中身 ----------------------------------------------------------------


@pytest.mark.parametrize("fmt", list(WavFormat))
def test_silence_is_all_zero(fmt: WavFormat) -> None:
    blob = build_wav(seconds=0.5, fmt=fmt, content=Content.SILENCE)
    size, start = chunk(blob, "data")
    assert blob[start : start + size] == b"\0" * size


@pytest.mark.parametrize("fmt", list(WavFormat))
def test_speech_is_not_silent(fmt: WavFormat) -> None:
    """発話区間があること。#21 の `NO_SPEECH_DETECTED` と区別できなければ意味がない。"""
    blob = build_wav(seconds=1.5, fmt=fmt, content=Content.SPEECH)
    size, start = chunk(blob, "data")
    data = blob[start : start + size]
    nonzero = sum(1 for byte in data if byte)
    assert nonzero > size * 0.10, f"非ゼロが少なすぎる: {nonzero}/{size}"


def test_deterministic() -> None:
    """同じ引数なら同じバイト列（乱数を使っていないこと）。"""
    first = build_wav(seconds=0.5, content=Content.SPEECH)
    second = build_wav(seconds=0.5, content=Content.SPEECH)
    assert first == second


def test_tone_hz_changes_content() -> None:
    """`tone_hz` で中身が変わること。

    同じ録音の orig と denoised は**別ファイル**であり、sha256 が一致してはならない。
    """
    a = build_wav(seconds=0.5, content=Content.SPEECH, tone_hz=220.0)
    b = build_wav(seconds=0.5, content=Content.SPEECH, tone_hz=330.0)
    assert hashlib.sha256(a).hexdigest() != hashlib.sha256(b).hexdigest()


def test_pcm24_negative_samples_are_two_complement() -> None:
    """負のサンプルが 3 バイトの 2 の補数で入ること（`-1` → `ff ff ff`）。"""
    blob = build_wav(seconds=0.5, content=Content.SPEECH)
    size, start = chunk(blob, "data")
    data = blob[start : start + size]
    width = sample_width(WavFormat.PCM24)
    values = [
        int.from_bytes(data[i : i + width], "little", signed=True) for i in range(0, size, width)
    ]
    assert any(value < 0 for value in values), "負のサンプルが 1 つも無い"
    assert min(values) >= -(2**23)
    assert max(values) <= 2**23 - 1


@pytest.mark.parametrize("seconds", [0, -1.0])
def test_rejects_non_positive_seconds(seconds: float) -> None:
    with pytest.raises(ValueError, match="seconds は正の値"):
        build_wav(seconds=seconds)


# --- CLI とファイル出力 --------------------------------------------------


def test_write_wav_creates_parents(tmp_path: Path) -> None:
    target = write_wav(tmp_path / "a" / "b" / "x.wav", seconds=0.25)
    assert target.is_file()
    assert target.stat().st_size == BWF_HEADER_BYTES + round(0.25 * byte_rate(WavFormat.PCM24))


def test_cli_writes_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "cli.wav"
    assert main([str(out), "--seconds", "0.5", "--content", "speech"]) == 0
    assert out.is_file()
    printed = capsys.readouterr().out
    assert "header=32776" in printed
    assert "byte_rate=144000" in printed


def test_cli_minimal_header(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "cli.wav"
    assert main([str(out), "--seconds", "0.25", "--minimal-header"]) == 0
    assert "header=44" in capsys.readouterr().out


# --- ffprobe による裏取り（コンテナ内でのみ走る） ------------------------


def probe(path: Path) -> dict[str, Any]:
    # 絶対パスで起動する（S607）。needs_ffmpeg が付いているので必ず見つかる
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None, "needs_ffmpeg が付いているのに ffprobe が無い"
    result = subprocess.run(  # noqa: S603
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,sample_fmt,bits_per_raw_sample"
            ":format=duration,bit_rate,size",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


@pytest.mark.needs_ffmpeg
@pytest.mark.parametrize(
    ("fmt", "codec", "sample_fmt", "raw_bits"),
    [
        (WavFormat.PCM24, "pcm_s24le", "s32", "24"),
        (WavFormat.FLOAT32, "pcm_f32le", "flt", None),
    ],
)
def test_ffprobe_agrees(
    tmp_path: Path, fmt: WavFormat, codec: str, sample_fmt: str, raw_bits: str | None
) -> None:
    """実際の toolchain が読めること。

    **24 bit の `sample_fmt` は `s32` になる**（ffmpeg が s24 を s32 へ展開して報告する）。
    24 bit であることは `bits_per_raw_sample` で見る。
    """
    target = write_wav(tmp_path / "p.wav", seconds=1.0, fmt=fmt, content=Content.SPEECH)
    stream = probe(target)["streams"][0]
    assert stream["codec_name"] == codec
    assert stream["sample_rate"] == "48000"
    assert stream["channels"] == 1
    assert stream["sample_fmt"] == sample_fmt
    assert stream.get("bits_per_raw_sample") == raw_bits
    assert probe(target)["format"]["duration"] == "1.000000"


@pytest.mark.needs_ffmpeg
def test_ffprobe_agrees_on_minimal_header(tmp_path: Path) -> None:
    target = write_wav(tmp_path / "m.wav", seconds=1.0, minimal_header=True)
    stream = probe(target)["streams"][0]
    assert stream["codec_name"] == "pcm_s24le"
    assert probe(target)["format"]["duration"] == "1.000000"


@pytest.mark.needs_ffmpeg
def test_ffprobe_bit_rate_includes_the_header(tmp_path: Path) -> None:
    """`format.bit_rate` がヘッダを含むためズレることを固定する（§5.1 の注記の根拠）。

    1 秒の 24 bit で真の音声は 1,152,000 bps だが、32 KB のヘッダが乗って 1,414,208 bps に
    なる（**23% の誤差**）。**バイトレートは `fmt` チャンクで見ること。**
    """
    target = write_wav(tmp_path / "b.wav", seconds=1.0)
    reported = int(probe(target)["format"]["bit_rate"])
    true_audio = byte_rate(WavFormat.PCM24) * 8
    assert reported > true_audio * 1.2
    assert fmt_fields(target.read_bytes())["byte_rate"] == byte_rate(WavFormat.PCM24)
