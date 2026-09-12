"""構造化ログ（SPEC §16）。

Python の `logging` は使わず、ストリームへ直接書く。出力先は stdout 1 本で（§16.1）、
ハンドラ・フィルタ・親ロガーといった設定可能な層を挟むと、§16.3 の遮断を
**すり抜ける経路**が生まれるためである。テストではストリームを差し替え、
出力を 1 バイト単位で固定する。

このモジュールは `config` を import しない（循環を作らない）。`fmt` / `level` / `tz` /
`unsafe_log_content` は引数で受け取る。

`_EVENT_ORDER` の並びは **SPEC §16.4 の行の並びと同じ**に保つこと。`tests/unit/test_log.py` が
SPEC を parse して過不足と並びを検出する。
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from typing import IO, Final, Literal

LogValue = str | int | float | bool | None
"""ログの値に許す型。これ以外は `TypeError`（§16.3 の遮断の土台）。"""

LogFormat = Literal["text", "json"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

_LEVEL_ORDER: Final[Mapping[str, int]] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
}

# SPEC §16.4 の行の区切りを保つため整形を止める（照合しやすさのため）
# fmt: off
_EVENT_ORDER: Final[tuple[str, ...]] = (
    "service_started", "service_stopping", "recovery_completed",
    "device_detected", "device_lost", "device_unreadable", "device_excluded",
    "helper_heartbeat_stale", "helper_recovered", "remount_readonly_failed",
    "inbox_part_found", "inbox_meta_missing", "inbox_source_deleted", "source_hash_mismatch",
    "delete_requested", "delete_result_received", "delete_timeout", "delete_queue_failed",
    "deep_scan_started", "deep_scan_completed", "deep_scan_skipped",
    "part_discovered", "part_skipped", "unparsable_filename", "file_not_stable",
    "normalize_started", "normalize_completed", "normalize_failed", "duplicate_content",
    "transcription_started", "transcription_completed", "transcription_failed",
    "transcription_timeout", "no_speech_detected",
    "raw_note_saved", "raw_note_write_failed", "raw_note_verify_failed",
    "session_opened", "session_ready", "session_reopened", "session_merged",
    "session_merge_failed", "session_empty",
    "llm_started", "llm_completed", "llm_invalid_json", "llm_repair_attempted",
    "llm_unavailable",
    "wikilink_index_built", "wikilink_skipped",
    "obsidian_saved", "obsidian_write_failed", "obsidian_verify_failed",
    "source_delete_started", "source_deleted", "source_delete_failed",
    "source_identity_mismatch", "source_delete_pending", "source_delete_skipped",
    "staging_deleted", "staging_delete_failed",
    "session_completed", "part_completed",
    "disk_space_low", "retry_scheduled", "auto_retry_queued", "retry_exhausted",
)
"""SPEC §16.4 の並び。`tests/unit/test_log.py` が SPEC と行の並びまで突き合わせる。"""
# fmt: on

EVENTS: Final[frozenset[str]] = frozenset(_EVENT_ORDER)

CONTENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "text",
        "transcript",
        "summary",
        "content",
        "body",
        "prompt",
        "title",
        "tags",
        "key_points",
        "tasks",
        "decisions",
        "ideas",
        "segments",
        "filename",
        "note_name",
    }
)
"""本文を運ぶキー名（§16.3）。値は常に伏せる。

`title` と `filename` を含めるのは、§16.3 が「**ファイル名も内容を含み得る**」と明記して
いるためである。v3.0 はノートのファイル名に LLM 生成タイトル（＝会話内容）を含め、
それをログへ出していた。
"""

MAX_VALUE_CHARS: Final = 200
"""この長さを超える文字列は、キー名によらず常に伏せる。

キー名のデニーリストは「名前が分かっている漏洩」しか防げない。将来
`detail=<transcript 全文>` のような呼び出しが入っても、長さで止まる。
"""

REDACTED: Final = "<redacted>"

_RESERVED_FIELDS: Final[frozenset[str]] = frozenset({"ts", "level", "event"})
"""フィールド名に使えない名前。

