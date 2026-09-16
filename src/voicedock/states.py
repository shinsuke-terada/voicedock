"""状態機械の定義（SPEC §9）。

**状態はデータである。**§9 の定義と遷移表は `pipeline.py` の制御フローとは変更理由が
異なるため、独立モジュールにしている（§6 の判断）。

**Phase 7 で使う状態（`SOURCE_DELETING` / `SOURCE_DELETE_PENDING`）も全件定義する。**
後から状態を足すと、終端集合の定義が §14.1 の削除条件と別の場所に二重化し、
**食い違いが削除事故になる。**

**2 つの「終端」を混同しないこと。**§9.1 の終端状態（`PART_TERMINAL`、6 件）は
Session の進行判定に使い `FAILED` / `SKIPPED` を含む。削除してよい状態
（`PART_DELETABLE`、4 件）はそれらを含まない — **本文が保存されていない Part を
削除してはならない**（§14.1）。v4.5 までは両方が `TERMINAL` と呼ばれていた。

このモジュールは `voicedock` の他モジュールを import しない。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final


class PartStatus(StrEnum):
    """Part の状態（SPEC §9.1 の表の並び順）。"""

    DISCOVERED = "DISCOVERED"
    NORMALIZING = "NORMALIZING"
    NORMALIZED = "NORMALIZED"
    TRANSCRIBING = "TRANSCRIBING"
    TRANSCRIBED = "TRANSCRIBED"
    RAW_WRITING = "RAW_WRITING"
    RAW_SAVED = "RAW_SAVED"
    SOURCE_DELETING = "SOURCE_DELETING"
    SOURCE_DELETE_PENDING = "SOURCE_DELETE_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class SessionStatus(StrEnum):
    """Session の状態（SPEC §9.2 の表の並び順）。"""

    OPEN = "OPEN"
    READY = "READY"
    MERGING = "MERGING"
    MERGED = "MERGED"
    ANALYZING = "ANALYZING"
    ANALYZED = "ANALYZED"
    WRITING = "WRITING"
    SAVED = "SAVED"
    SOURCE_DELETING = "SOURCE_DELETING"
    SOURCE_DELETE_PENDING = "SOURCE_DELETE_PENDING"
    CLEANUP = "CLEANUP"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


Status = PartStatus | SessionStatus

PART_INITIAL: Final = PartStatus.DISCOVERED
"""`[*] --> DISCOVERED`（§9.1）。行の作成で入る状態。"""

SESSION_INITIAL: Final = SessionStatus.OPEN
"""`[*] --> OPEN`（§9.2）。"""


# --- 終端と削除可否（**名前を分ける**） ----------------------------------

PART_TERMINAL: Final[frozenset[PartStatus]] = frozenset(
    {
        PartStatus.RAW_SAVED,
        PartStatus.SOURCE_DELETING,
        PartStatus.SOURCE_DELETE_PENDING,
        PartStatus.COMPLETED,
        PartStatus.FAILED,
        PartStatus.SKIPPED,
    }
)
"""§9.1 の終端状態（6 件）。**Session の進行判定に使う。**

`FAILED` を含むのは、1 本の失敗でその日の記録全体を止めないためである（§1.3 の優先順位 2）。
§9.3 の `READY → MERGING` のガード「全 Part が終端状態」がこれを参照する。

**削除の可否には使えない**（`PART_DELETABLE` を使う）。
"""

PART_DELETABLE: Final[frozenset[PartStatus]] = frozenset(
    {
        PartStatus.RAW_SAVED,
        PartStatus.SOURCE_DELETING,
        PartStatus.SOURCE_DELETE_PENDING,
        PartStatus.COMPLETED,
    }
)
"""§14.1 で元音声を削除してよい Part の状態（4 件）。

**`PART_TERMINAL` から `FAILED` / `SKIPPED` を除いたもの。**それらは
**文字起こし本文が保存されていない**ので削除してはならない。混同すると
ND-01〜28 のどれも検出しない削除事故になる。
"""

STAGING_DISPOSABLE: Final[frozenset[PartStatus]] = frozenset(PART_TERMINAL) - {PartStatus.FAILED}
"""staging の 16 kHz 音声を捨ててよい Part の状態（5 件。§14.3）。

**`PART_TERMINAL` から `FAILED` だけを除いたもの。**`FAILED` の 16 kHz 音声は
**残骸ではなく再試行の入力**である（§15.2 はデバイス再接続とサービス起動で
`FAILED` を無条件に再評価すると定めている）。`inbox_retain: normalized`（既定）では
inbox の原本が正規化直後に消えているため、**ここで消すと復旧手段が無くなる。**

