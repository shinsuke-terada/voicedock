"""テスト用の WAV を生成する（SPEC §20.2 / §5.1、`docs/POC.md` §6.2）。

**標準ライブラリだけで書く。**ホストに `ffmpeg` は無い（§3.3 の方針どおり正しい状態）し、
`wave` モジュールは 24 bit も float32 も書けないので RIFF を自分で組み立てる。
`voicedock` を import しない — これはテスト側の道具であり、実装に依存しない。

**既定は実機の実測どおり 48 kHz / 24 bit PCM / モノラル**（144,000 B/秒）。
32 bit float（192,000 B/秒）も生成できる。DJI 側の設定で形式が変わるため（§5.1）、
パイプラインは両方を扱えなければならない。**16 kHz を生成してはならない**
（§10.5 の変換バグを見逃す。§20.2）。

ライブラリとしても CLI としても使える。

    python tests/fixtures/make_wav.py OUT.wav --seconds 3 --format pcm24 --content speech
"""

from __future__ import annotations

import argparse
import hashlib
import math
import struct
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Final

SAMPLE_RATE: Final = 48000
CHANNELS: Final = 1

_BLOCK = 8192
"""`struct.pack` へ一度に渡すサンプル数。100 万個の引数を渡さないため。"""


class WavFormat(StrEnum):
    PCM24 = "pcm24"
    """実機の実測（`docs/POC.md` §6.2）。144,000 B/秒。"""

    FLOAT32 = "float32"
    """DJI 側の設定で選べるもう一方。192,000 B/秒。"""


class Content(StrEnum):
    SILENCE = "silence"
    """無音。#21 の `NO_SPEECH_DETECTED` のテストで使う。"""

    SPEECH = "speech"
    """発話相当。可聴帯域のトーンと無音を交替させる。"""


# --- POC §6.2 の実測チャンク構成 ----------------------------------------

BEXT_SIZE: Final = 602
IXML_SIZE: Final = 1092
CUE_SIZE: Final = 28
BWF_HEADER_BYTES: Final = 32776
"""実機の `data` チャンク開始オフセット（`docs/POC.md` §6.2 の実測）。"""

MINIMAL_HEADER_BYTES: Final = 44
"""`fmt ` と `data` だけの最小 RIFF。"""

_RIFF_HEADER = 12  # "RIFF" + size + "WAVE"
_CHUNK_HEADER = 8  # id + size
_FMT_SIZE = 16

# `PAD ` は data の開始を実測値へ合わせるための詰め物である。直書きせず逆算する。
PAD_SIZE: Final = BWF_HEADER_BYTES - (
    _RIFF_HEADER
    + _CHUNK_HEADER * 5  # fmt / bext / iXML / cue / PAD
    + _FMT_SIZE
    + BEXT_SIZE
    + IXML_SIZE
    + CUE_SIZE
    + _CHUNK_HEADER  # data のヘッダ
)
assert PAD_SIZE == 30978, f"POC §6.2 の実測 30,978 と合わない: {PAD_SIZE}"

_SPEECH_ON = 0.8
"""発話区間の長さ（秒）。"""

_SPEECH_OFF = 0.4
"""無音区間の長さ（秒）。"""

_AMPLITUDE = 0.1
"""おおよそ −20 dBFS。"""


def sample_width(fmt: WavFormat) -> int:
    """1 サンプルのバイト数（モノラルなので block align と同じ）。"""
    return 3 if fmt is WavFormat.PCM24 else 4


def byte_rate(fmt: WavFormat) -> int:
    """1 秒あたりのバイト数。`fmt` チャンクの `byteRate` と同じ値。"""
    return SAMPLE_RATE * CHANNELS * sample_width(fmt)


def header_bytes(*, minimal_header: bool = False) -> int:
    return MINIMAL_HEADER_BYTES if minimal_header else BWF_HEADER_BYTES


# --- サンプル生成 --------------------------------------------------------


def _samples(frames: int, content: Content, tone_hz: float) -> list[float]:
    """−1.0〜1.0 のサンプル列。**乱数を使わない**（同じ引数なら同じバイト列になる）。"""
    if content is Content.SILENCE:
        return [0.0] * frames

    period = _SPEECH_ON + _SPEECH_OFF
    values: list[float] = []
    for index in range(frames):
        seconds = index / SAMPLE_RATE
        if seconds % period >= _SPEECH_ON:
            values.append(0.0)
            continue
        angle = 2.0 * math.pi * tone_hz * seconds
        values.append(
            _AMPLITUDE * (math.sin(angle) + 0.5 * math.sin(2 * angle) + 0.25 * math.sin(4 * angle))
        )
    return values