json 形式ではこの 3 つが先頭のキーになるため、同名のフィールドを許すと
`level` が実際のレベルと違う値で出るような行を作れてしまう。
"""

# 引用せずにそのまま出せる文字列。印字可能 ASCII のみで、空白・`=`・`"` を含まないもの。
_PLAIN_VALUE = re.compile(r"^[\x21-\x7e]+$")


class Logger:
    """イベント名と scalar フィールドだけを受け取るロガー。

    `get_logger()` から得る。直接構築してもよいが、引数はすべてキーワードである。
    """

    def __init__(
        self,
        *,
        fmt: LogFormat = "text",
        level: LogLevel = "INFO",
        tz: tzinfo = UTC,
        unsafe_log_content: bool = False,
        stream: IO[str] | None = None,
    ) -> None:
        if fmt not in ("text", "json"):
            raise ValueError(f"未知のログ形式: {fmt!r}")
        if level not in _LEVEL_ORDER:
            raise ValueError(f"未知のログレベル: {level!r}")
        self._fmt = fmt
        self._threshold = _LEVEL_ORDER[level]
        self._tz = tz
        self._unsafe = unsafe_log_content
        self._stream = stream

    # --- 公開 API --------------------------------------------------------

    def debug(self, event: str, **fields: LogValue) -> None:
        self._emit("DEBUG", event, fields)

    def info(self, event: str, **fields: LogValue) -> None:
        self._emit("INFO", event, fields)

    def warning(self, event: str, **fields: LogValue) -> None:
        self._emit("WARNING", event, fields)

    def error(self, event: str, **fields: LogValue) -> None:
        self._emit("ERROR", event, fields)

    # --- 内部 ------------------------------------------------------------

    def _emit(self, level: LogLevel, event: str, fields: Mapping[str, LogValue]) -> None:
        # 検証はレベルフィルタより先に行う。イベント名の typo と値の型違反を
        # 「そのレベルが出力されるかどうか」に依存させないため。
        if event not in EVENTS:
            raise ValueError(
                f"未登録のイベント名: {event!r}。SPEC §16.4 の一覧と log.EVENTS に追加すること"
            )
        reserved = _RESERVED_FIELDS & fields.keys()
        if reserved:
            raise ValueError(
                f"フィールド名に使えない名前: {sorted(reserved)}。json 形式の先頭キーと衝突する"
            )
        safe = {k: self._sanitize(level, k, v) for k, v in fields.items()}

        if _LEVEL_ORDER[level] < self._threshold:
            return

        ts = datetime.now(self._tz).isoformat(timespec="seconds")
        line = (
            self._format_json(ts, level, event, safe)
            if self._fmt == "json"
            else self._format_text(ts, level, event, safe)
        )
        # stdout は遅延評価する。get_logger() の時点で束縛すると、
        # capsys などによる差し替えが効かなくなる。
        out = self._stream if self._stream is not None else sys.stdout
        out.write(line + "\n")
        out.flush()

    def _sanitize(self, level: LogLevel, key: str, value: LogValue) -> LogValue:
        if value is not None and not isinstance(value, str | int | float | bool):
            raise TypeError(
                f"ログの値は str / int / float / bool / None のみ: {key}={type(value).__name__}"
            )
        # §16.3: unsafe_log_content かつ DEBUG のときだけ本文の出力を許す
        if self._unsafe and level == "DEBUG":
            return value
        if key in CONTENT_FIELDS:
            return REDACTED
        if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
            return REDACTED
        return value

    def _format_text(
        self, ts: str, level: LogLevel, event: str, fields: Mapping[str, LogValue]
    ) -> str:
        parts = [f"{ts} {level:<5} {event}"]
        parts += [f"{k}={_text_value(v)}" for k, v in fields.items()]
        return " ".join(parts)

    def _format_json(
        self, ts: str, level: LogLevel, event: str, fields: Mapping[str, LogValue]
    ) -> str:
        payload: dict[str, LogValue] = {"ts": ts, "level": level, "event": event}
        payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _text_value(value: LogValue) -> str:
    """text 形式の値 1 つを文字列にする。

    bool と None は json 形式と同じ綴りにする（`true` / `false` / `null`）。
    text と json で同じイベントを grep したときに揃うため。
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if _PLAIN_VALUE.match(value) and '"' not in value and "=" not in value:
            return value
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def get_logger(
    *,
    fmt: LogFormat = "text",
    level: LogLevel = "INFO",
    tz: tzinfo = UTC,
    unsafe_log_content: bool = False,
    stream: IO[str] | None = None,
) -> Logger:
    """`Logger` を作る（§16.2 の形式、§16.3 の遮断つき）。

    `stream` は主にテスト用。`None` なら出力のたびに `sys.stdout` を見る。
    """
    return Logger(fmt=fmt, level=level, tz=tz, unsafe_log_content=unsafe_log_content, stream=stream)