**`SKIPPED` は含む。**`NO_SPEECH_DETECTED` も `DUPLICATE_CONTENT` も再評価の対象ではない
（§10.2 は重複時に 16 kHz 音声を消すと明記している）。

**手で並べない。**`PART_TERMINAL` から引く — 終端が増えたときに
「捨ててよいか」を明示的に考える場所を 1 つにする。`PART_DELETABLE` とは**別物**である
（あちらは元音声、こちらは変換後の作業ファイル）。
"""

AWAITING_DELETION: Final[frozenset[PartStatus]] = PART_DELETABLE - {PartStatus.COMPLETED}
"""**まだ削除を待っている** Part の状態（3 件。§14.3）。

**`PART_DELETABLE` から `COMPLETED` を除いたもの。**`COMPLETED` は後追い削除
（§17.1 の `cleanup --backlog`）の対象ではあるが、**通常の待ちの対象ではない。**

この集合が空なら、**そのセッションで待つものは何も無い** —— `delete_attempts` を
増やし続けても何も変わらないので、**完了させる**（v5.44→v5.45 の変更 BG-3）。

**手で並べない。**`PART_DELETABLE` から引く。
"""

CLEANUP_FROM: Final[frozenset[SessionStatus]] = frozenset(
    {SessionStatus.SAVED, SessionStatus.SOURCE_DELETING, SessionStatus.SOURCE_DELETE_PENDING}
)
"""`CLEANUP` へ進めるセッションの状態（§9.3）。

**`_complete_without_deleting()` が遷移元に使う。**`SAVED` の決め打ちだと、
**`SOURCE_DELETE_PENDING` に座ったセッションが畳めない**（変更 BG-3）。
"""

SESSION_TERMINAL: Final[frozenset[SessionStatus]] = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.FAILED}
)
"""Session の終端状態。

**SPEC に明示の規定が無い。**§9.2 の表には「終端」列が無いため、遷移先を持たない状態
（`COMPLETED` は再オープンで `MERGING` へ戻れるが、通常運用の終わり）と `FAILED` を
置いている。#19 / #33 が判定に使うなら **SPEC §9.2 へ追記してからにすること。**
"""


# --- 遷移表（§9.3。図と表が一致することをテストが固定している） ----------

PART_TRANSITIONS: Final[frozenset[tuple[PartStatus, PartStatus]]] = frozenset(
    {
        (PartStatus.DISCOVERED, PartStatus.NORMALIZING),
        (PartStatus.DISCOVERED, PartStatus.SKIPPED),
        (PartStatus.NORMALIZING, PartStatus.NORMALIZED),
        (PartStatus.NORMALIZING, PartStatus.SKIPPED),
        (PartStatus.NORMALIZING, PartStatus.FAILED),
        (PartStatus.NORMALIZED, PartStatus.TRANSCRIBING),
        # 16 kHz 音声が消えていた。**作り直しへ戻す**（v5.34→v5.35 の変更 AW-1）
        (PartStatus.NORMALIZED, PartStatus.NORMALIZING),
        (PartStatus.TRANSCRIBING, PartStatus.NORMALIZING),
        (PartStatus.TRANSCRIBING, PartStatus.TRANSCRIBED),
        (PartStatus.TRANSCRIBING, PartStatus.SKIPPED),
        (PartStatus.TRANSCRIBING, PartStatus.FAILED),
        (PartStatus.TRANSCRIBED, PartStatus.RAW_WRITING),
        (PartStatus.RAW_WRITING, PartStatus.RAW_SAVED),
        (PartStatus.RAW_WRITING, PartStatus.FAILED),
        (PartStatus.RAW_SAVED, PartStatus.SOURCE_DELETING),
        (PartStatus.RAW_SAVED, PartStatus.COMPLETED),
        (PartStatus.SOURCE_DELETING, PartStatus.COMPLETED),
        (PartStatus.SOURCE_DELETING, PartStatus.SOURCE_DELETE_PENDING),
        (PartStatus.SOURCE_DELETE_PENDING, PartStatus.SOURCE_DELETING),
        # Phase 7 の後追い削除（§17.1 `cleanup --backlog`）
        (PartStatus.COMPLETED, PartStatus.SOURCE_DELETING),
        # FAILED からの復帰。**最初からやり直さない**（§15.2）
        (PartStatus.FAILED, PartStatus.NORMALIZING),
        (PartStatus.FAILED, PartStatus.TRANSCRIBING),
        (PartStatus.FAILED, PartStatus.RAW_WRITING),
    }
)

SESSION_TRANSITIONS: Final[frozenset[tuple[SessionStatus, SessionStatus]]] = frozenset(
    {
        # Part 追加。§9.3 が「のまま」と書いていない唯一の自己遷移
        (SessionStatus.OPEN, SessionStatus.OPEN),
        (SessionStatus.OPEN, SessionStatus.READY),
        (SessionStatus.READY, SessionStatus.MERGING),
        (SessionStatus.MERGING, SessionStatus.MERGED),
        (SessionStatus.MERGING, SessionStatus.COMPLETED),
        (SessionStatus.MERGING, SessionStatus.FAILED),
        (SessionStatus.MERGED, SessionStatus.ANALYZING),
        (SessionStatus.ANALYZING, SessionStatus.ANALYZED),
        (SessionStatus.ANALYZING, SessionStatus.FAILED),
        (SessionStatus.ANALYZED, SessionStatus.WRITING),
        (SessionStatus.WRITING, SessionStatus.SAVED),
        (SessionStatus.WRITING, SessionStatus.FAILED),
        (SessionStatus.SAVED, SessionStatus.SOURCE_DELETING),
        (SessionStatus.SAVED, SessionStatus.CLEANUP),
        # 再オープン（`allow_reopen`）
        (SessionStatus.SAVED, SessionStatus.MERGING),
        (SessionStatus.COMPLETED, SessionStatus.MERGING),
        (SessionStatus.SOURCE_DELETING, SessionStatus.CLEANUP),
        (SessionStatus.SOURCE_DELETING, SessionStatus.SOURCE_DELETE_PENDING),
        (SessionStatus.SOURCE_DELETE_PENDING, SessionStatus.SOURCE_DELETING),
        # **待つものが無くなったら畳む**（v5.44→v5.45 の変更 BG-3）
        (SessionStatus.SOURCE_DELETE_PENDING, SessionStatus.CLEANUP),
        (SessionStatus.CLEANUP, SessionStatus.COMPLETED),
        (SessionStatus.FAILED, SessionStatus.MERGING),
        (SessionStatus.FAILED, SessionStatus.ANALYZING),
        (SessionStatus.FAILED, SessionStatus.WRITING),
    }
)

_TABLES: Final[Mapping[type, frozenset[tuple[Status, Status]]]] = {
    PartStatus: PART_TRANSITIONS,
    SessionStatus: SESSION_TRANSITIONS,
}
"""**表は entity ごとに分ける。**

