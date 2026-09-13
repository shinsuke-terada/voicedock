"""ffprobe による音声形式の取得（SPEC §10.5(1)）。

**このモジュールは `probe()` だけを持つ。**16 kHz への変換・SHA-256 のストリーム算出・
空き容量の判定は #18 が同じファイルへ足す。

**`probe()` は例外を投げない。**§10.5 は「`max_attempts` 回リトライし、それでも失敗したら
`duration_seconds = NULL` のまま処理を続行する」と規定している。例外にすると全呼び手が
try/except を持つことになり、**1 箇所忘れた時点でその Part の記録が失われる**
（§1.3 の優先順位 1 に反する）。失敗は戻り値で表す。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FFPROBE_ARGS: Final[tuple[str, ...]] = (
    "-v",
    "error",
    "-select_streams",
    "a:0",
    "-show_entries",
    "format=duration:stream=sample_rate,channels,sample_fmt,codec_name",
    "-of",
    "json",
)
"""§10.5(1) のコマンドライン。**SPEC のコードブロックと 1 対 1 に保つこと。**

`tests/unit/test_probe.py` が §10.5 のコードブロックから読み取って突き合わせる。
"""

MAX_ERROR_CHARS: Final = 200
"""`stderr` の保持長。`db.truncate()` と同じ上限（§8.4）。"""


@dataclass(frozen=True)
class AudioFormat:
    """ffprobe が返した形式（§10.5(1)）。"""

    duration_seconds: float | None
    """**`None` でも probe は成功扱いである。**`format.duration` は壊れた WAV や
    ストリーミング由来のファイルで `N/A` になりうるが、sample_rate などは取れている。
    """

    sample_rate: int | None
    channels: int | None
    sample_fmt: str | None
    codec_name: str | None


@dataclass(frozen=True)
class ProbeResult:
    """`probe()` の結果。**失敗も戻り値で表す**（例外を投げない）。"""

    format: AudioFormat | None
    """`None` は「`max_attempts` 回試して取れなかった」。"""

    attempts: int
    error: str | None

    @property
    def ok(self) -> bool:
        return self.format is not None

    @property
    def duration_seconds(self) -> float | None:
        """取れなかった場合も `None`。**呼び手が `ok` と duration を区別せずに済む。**"""
        return None if self.format is None else self.format.duration_seconds


def probe(
    path: Path,
    *,
    ffprobe: Path,
    timeout_seconds: int,
    max_attempts: int = 1,
) -> ProbeResult:
    """音声ファイルの形式を取得する（§10.5(1)）。**例外を投げない。**

    v4.0 以降は inbox（ローカルディスク）を読むため、USB 越しの往復が無い。

    Args:
        path: **inbox 上の**ファイル。デバイス上のパスを渡してはならない（§14.4 N-3）。
        ffprobe: `audio.ffprobe`。
        timeout_seconds: `audio.ffprobe_timeout_seconds`。
        max_attempts: `retry.max_attempts`。`AUDIO_PROBE_FAILED` は再試行可（§15.1）。
    """
    attempts = 0
    error = "未実行"
    for attempts in range(1, max(1, max_attempts) + 1):
        completed, error = _run(path, ffprobe=ffprobe, timeout_seconds=timeout_seconds)
        if completed is None:
            continue
        parsed = parse_output(completed)
        if parsed is None:
            error = "ffprobe の出力を解釈できません"
            continue
        return ProbeResult(format=parsed, attempts=attempts, error=None)
    return ProbeResult(format=None, attempts=attempts, error=error)


def _run(path: Path, *, ffprobe: Path, timeout_seconds: int) -> tuple[str | None, str]:
    """ffprobe を 1 回だけ起動する。`(stdout, エラー説明)` を返す。

    **`stdin` を `DEVNULL` にする。**常駐サービスの標準入力を子プロセスが奪わないようにする。
    §10.5 の「`-nostdin` を付けてはならない」は **ffmpeg が `pipe:0` から WAV を受け取る**
    ための規定であり、ファイルパスを読む ffprobe には当たらない。
    """
    try:
        completed = subprocess.run(  # noqa: S603 - 引数は固定、path は呼び手が検証済み
            [str(ffprobe), *FFPROBE_ARGS, str(path)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return None, f"{timeout_seconds} 秒で応答しません"
    except OSError as exc:  # ffprobe が無い / 実行できない
        return None, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:MAX_ERROR_CHARS]
        return None, f"終了コード {completed.returncode}: {detail}"
    return completed.stdout, ""


def parse_output(stdout: str) -> AudioFormat | None:
    """ffprobe の JSON 出力を読む。解釈できなければ `None`。

    **音声ストリームが 1 本も無いものは失敗とする。**#18 の検証（16000 Hz / 1 ch / `s16`）が
    成り立たず、そのまま進めば ffmpeg が必ず落ちる。ここで失敗にしておけば
    `duration_seconds = NULL` として記録だけは残る。
    """
    try:
        document = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None

    streams = document.get("streams")
    if not isinstance(streams, list) or not streams:
        return None
    stream = streams[0]
    if not isinstance(stream, dict):
        return None

    container = document.get("format")
    duration = container.get("duration") if isinstance(container, dict) else None

    return AudioFormat(
        duration_seconds=_as_float(duration),
        sample_rate=_as_int(stream.get("sample_rate")),
        channels=_as_int(stream.get("channels")),
        sample_fmt=_as_str(stream.get("sample_fmt")),
        codec_name=_as_str(stream.get("codec_name")),
    )


def _as_float(value: object) -> float | None:
    """**ffprobe は数値を文字列で返す**（`"1.500000"`）。`"N/A"` と欠落は `None`。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    # 負の duration は壊れたヘッダ。`ended_at` が開始より前になるので採らない
    return None if parsed < 0 else parsed


def _as_int(value: object) -> int | None:
    parsed = _as_float(value)
    return None if parsed is None else int(parsed)


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
