"""`voicedock.errors` が SPEC §15 / §17.3 と一致することを固定する。"""

from __future__ import annotations

import pytest

from tests.spec_sync import (
    spec_error_categories,
    spec_error_codes,
    spec_exit_codes,
    spec_startup_aborting_codes,
)
from voicedock import errors
from voicedock.errors import Category, ErrorCode, RetryPolicy

# SPEC §15.1 の「リトライ」列の原文 → RetryPolicy。
# SPEC に新しい表現が現れたらここを足さねばならず、そのとき必ずテストが落ちる。
RETRY_BY_SPEC_TEXT = {
    "不可": RetryPolicy.NONE,
    "不可（repair 1 回後）": RetryPolicy.NONE,
    "可（3 回）": RetryPolicy.ATTEMPTS,
    "可（次回 polling）": RetryPolicy.NEXT_POLL,
    "可（次回接続時）": RetryPolicy.NEXT_CONNECT,
    "可（次回評価時）": RetryPolicy.NEXT_EVAL,
}

# SPEC §15.1 の「分類」列の原文 → Category
CATEGORY_BY_SPEC_TEXT = {
    "設定": Category.CONFIG,
    "デバイス": Category.DEVICE,
    "Helper": Category.HELPER,
    "取り込み": Category.IMPORT,
    "音声": Category.AUDIO,
    "文字起こし": Category.TRANSCRIBE,
    "出力": Category.OUTPUT,
    "セッション": Category.SESSION,
    "LLM": Category.LLM,
    "削除": Category.DELETE,
    "DB": Category.DB,
}

# §15.2 の「自動再試行の対象外」
AUTO_RETRY_EXEMPT = {
    ErrorCode.CONFIG_UNKNOWN_KEY,
    ErrorCode.CONFIG_INVALID_VALUE,
    ErrorCode.CONFIG_LOCK_MISMATCH,
    ErrorCode.DUPLICATE_CONTENT,
    ErrorCode.NO_SPEECH_DETECTED,
    ErrorCode.SOURCE_MISSING,
}


def test_error_codes_match_spec() -> None:
    """§15.1 の表と ErrorCode が 1 対 1 であること（過不足の両方を検出する）。"""
    assert {c.value for c in ErrorCode} == set(spec_error_codes())


def test_error_codes_follow_spec_order() -> None:
    """ErrorCode の並びが §15.1 の表と同じであること（照合しやすさのため）。"""
    assert [c.value for c in ErrorCode] == list(spec_error_codes())


def test_retry_policy_matches_spec() -> None:
    for code, retry_text in spec_error_codes().items():
        expected = RETRY_BY_SPEC_TEXT[retry_text]
        assert errors.ERRORS[ErrorCode(code)].retry is expected, code


def test_category_matches_spec() -> None:
    for code, category_text in spec_error_categories().items():
        expected = CATEGORY_BY_SPEC_TEXT[category_text]
        assert errors.ERRORS[ErrorCode(code)].category is expected, code


def test_every_code_has_spec() -> None:
    assert set(errors.ERRORS) == set(ErrorCode)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # §15.2 が max_attempts の対象外と名指しするもの
        (ErrorCode.HELPER_UNAVAILABLE, False),
        (ErrorCode.DELETE_TIMEOUT, False),
        (ErrorCode.SOURCE_IDENTITY_MISMATCH, False),
        (ErrorCode.SOURCE_DELETE_FAILED, False),
        (ErrorCode.DELETE_QUEUE_FAILED, False),
        # polling で再評価されるもの（3 回で打ち切らない）
        (ErrorCode.DEVICE_NOT_READABLE, False),
        (ErrorCode.FILE_NOT_STABLE, False),
        (ErrorCode.DISK_SPACE_LOW, False),
        # 「可（3 回）」のもの
        (ErrorCode.WHISPER_FAILED, True),
        (ErrorCode.LLM_UNAVAILABLE, True),
        (ErrorCode.DB_ERROR, True),
        # 「不可」のもの
        (ErrorCode.NO_SPEECH_DETECTED, False),
        (ErrorCode.LLM_INVALID_JSON, False),
    ],
)
def test_counts_against_max_attempts(code: ErrorCode, expected: bool) -> None:
    assert errors.counts_against_max_attempts(code) is expected


def test_is_retryable_matches_spec_column() -> None:
    for code, retry_text in spec_error_codes().items():
        assert errors.is_retryable(ErrorCode(code)) is retry_text.startswith("可"), code


def test_auto_retry_exempt_list() -> None:
    """§15.2 が名指しする 6 件だけが対象外であること。"""
    actual = {c for c in ErrorCode if errors.ERRORS[c].auto_retry_exempt}
    assert actual == AUTO_RETRY_EXEMPT


def test_llm_invalid_json_is_auto_requeued() -> None:
    """即時リトライは不可だが、24 時間後の自動再投入は行う（§15.2）。

    即時リトライの可否と自動再投入の可否は別の軸である。`retry` 列で自動再投入を
    判定すると、LLM の一時的な出力崩れから永久に復帰できなくなる。
    """
    assert errors.is_retryable(ErrorCode.LLM_INVALID_JSON) is False
    assert errors.is_auto_retryable(ErrorCode.LLM_INVALID_JSON) is True


def test_is_auto_retryable_excludes_only_the_exempt_list() -> None:
    for code in ErrorCode:
        assert errors.is_auto_retryable(code) is (code not in AUTO_RETRY_EXEMPT), code


def test_aborts_startup_matches_spec() -> None:
    actual = {c.value for c in ErrorCode if errors.aborts_startup(c)}
    assert actual == spec_startup_aborting_codes()


def test_spec_for_returns_the_same_object() -> None:
    assert errors.spec_for(ErrorCode.DB_ERROR) is errors.ERRORS[ErrorCode.DB_ERROR]


def test_exit_codes_match_spec() -> None:
    """§17.3 の表と定数が一致すること。"""
    assert spec_exit_codes() == {
        errors.EXIT_OK: "正常",
        errors.EXIT_ERROR: "一般的なエラー",
        errors.EXIT_CONFIG: "設定エラー（起動不可）",
        errors.EXIT_UNHEALTHY: "health check 失敗（unhealthy）",
        errors.EXIT_DOCTOR_FATAL: "診断で致命的な問題を検出（`doctor` のみ）",
    }
