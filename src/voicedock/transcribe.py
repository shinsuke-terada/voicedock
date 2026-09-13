"""whisper.cpp による文字起こし（SPEC §10.6, §11.1(2)）。

**whisper.cpp の知識はこのモジュールの中だけに閉じる。**`pipeline.py` は `whisper-cli` の
引数もモデルのパスも知らない（§11.1(2)）。CPU 実行の速度が実用に満たず Mac ネイティブ実行へ
差し替えるとき、変更するのはここの中身だけである。

**`subprocess` は argv リストで渡す。`shell=True` と文字列連結は使わない**（§14.4）。
ファイル名は外部入力であり、デバイス上のファイル名は利用者が付けたものである。

**状態遷移も DB 更新も行わない**（#21）。失敗は戻り値で返す（例外を投げない）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from voicedock import paths
from voicedock.config import Config, TranscriptionConfig
from voicedock.errors import ErrorCode
from voicedock.paths import PartKey, StagingPath

MAX_THREADS: Final = 8
"""`threads: 0` のときの上限（§10.6）。"""

STDERR_TAIL_CHARS: Final = 1000
"""`error_message` に残す `stderr` の末尾長（§10.6）。"""

RAW_BASENAME: Final = "whisper"
"""生 JSON の一時ベース名。**staging に置く**（§10.6 は「生 JSON は残さない」）。"""

SCHEMA_KEYS: Final[frozenset[str]] = frozenset(
    {"partkey", "language", "duration_seconds", "started_at", "text", "segments"}
)
"""§10.6 の正規化形式の必須キー。**冪等判定がこれで形を確かめる。**"""


# --- 正規化形式（§10.6 / §11.2） ----------------------------------------


@dataclass(frozen=True)
class TranscriptSegment:
    """1 区間。`start` / `end` は **Part の先頭からの相対秒**（§10.6）。"""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """§10.6 の正規化形式。**これが唯一の永続スキーマである。**

    whisper-cli が `-oj` で出した生 JSON は読み込み後にこの形へ変換し、**生 JSON は残さない。**
    同じディレクトリに 2 種類のスキーマが混在すると、**§9.4 の冪等判定がどちらを読むか
    決まらなくなる。**
    """

    partkey: str
    language: str
    duration_seconds: float | None
    started_at: str
    text: str
    segments: tuple[TranscriptSegment, ...]

    @property
    def speech_seconds(self) -> float:
        """発話の合計秒。`speech_ratio` の分子（§16.2）。"""
        return sum(max(0.0, s.end - s.start) for s in self.segments)

    def to_document(self) -> dict[str, object]:
        return {
            "partkey": self.partkey,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "started_at": self.started_at,
            "text": self.text,
            "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in self.segments],
        }


@dataclass(frozen=True)
class TranscribeResult:
    """`transcribe()` の結果。**状態遷移は呼び手が決める**（#21）。"""

    transcript: Transcript | None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    elapsed_seconds: float = 0.0
    reused: bool = False
    """冪等判定で whisper を起動しなかった（§10.6）。"""

    @property
    def ok(self) -> bool:
        return self.transcript is not None and self.error_code is None


@dataclass(frozen=True)
class Availability:
    """`is_available()` の結果。doctor D-7 / H-* が使う。"""

    ok: bool
    detail: str
    vad_supported: bool | None = None
    """`--help` に `--vad` が在るか。**バージョン文字列では判定しない**（§10.6 の注記）。"""


# --- argv の組み立て（§10.6） -------------------------------------------


def resolve_threads(configured: int) -> int:
    """`transcription.threads`。`0` なら `min(os.cpu_count(), 8)`（§10.6）。"""
    if configured > 0:
        return configured
    return min(os.cpu_count() or 1, MAX_THREADS)