`StrEnum` は**値で比較しハッシュする**ため、`PartStatus.COMPLETED == SessionStatus.COMPLETED`
は真になり、`set` / `dict` は 2 つを区別できない。1 つの `frozenset` にまとめると Part の
`(SOURCE_DELETING, COMPLETED)` と Session の同名タプルが**同一要素に潰れる**。
**`type()` だけが両者を区別できる。**
"""


def can_transition(current: Status, target: Status) -> bool:
    """§9.3 が許す遷移かを返す。

    **`type()` で表を引く。**`==` や `in` では 2 つの enum を区別できない（上の注記）。
    entity が混ざった呼び出しは `TypeError` にする。

    自己遷移は `OPEN → OPEN`（Part 追加）だけである。`READY のまま` / `SAVED のまま` は
    §9.3 の注記どおり**遷移ではない**（`events` を書かない）。
    """
    if type(current) is not type(target):
        raise TypeError(
            f"entity の違う状態を比べています: {type(current).__name__} → {type(target).__name__}"
        )
    return (current, target) in _TABLES[type(current)]


def transitions_for(current: Status) -> frozenset[Status]:
    """`current` から進める状態の集合。"""
    table = _TABLES[type(current)]
    return frozenset(target for source, target in table if source == current)


# --- FAILED からの復帰（§15.2） -----------------------------------------

PART_RETRYABLE_FROM_FAILED: Final[frozenset[PartStatus]] = frozenset(
    {PartStatus.NORMALIZING, PartStatus.TRANSCRIBING, PartStatus.RAW_WRITING}
)
"""`FAILED` から戻れる Part の状態。

