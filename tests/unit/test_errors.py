"""`voicedock.errors` が SPEC §15 / §17.3 と一致することを固定する。"""

from __future__ import annotations

import dataclasses

import pytest

from tests.spec_sync import (
    spec_error_categories,
    spec_error_codes,
    spec_error_next_states,
    spec_exit_codes,
    spec_startup_aborting_codes,
    spec_status_tuple,
)
from voicedock import errors
from voicedock.daily import BENIGN_SKIP_REASONS
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

# v4.6 の §15.2 が「自動再試行の対象外」として名指ししていた 6 件。
# v5.0 でこの概念は廃止したが、**廃止できた前提を固定するために名前を残す**
# （v4.6→v5.0 の変更 L-3）。
FORMERLY_AUTO_RETRY_EXEMPT = {
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


def test_error_spec_has_no_auto_retry_exemption() -> None:
    """`ErrorSpec` が 3 フィールドであること（v5.0 で `auto_retry_exempt` を廃止）。

    フィールドが戻るのは「§15.2 が時間で待つ設計に戻った」ことを意味する。
    """
    assert [f.name for f in dataclasses.fields(errors.ErrorSpec)] == [
        "category",
        "retry",
        "aborts_startup",
    ]
    assert not hasattr(errors, "is_auto_retryable")


def test_llm_invalid_json_recovers_on_reconnect() -> None:
    """即時リトライは不可だが、`FAILED` に残るので再評価で復帰する（§15.2）。

    v4.6 はこれを「対象外リストに入っていないから 24 時間後に再投入される」と説明していた。
    v5.0 は `FAILED` を無条件に再評価するので、**説明すべき例外が無くなった。**
    """
    assert errors.is_retryable(ErrorCode.LLM_INVALID_JSON) is False
    assert "FAILED" in spec_error_next_states()["LLM_INVALID_JSON"]


def test_formerly_exempt_codes_never_reach_failed() -> None:
    """**`auto_retry_exempt` を廃止できた前提そのものを固定する**（v4.6→v5.0 の変更 L-3）。

    v5.0 の §15.2 は「`FAILED` を無条件に再評価する」だけで、除外リストを持たない。
    それが成り立つのは、v4.6 が除外していた 6 件がどれも **`FAILED` に来ない**
    （設定系は起動中止、残りは Part `SKIPPED`）からである。

    将来 §15.1 でこのどれかの遷移先を `FAILED` に変えたら、**その時点で
    「直らない失敗を接続ごとに永久に再試行する」経路ができる。**このテストが
    そこで落ち、除外の概念を戻す判断を強制する。
    """
    next_states = spec_error_next_states()
    for code in FORMERLY_AUTO_RETRY_EXEMPT:
        target = next_states[code.value]
        assert "FAILED" not in target, (code, target)
        assert "起動中止" in target or "SKIPPED" in target, (code, target)


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


# --- 根拠 B の許可リスト（§14.1） ----------------------------------------


def test_deletable_skip_reasons_match_spec() -> None:
    """**元音声を消してよい `SKIPPED` の理由**が §14.1 の `DELETABLE_SKIP_REASONS` と一致する。

    **足すときは §14.1 を先に直すこと。**理由ごとに「保全すべき本文が無い」と
    言える根拠が要る —— `NO_SPEECH_DETECTED` は whisper の出力が残ることがそれである。
    """
    assert {str(code) for code in errors.DELETABLE_SKIP_REASONS} == set(
        spec_status_tuple("DELETABLE_SKIP_REASONS")
    )
    assert len(errors.DELETABLE_SKIP_REASONS) == 1


def test_source_missing_is_never_deletable() -> None:
    """`SOURCE_MISSING` は入れない。**内容について何も観測していない**（§15.1 / ND-33）。"""
    assert ErrorCode.SOURCE_MISSING not in errors.DELETABLE_SKIP_REASONS


def test_deletable_reasons_need_no_user_action() -> None:
    """**消してよい理由は、必ず「利用者がすることが無い」理由である。**

    `daily.BENIGN_SKIP_REASONS`（警告記号を付けない理由）と**別の定数に分けてある**が、
    包含関係は崩してはならない —— 警告を出す（利用者の確認を求める）理由の元音声を
    黙って消すと、**確認しに行ったときには音声がもう無い。**
    """
    assert errors.DELETABLE_SKIP_REASONS <= BENIGN_SKIP_REASONS