def timeout_for(duration_seconds: float | None, cfg: TranscriptionConfig) -> int:
    """`clamp(duration × timeout_factor, min, max)`（§10.6）。

    **`duration_seconds` が `None` なら `max_timeout_seconds` を使う。**ffprobe 失敗で
    NULL のまま続行するので（§10.5）実際に存在する。`min` 側に倒すと**長い録音が必ず
    タイムアウトする。**
    """
    if duration_seconds is None:
        return cfg.max_timeout_seconds
    scaled = duration_seconds * cfg.timeout_factor
    return int(min(max(scaled, cfg.min_timeout_seconds), cfg.max_timeout_seconds))


def build_argv(
    cfg: TranscriptionConfig, *, audio: Path, out_base: Path, threads: int | None = None
) -> list[str]:
    """§10.6 のコマンドラインを組み立てる。**純関数である。**

    埋め込みで組み立てず独立させるのは、**§10.6 の bash ブロックと逐語照合する**ためである
    （`tests/unit/test_transcribe.py`）。埋め込むと、SPEC のフラグが 1 つ変わっても
    何も落ちない。

    `vad.enabled` が偽なら **VAD の 6 フラグを 1 つも渡さない。**
    """
    argv = [
        str(cfg.executable),
        "-m",
        str(cfg.model),
        "-f",
        str(audio),
        "-l",
        cfg.language,
        "-t",
        str(resolve_threads(cfg.threads) if threads is None else threads),
    ]
    if cfg.vad.enabled:
        argv += [
            "--vad",
            "--vad-model",
            str(cfg.vad.model),
            "--vad-threshold",
            _number(cfg.vad.threshold),
            "--vad-min-speech-duration-ms",
            str(cfg.vad.min_speech_duration_ms),
            "--vad-min-silence-duration-ms",
            str(cfg.vad.min_silence_duration_ms),
            "--vad-speech-pad-ms",
            str(cfg.vad.speech_pad_ms),
        ]
    return [*argv, "-oj", "-of", str(out_base), "-np"]


def _number(value: float) -> str:
    """`0.5` を `"0.5"`、`1.0` を `"1"` にする（SPEC の書式に合わせる）。"""
    return str(int(value)) if value == int(value) else str(value)


# --- 実行可能性（§19.2 D-7〜D-9） ---------------------------------------