**`FAILED` へ入れる状態と一致する。**§15.2 の「リトライは最初からやり直さない」を
集合の等式として `tests/unit/test_states.py` が固定している。
"""

SESSION_RETRYABLE_FROM_FAILED: Final[frozenset[SessionStatus]] = frozenset(
    {SessionStatus.MERGING, SessionStatus.ANALYZING, SessionStatus.WRITING}
)
"""`FAILED` から戻れる Session の状態（v4.6 で §9.3 へ追記した）。"""


# --- クラッシュリカバリの巻き戻し（§9.4） -------------------------------

PART_RECOVERY: Final[Mapping[PartStatus, PartStatus]] = {
    PartStatus.NORMALIZING: PartStatus.DISCOVERED,
    PartStatus.TRANSCRIBING: PartStatus.NORMALIZED,
    PartStatus.RAW_WRITING: PartStatus.TRANSCRIBED,
    PartStatus.SOURCE_DELETING: PartStatus.SOURCE_DELETE_PENDING,
}
"""進行中で残った Part を、その工程の直前の完了状態へ巻き戻す（§9.4）。

副作用（部分出力や `*.tmp` の削除）は #31 が行う。ここは行き先だけを持つ。
"""

SESSION_RECOVERY: Final[Mapping[SessionStatus, SessionStatus]] = {
    SessionStatus.MERGING: SessionStatus.READY,
    SessionStatus.ANALYZING: SessionStatus.MERGED,
    SessionStatus.WRITING: SessionStatus.ANALYZED,
    SessionStatus.SOURCE_DELETING: SessionStatus.SOURCE_DELETE_PENDING,
    SessionStatus.CLEANUP: SessionStatus.SAVED,
}
"""§9.4 の Session 側。

§9.4 の表は 8 行だが、`SOURCE_DELETING → SOURCE_DELETE_PENDING` が Part と Session の
**両方に存在する**ため、entity ごとに分けると 4 + 5 = 9 件になる。
"""

PART_IN_PROGRESS: Final[frozenset[PartStatus]] = frozenset(
    {
        PartStatus.NORMALIZING,
        PartStatus.TRANSCRIBING,
        PartStatus.RAW_WRITING,
        PartStatus.SOURCE_DELETING,
    }
)
"""作業が走っている最中の Part の状態。**巻き戻しの対象**（§9.4）。"""

SESSION_IN_PROGRESS: Final[frozenset[SessionStatus]] = frozenset(
    {
        SessionStatus.MERGING,
        SessionStatus.ANALYZING,
        SessionStatus.WRITING,
        SessionStatus.SOURCE_DELETING,
        SessionStatus.CLEANUP,
    }
)
"""作業が走っている最中の Session の状態。`CLEANUP` は名前が `ING` で終わらないが含む。"""

WAITING: Final[frozenset[str]] = frozenset({"SOURCE_DELETE_PENDING"})
"""名前が `ING` で終わるが**進行中ではない**状態。

§9.4 は「進行中状態（`*ING`）」と略記しているが、`SOURCE_DELETE_PENDING` は
**巻き戻しの行き先そのもの**であって巻き戻す対象ではない。`PENDING` が `ING` で
終わるため、接尾辞だけで判定すると取り違える。
"""

IN_PROGRESS_SUFFIX: Final = "ING"
"""`WAITING` との組み合わせで「進行中の取りこぼしが無いこと」をテストが確認する。"""


def recovery_target(current: Status) -> Status | None:
    """巻き戻し先。進行中でなければ `None`。"""
    if isinstance(current, PartStatus):
        return PART_RECOVERY.get(current)
    return SESSION_RECOVERY.get(current)


# --- retry_count のリセット（§15.2。`db.py` が参照する） ----------------

PART_RETRY_RESET: Final[frozenset[PartStatus]] = frozenset(
    {PartStatus.NORMALIZED, PartStatus.TRANSCRIBED, PartStatus.RAW_SAVED}
)
"""工程を通過した Part の状態（§15.2）。"""

SESSION_RETRY_RESET: Final[frozenset[SessionStatus]] = frozenset(
    {SessionStatus.MERGED, SessionStatus.ANALYZED, SessionStatus.SAVED}
)
"""工程を通過した Session の状態（§15.2）。"""

RETRY_RESET_STATUSES: Final[frozenset[str]] = frozenset(
    {status.value for status in PART_RETRY_RESET | SESSION_RETRY_RESET}
)
"""§15.2 の 6 状態を**素の文字列**で持つ。

`db.record_transition` は entity 非依存で `to_status: str` を受けるため、
ここは文字列の集合にしておく。工程をまたいで `retry_count` を累積させると、
前段で 2 回失敗した行が後段の 1 回の失敗で打ち切られてしまう（§15.2）。
"""

FAILED_STATUS: Final = PartStatus.FAILED.value
"""`"FAILED"`。Part と Session で同じ綴りである。"""