def _encode(values: Sequence[float], fmt: WavFormat) -> bytes:
    """サンプル列をリトルエンディアンのバイト列にする。

    24 bit は `struct` で明示的にリトルエンディアンの int32 にし、各 4 バイトの
    最上位を捨てる。**`array("i")` はネイティブエンディアンなので使わない**
    （プラットフォームの前提を置かない）。負数も 2 の補数のまま正しく 24 bit に
    なる（`-1` → `ff ff ff`）。
    """
    out = bytearray()
    for start in range(0, len(values), _BLOCK):
        block = values[start : start + _BLOCK]
        if fmt is WavFormat.FLOAT32:
            out += struct.pack(f"<{len(block)}f", *block)
            continue
        ints = [int(max(-1.0, min(1.0, value)) * 0x7FFFFF) for value in block]
        raw = bytearray(struct.pack(f"<{len(ints)}i", *ints))
        del raw[3::4]
        out += raw
    return bytes(out)


# --- RIFF の組み立て -----------------------------------------------------


def _chunk(chunk_id: bytes, payload: bytes) -> bytes:
    """RIFF チャンク。奇数長には 1 バイトのパッドを足す（サイズ欄はパッドを含めない）。"""
    if len(chunk_id) != 4:
        raise ValueError(f"チャンク ID は 4 バイト: {chunk_id!r}")
    pad = b"\0" if len(payload) % 2 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + pad


def _fmt_chunk(fmt: WavFormat) -> bytes:
    # tag=1 は PCM、tag=3 は IEEE float。実機は 0x0001 だった（POC §6.2）ので
    # WAVE_FORMAT_EXTENSIBLE は使わない
    tag = 1 if fmt is WavFormat.PCM24 else 3
    width = sample_width(fmt)
    return _chunk(
        b"fmt ",
        struct.pack(
            "<HHIIHH", tag, CHANNELS, SAMPLE_RATE, byte_rate(fmt), width * CHANNELS, width * 8
        ),
    )


def build_wav(
    *,
    seconds: float,
    fmt: WavFormat = WavFormat.PCM24,
    content: Content = Content.SILENCE,
    minimal_header: bool = False,
    tone_hz: float = 220.0,
) -> bytes:
    """WAV 1 本をバイト列で返す。"""
    if seconds <= 0:
        raise ValueError(f"seconds は正の値: {seconds}")
    frames = round(SAMPLE_RATE * seconds)
    data = _encode(_samples(frames, content, tone_hz), fmt)

    chunks = [_fmt_chunk(fmt)]
    if not minimal_header:
        chunks += [
            _chunk(b"bext", b"\0" * BEXT_SIZE),
            _chunk(b"iXML", b"<BWFXML></BWFXML>".ljust(IXML_SIZE, b" ")),
            _chunk(b"cue ", b"\0" * CUE_SIZE),
            _chunk(b"PAD ", b"\0" * PAD_SIZE),
        ]
    body = b"".join(chunks) + _chunk(b"data", data)
    blob = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body

    expected = header_bytes(minimal_header=minimal_header)
    if len(blob) - len(data) != expected:
        raise AssertionError(f"ヘッダ長が {expected} にならない: {len(blob) - len(data)}")
    return blob


def write_wav(path: Path, **kwargs: object) -> Path:
    """`build_wav()` の結果を書く。親ディレクトリは作る。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_wav(**kwargs))  # type: ignore[arg-type]
    return path


# --- CLI -----------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_wav",
        description="テスト用の 48 kHz WAV を生成する（SPEC §20.2）",
    )
    parser.add_argument("out", type=Path, help="出力先")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument(
        "--format", dest="fmt", type=WavFormat, choices=list(WavFormat), default=WavFormat.PCM24
    )
    parser.add_argument("--content", type=Content, choices=list(Content), default=Content.SILENCE)
    parser.add_argument(
        "--minimal-header", action="store_true", help="fmt と data だけの 44 バイトヘッダにする"
    )
    parser.add_argument("--tone-hz", type=float, default=220.0)
    args = parser.parse_args(argv)

    blob = build_wav(
        seconds=args.seconds,
        fmt=args.fmt,
        content=args.content,
        minimal_header=args.minimal_header,
        tone_hz=args.tone_hz,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(blob)

    head = header_bytes(minimal_header=args.minimal_header)
    print(
        f"{args.out} size={len(blob)} header={head} data={len(blob) - head} "
        f"format={args.fmt} rate={SAMPLE_RATE} ch={CHANNELS} "
        f"byte_rate={byte_rate(args.fmt)} seconds={args.seconds} "
        f"sha256={hashlib.sha256(blob).hexdigest()[:16]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
