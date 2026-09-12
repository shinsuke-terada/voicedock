"""エラーコードと終了コード（SPEC §15, §17.3）。

このモジュールは **判定材料をデータとして提供するだけ**で、リトライを実行しない。
実行は #32（リトライ）と #33（status / retry）が担う。

`ErrorCode` の並びは **SPEC §15.1 の表と同じ順**に保つこと。照合しやすさのためであり、
`tests/unit/test_errors.py` が SPEC を parse して過不足を検出する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

# --- 終了コード（SPEC §17.3） --------------------------------------------
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_UNHEALTHY = 3
EXIT_DOCTOR_FATAL = 4


class Category(StrEnum):
    """SPEC §15.1 の「分類」列。"""

    CONFIG = "config"
    DEVICE = "device"
    HELPER = "helper"
    IMPORT = "import"
    AUDIO = "audio"
    TRANSCRIBE = "transcribe"
    OUTPUT = "output"
    SESSION = "session"
    LLM = "llm"
    DELETE = "delete"
    DB = "db"


class RetryPolicy(StrEnum):
    """SPEC §15.1 の「リトライ」列。

    `ATTEMPTS` だけが `retry.max_attempts`（既定 3）の対象である。
    残りの「可」は契機の到来ごとに無期限に再評価される（§15.2）。
    """

    NONE = "none"
    """不可。即時リトライしない。"""

    ATTEMPTS = "attempts"
    """可（3 回）。`retry.max_attempts` と `backoff_seconds` に従う。"""

    NEXT_POLL = "next_poll"
    """可（次回 polling）。走査の次の周回で再評価する。"""

    NEXT_CONNECT = "next_connect"
    """可（次回接続時）。デバイスが再接続されるたびに再評価する。"""

    NEXT_EVAL = "next_eval"
    """可（次回評価時）。Helper の復帰などの契機ごとに再評価する。"""


@dataclass(frozen=True)
class ErrorSpec:
    """1 つのエラーコードについて SPEC §15 が定める性質。"""

    category: Category
    retry: RetryPolicy

    auto_retry_exempt: bool = False
    """§15.2 の「自動再試行の対象外」。`FAILED` の 24 時間後の自動再投入を行わない。"""

    aborts_startup: bool = False
    """遷移先が「起動中止（終了コード 2）」である。"""


class ErrorCode(StrEnum):
    """SPEC §15.1 のエラーコード（表の並び順）。"""

    # 設定
    CONFIG_UNKNOWN_KEY = "CONFIG_UNKNOWN_KEY"
    CONFIG_INVALID_VALUE = "CONFIG_INVALID_VALUE"
    CONFIG_LOCK_MISMATCH = "CONFIG_LOCK_MISMATCH"
    # デバイス
    DEVICE_NOT_READABLE = "DEVICE_NOT_READABLE"
    DEVICE_UNSUPPORTED = "DEVICE_UNSUPPORTED"
    FILE_NOT_STABLE = "FILE_NOT_STABLE"
    # 取り込み
    DUPLICATE_CONTENT = "DUPLICATE_CONTENT"
    SOURCE_MISSING = "SOURCE_MISSING"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    # Helper / 削除キュー
    HELPER_UNAVAILABLE = "HELPER_UNAVAILABLE"
    DELETE_QUEUE_FAILED = "DELETE_QUEUE_FAILED"
    DELETE_TIMEOUT = "DELETE_TIMEOUT"
    DISK_SPACE_LOW = "DISK_SPACE_LOW"
    # 音声
    AUDIO_PROBE_FAILED = "AUDIO_PROBE_FAILED"
    IMPORT_FAILED = "IMPORT_FAILED"
    NORMALIZE_VERIFY_FAILED = "NORMALIZE_VERIFY_FAILED"
    # 文字起こし
    WHISPER_EXEC_MISSING = "WHISPER_EXEC_MISSING"
    WHISPER_MODEL_MISSING = "WHISPER_MODEL_MISSING"
    WHISPER_FAILED = "WHISPER_FAILED"
    WHISPER_TIMEOUT = "WHISPER_TIMEOUT"
    NO_SPEECH_DETECTED = "NO_SPEECH_DETECTED"
    # 出力（Raw）
    OBSIDIAN_RAW_WRITE_FAILED = "OBSIDIAN_RAW_WRITE_FAILED"
    OBSIDIAN_RAW_VERIFY_FAILED = "OBSIDIAN_RAW_VERIFY_FAILED"
    # セッション / LLM
    SESSION_MERGE_FAILED = "SESSION_MERGE_FAILED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_FAILED = "LLM_FAILED"
    LLM_INVALID_JSON = "LLM_INVALID_JSON"
    # 出力（Daily）
    OBSIDIAN_NOT_FOUND = "OBSIDIAN_NOT_FOUND"
    OBSIDIAN_WRITE_FAILED = "OBSIDIAN_WRITE_FAILED"
    OBSIDIAN_VERIFY_FAILED = "OBSIDIAN_VERIFY_FAILED"
    # 削除
    SOURCE_IDENTITY_MISMATCH = "SOURCE_IDENTITY_MISMATCH"
    SOURCE_DELETE_FAILED = "SOURCE_DELETE_FAILED"
    LOCAL_DELETE_FAILED = "LOCAL_DELETE_FAILED"
    # DB
    DB_ERROR = "DB_ERROR"


_C = Category
_R = RetryPolicy

ERRORS: Mapping[ErrorCode, ErrorSpec] = {
    ErrorCode.CONFIG_UNKNOWN_KEY: ErrorSpec(
        _C.CONFIG, _R.NONE, auto_retry_exempt=True, aborts_startup=True
    ),
    ErrorCode.CONFIG_INVALID_VALUE: ErrorSpec(
        _C.CONFIG, _R.NONE, auto_retry_exempt=True, aborts_startup=True
    ),
    ErrorCode.CONFIG_LOCK_MISMATCH: ErrorSpec(
        _C.CONFIG, _R.NONE, auto_retry_exempt=True, aborts_startup=True
    ),
    ErrorCode.DEVICE_NOT_READABLE: ErrorSpec(_C.DEVICE, _R.NEXT_POLL),
    ErrorCode.DEVICE_UNSUPPORTED: ErrorSpec(_C.DEVICE, _R.NONE),
    ErrorCode.FILE_NOT_STABLE: ErrorSpec(_C.DEVICE, _R.NEXT_POLL),
    ErrorCode.DUPLICATE_CONTENT: ErrorSpec(_C.IMPORT, _R.NONE, auto_retry_exempt=True),
    ErrorCode.SOURCE_MISSING: ErrorSpec(_C.IMPORT, _R.NONE, auto_retry_exempt=True),
    ErrorCode.SOURCE_HASH_MISMATCH: ErrorSpec(_C.IMPORT, _R.ATTEMPTS),
    ErrorCode.HELPER_UNAVAILABLE: ErrorSpec(_C.HELPER, _R.NEXT_EVAL),
    ErrorCode.DELETE_QUEUE_FAILED: ErrorSpec(_C.DELETE, _R.NEXT_CONNECT),
    ErrorCode.DELETE_TIMEOUT: ErrorSpec(_C.DELETE, _R.NEXT_CONNECT),
    ErrorCode.DISK_SPACE_LOW: ErrorSpec(_C.IMPORT, _R.NEXT_POLL),
    ErrorCode.AUDIO_PROBE_FAILED: ErrorSpec(_C.AUDIO, _R.ATTEMPTS),
    ErrorCode.IMPORT_FAILED: ErrorSpec(_C.AUDIO, _R.ATTEMPTS),
    ErrorCode.NORMALIZE_VERIFY_FAILED: ErrorSpec(_C.AUDIO, _R.ATTEMPTS),
    ErrorCode.WHISPER_EXEC_MISSING: ErrorSpec(_C.TRANSCRIBE, _R.NONE, aborts_startup=True),
    ErrorCode.WHISPER_MODEL_MISSING: ErrorSpec(_C.TRANSCRIBE, _R.NONE, aborts_startup=True),
    ErrorCode.WHISPER_FAILED: ErrorSpec(_C.TRANSCRIBE, _R.ATTEMPTS),
    ErrorCode.WHISPER_TIMEOUT: ErrorSpec(_C.TRANSCRIBE, _R.ATTEMPTS),
    ErrorCode.NO_SPEECH_DETECTED: ErrorSpec(_C.TRANSCRIBE, _R.NONE, auto_retry_exempt=True),
    ErrorCode.OBSIDIAN_RAW_WRITE_FAILED: ErrorSpec(_C.OUTPUT, _R.ATTEMPTS),
    ErrorCode.OBSIDIAN_RAW_VERIFY_FAILED: ErrorSpec(_C.OUTPUT, _R.ATTEMPTS),
    ErrorCode.SESSION_MERGE_FAILED: ErrorSpec(_C.SESSION, _R.ATTEMPTS),
    ErrorCode.LLM_UNAVAILABLE: ErrorSpec(_C.LLM, _R.ATTEMPTS),
    ErrorCode.LLM_FAILED: ErrorSpec(_C.LLM, _R.ATTEMPTS),
    # 即時リトライは repair 1 回で打ち切るが、§15.2 の対象外リストに入っていないため
    # FAILED として残った後 24 時間で自動再投入される。即時リトライの可否と
    # 自動再投入の可否は別の概念である（混同すると LLM の一時障害から復帰できない）。
    ErrorCode.LLM_INVALID_JSON: ErrorSpec(_C.LLM, _R.NONE),
    ErrorCode.OBSIDIAN_NOT_FOUND: ErrorSpec(_C.OUTPUT, _R.ATTEMPTS),
    ErrorCode.OBSIDIAN_WRITE_FAILED: ErrorSpec(_C.OUTPUT, _R.ATTEMPTS),
    ErrorCode.OBSIDIAN_VERIFY_FAILED: ErrorSpec(_C.OUTPUT, _R.ATTEMPTS),
    ErrorCode.SOURCE_IDENTITY_MISMATCH: ErrorSpec(_C.DELETE, _R.NEXT_CONNECT),
    ErrorCode.SOURCE_DELETE_FAILED: ErrorSpec(_C.DELETE, _R.NEXT_CONNECT),
    ErrorCode.LOCAL_DELETE_FAILED: ErrorSpec(_C.DELETE, _R.ATTEMPTS),
    ErrorCode.DB_ERROR: ErrorSpec(_C.DB, _R.ATTEMPTS),
}


def spec_for(code: ErrorCode) -> ErrorSpec:
    """エラーコードの性質を返す。"""
    return ERRORS[code]


def is_retryable(code: ErrorCode) -> bool:
    """即時または契機到来時にリトライしてよいか（§15.1 の「リトライ」列が「可」）。"""
    return ERRORS[code].retry is not RetryPolicy.NONE


def counts_against_max_attempts(code: ErrorCode) -> bool:
    """`retry.max_attempts`（既定 3）で打ち切る対象か。

    `ATTEMPTS` のときだけ真。これにより §15.2 の
    「`HELPER_UNAVAILABLE` と `DELETE_TIMEOUT` は `max_attempts` の対象外」
    「`SOURCE_DELETE_PENDING` は対象外」が自動的に満たされる。
    """
    return ERRORS[code].retry is RetryPolicy.ATTEMPTS


def is_auto_retryable(code: ErrorCode) -> bool:
    """`FAILED` の `auto_retry_failed_after_hours` 経過後の自動再投入の対象か（§15.2）。

    **`retry` 列を見ない。**§15.1 の「可 / 不可」は*即時*リトライの可否であり、
    24 時間後の自動再投入は §15.2 が名指しする対象外リストだけで決まる。
    この 2 つは別の軸である。

    この区別は `LLM_INVALID_JSON` で効く。即時リトライは repair 1 回で打ち切る（`不可`）が、
    対象外リストに入っていないため翌日には自動で再投入される。`retry` 列で判定すると
    LLM の一時的な出力崩れから永久に復帰できなくなる。
    """
    return not ERRORS[code].auto_retry_exempt


def aborts_startup(code: ErrorCode) -> bool:
    """起動を中止すべきか（終了コード `EXIT_CONFIG`）。"""
    return ERRORS[code].aborts_startup