def is_available(cfg: TranscriptionConfig, *, timeout_seconds: int = 30) -> Availability:
    """`whisper-cli` が実行でき、`--vad` を持つか（D-7）。**例外を投げない。**"""
    executable = Path(cfg.executable)
    if not executable.is_file():
        return Availability(ok=False, detail=f"{executable} が存在しません")
    try:
        completed = subprocess.run(  # noqa: S603 - argv は固定、パスは設定検証済み
            [str(executable), "--help"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Availability(ok=False, detail=f"{type(exc).__name__}: {exc}")

    output = completed.stdout + completed.stderr
    # **`--help` の終了コードを見ない。**whisper-cli は 0 を返すが、版によって 1 を返す
    # 実装もある。判定したいのは「起動でき、VAD フラグを持つか」である
    if "--vad" not in output:
        return Availability(ok=True, detail=str(executable), vad_supported=False)
    return Availability(ok=True, detail=str(executable), vad_supported=True)


# --- 本体（§10.6） ------------------------------------------------------


def transcribe(
    audio_path: StagingPath,
    *,
    partkey: PartKey,
    started_at: str,
    duration_seconds: float | None,
    cfg: Config,
) -> TranscribeResult:
    """16 kHz WAV を文字起こしし、正規化形式で保存する（§10.6）。

    **例外を投げない。**失敗は `TranscribeResult.error_code` で返す。状態遷移は #21 の
    責務であり、ここは「何が起きたか」を返すだけにする。

    **冪等性**: 出力 JSON が在り、正規化スキーマとしてパースでき `text` が `min_chars`
    以上なら**再実行しない**（§10.6）。
    """
    settings = cfg.transcription
    target = paths.transcript_path_for(partkey)

    existing = _load(target)
    if existing is not None and len(existing.text) >= settings.min_chars:
        return TranscribeResult(transcript=existing, reused=True)

    raw_base = Path(audio_path).parent / RAW_BASENAME
    raw_json = raw_base.with_suffix(".json")
    argv = build_argv(settings, audio=Path(audio_path), out_base=raw_base)
    timeout = timeout_for(duration_seconds, settings)

    started = time.monotonic()
    outcome = _run(argv, timeout_seconds=timeout)
    elapsed = time.monotonic() - started

    if outcome.error_code is not None:
        _discard(raw_json)
        return TranscribeResult(
            transcript=None,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
            elapsed_seconds=elapsed,
        )

    document = _read_json(raw_json)
    _discard(raw_json)
    if document is None:
        return TranscribeResult(
            transcript=None,
            error_code=ErrorCode.WHISPER_FAILED,
            error_message=f"生 JSON を読めません: {raw_json}",
            elapsed_seconds=elapsed,
        )

    transcript = normalize(
        document,
        partkey=partkey,
        started_at=started_at,
        duration_seconds=duration_seconds,
        fallback_language=settings.language,
    )
    _write(target, transcript)

    if len(transcript.text) < settings.min_chars:
        # **失敗ではない**（§10.6）。呼び手が SKIPPED へ進める（#21）
        return TranscribeResult(
            transcript=transcript,
            error_code=ErrorCode.NO_SPEECH_DETECTED,
            error_message=f"{len(transcript.text)} 文字（min_chars={settings.min_chars}）",
            elapsed_seconds=elapsed,
        )
    return TranscribeResult(transcript=transcript, elapsed_seconds=elapsed)


@dataclass(frozen=True)
class _Outcome:
    error_code: ErrorCode | None = None
    error_message: str | None = None


def _run(argv: list[str], *, timeout_seconds: int) -> _Outcome:
    """whisper を 1 回起動する。**プロセスグループごと落とせるようにする**（§10.6）。

    `start_new_session=True` で新しいプロセスグループを作り、タイムアウト時に
    `os.killpg()` を撃つ。`subprocess.run(timeout=)` の既定は直接の子だけを殺すので、
    **whisper が子を持つ実装に変わった日に孤児が残る。**
    """
    try:
        process = subprocess.Popen(  # noqa: S603 - argv はリスト。shell=True を使わない
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    except OSError as exc:
        return _Outcome(ErrorCode.WHISPER_EXEC_MISSING, f"{type(exc).__name__}: {exc}")

    try:
        _stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _killpg(process)
        process.communicate()
        return _Outcome(ErrorCode.WHISPER_TIMEOUT, f"{timeout_seconds} 秒を超えました")

    if process.returncode != 0:
        tail = (stderr or "")[-STDERR_TAIL_CHARS:]
        return _Outcome(ErrorCode.WHISPER_FAILED, f"終了コード {process.returncode}: {tail}")
    return _Outcome()


def _killpg(process: subprocess.Popen[str]) -> None:
    """プロセスグループへ `SIGKILL`。失敗しても続ける（既に終わっている場合がある）。"""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


# --- 変換と永続化（§10.6） ----------------------------------------------


def normalize(
    document: object,
    *,
    partkey: PartKey,
    started_at: str,
    duration_seconds: float | None,
    fallback_language: str,
) -> Transcript:
    """whisper の生 JSON を §10.6 の正規化形式へ変換する。

    **`offsets` はミリ秒である。**whisper.cpp は
    `{"offsets": {"from": 0, "to": 3200}}` を返す。`timestamps`（`"00:00:03,200"`）は
    同じ情報の文字列表現なので**読まない** — 解析が増えるだけである。

    壊れた要素は飛ばす。**1 区間の不良で Part 全体を失わない**（§1.3）。
    """
    body = document if isinstance(document, dict) else {}
    result = body.get("result")
    language = result.get("language") if isinstance(result, dict) else None

    segments: list[TranscriptSegment] = []
    entries = body.get("transcription")
    for entry in entries if isinstance(entries, list) else []:
        segment = _segment(entry)
        if segment is not None:
            segments.append(segment)

    return Transcript(
        partkey=partkey,
        language=language if isinstance(language, str) and language else fallback_language,
        duration_seconds=duration_seconds,
        started_at=started_at,
        text="".join(s.text for s in segments).strip(),
        segments=tuple(segments),
    )


def _segment(entry: object) -> TranscriptSegment | None:
    if not isinstance(entry, dict):
        return None
    offsets = entry.get("offsets")
    text = entry.get("text")
    if not isinstance(offsets, dict) or not isinstance(text, str):
        return None
    start = _seconds(offsets.get("from"))
    end = _seconds(offsets.get("to"))
    if start is None or end is None:
        return None
    stripped = text.strip()
    return None if not stripped else TranscriptSegment(start=start, end=end, text=stripped)


def _seconds(millis: object) -> float | None:
    """ミリ秒を秒へ。`bool` は数値として採らない。"""
    if isinstance(millis, bool) or not isinstance(millis, int | float):
        return None
    return round(millis / 1000.0, 3)


def _write(target: Path, transcript: Transcript) -> None:
    """正規化形式を書く。**`/data/transcripts/parts/` には生 JSON を置かない**（§10.6）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(transcript.to_document(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_transcript(key: PartKey) -> Transcript | None:
    """保存済みの正規化形式を読む（§10.6 / §9.4）。

    **パスを呼び手に組み立てさせない。**`paths.transcript_path_for()` が唯一の出所で、
    `key_slug()` を通す規約（v5.0→v5.1 の変更 M-7）をここで閉じる。
    """
    return _load(paths.transcript_path_for(key))


def _load(path: Path) -> Transcript | None:
    """保存済みの正規化形式を読む。**形が違えば `None`**（冪等判定。§10.6 / §9.4）。"""
    document = _read_json(path)
    if not isinstance(document, dict) or not document.keys() >= SCHEMA_KEYS:
        return None
    entries = document.get("segments")
    if not isinstance(entries, list):
        return None
    segments: list[TranscriptSegment] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        start, end, text = entry.get("start"), entry.get("end"), entry.get("text")
        if not isinstance(start, int | float) or not isinstance(end, int | float):
            return None
        if not isinstance(text, str):
            return None
        segments.append(TranscriptSegment(start=float(start), end=float(end), text=text))

    text = document.get("text")
    started_at = document.get("started_at")
    language = document.get("language")
    duration = document.get("duration_seconds")
    if not (isinstance(text, str) and isinstance(started_at, str) and isinstance(language, str)):
        return None
    if duration is not None and not isinstance(duration, int | float):
        return None
    return Transcript(
        partkey=str(document.get("partkey", "")),
        language=language,
        duration_seconds=None if duration is None else float(duration),
        started_at=started_at,
        text=text,
        segments=tuple(segments),
    )


def _read_json(path: Path) -> object | None:
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed


def _discard(path: Path) -> None:
    """生 JSON と部分出力を消す。**`paths.safe_unlink_staging()` を通す**（§14.4 N-10）。"""
    try:
        paths.safe_unlink_staging(StagingPath(path), missing_ok=True)
    except (OSError, ValueError):
        return


def metrics(
    result: TranscribeResult, *, duration_seconds: float | None
) -> dict[str, str | int | float | bool | None]:
    """`transcription_completed` のフィールド（§16.2）。

    `rtf`（real-time factor）と `speech_ratio` は `duration_seconds` が `None` だと
    求まらない。**0 で割らず `None` を返す**（ログの値としてそのまま出せる）。
    """
    transcript = result.transcript
    chars = 0 if transcript is None else len(transcript.text)
    speech = 0.0 if transcript is None else transcript.speech_seconds
    total = duration_seconds if duration_seconds is not None and duration_seconds > 0 else None
    return {
        "elapsed_s": round(result.elapsed_seconds, 1),
        "chars": chars,
        "rtf": round(result.elapsed_seconds / total, 3) if total is not None else None,
        "speech_ratio": round(speech / total, 3) if total is not None else None,
    }


def started_at_of(moment: datetime) -> str:
    """`started_at` の書式（§10.6 の正規化形式）。秒までの ISO 8601。"""
    return moment.isoformat(timespec="seconds")
