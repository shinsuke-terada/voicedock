"""`voicedock.states` が SPEC §9 / §14.1 / §15.2 と一致することを固定する。

状態・遷移・終端集合・巻き戻し表はすべて `docs/SPEC.md` から parse して突き合わせる。
**§9.1 / §9.2 の mermaid 図と §9.3 の遷移表が一致すること**も固定しており、
SPEC の内部整合そのものがテストで守られる。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT
from tests.spec_sync import (
    spec_initial_state,
    spec_mermaid_edges,
    spec_part_terminal_paragraph,
    spec_recovery_map,
    spec_states,
    spec_status_tuple,
    spec_transition_edges,
)
from voicedock import states
from voicedock.states import (
    FAILED_STATUS,
    IN_PROGRESS_SUFFIX,
    PART_DELETABLE,
    PART_IN_PROGRESS,
    PART_INITIAL,
    PART_RECOVERY,
    PART_RETRY_RESET,
    PART_RETRYABLE_FROM_FAILED,
    PART_TERMINAL,
    PART_TRANSITIONS,
    RETRY_RESET_STATUSES,
    SESSION_IN_PROGRESS,
    SESSION_INITIAL,
    SESSION_RECOVERY,
    SESSION_RETRY_RESET,
    SESSION_RETRYABLE_FROM_FAILED,
    SESSION_TRANSITIONS,
    WAITING,
    PartStatus,
    SessionStatus,
    can_transition,
    recovery_target,
    transitions_for,
)

SRC_DIR = REPO_ROOT / "src" / "voicedock"


def values(edges: frozenset[tuple[object, object]]) -> set[tuple[str, str]]:
    return {(str(source), str(target)) for source, target in edges}


# --- 状態の定義 ----------------------------------------------------------


def test_part_states_match_spec() -> None:
    """§9.1 の表と `PartStatus` が 1 対 1（並び順も）。**12 件**。"""
    assert [status.value for status in PartStatus] == spec_states("9.1")
    assert len(list(PartStatus)) == 12


def test_session_states_match_spec() -> None:
    """§9.2 の表と `SessionStatus` が 1 対 1（並び順も）。**13 件**。"""
    assert [status.value for status in SessionStatus] == spec_states("9.2")
    assert len(list(SessionStatus)) == 13


def test_phase7_states_are_defined() -> None:
    """Phase 7 の状態も今すべて定義してある（後から足すと終端集合が二重化する）。"""
    assert PartStatus.SOURCE_DELETING in PartStatus
    assert PartStatus.SOURCE_DELETE_PENDING in PartStatus
    assert SessionStatus.SOURCE_DELETING in SessionStatus
    assert SessionStatus.SOURCE_DELETE_PENDING in SessionStatus


@pytest.mark.parametrize(
    ("section", "expected"), [("9.1", PartStatus.DISCOVERED), ("9.2", SessionStatus.OPEN)]
)
def test_initial_states_match_spec(section: str, expected: object) -> None:
    assert spec_initial_state(section) == str(expected)


def test_initial_constants() -> None:
    assert PART_INITIAL is PartStatus.DISCOVERED
    assert SESSION_INITIAL is SessionStatus.OPEN


# --- 遷移表 --------------------------------------------------------------


def test_part_transitions_match_spec() -> None:
    assert values(PART_TRANSITIONS) == spec_transition_edges("part")


def test_session_transitions_match_spec() -> None:
    assert values(SESSION_TRANSITIONS) == spec_transition_edges("session")


@pytest.mark.parametrize(("entity", "section"), [("part", "9.1"), ("session", "9.2")])
def test_diagrams_match_tables(entity: str, section: str) -> None:
    """**§9.1 / §9.2 の図と §9.3 の表が同じ辺集合であること**（自己遷移を除く）。

    v4.6 の狙いそのもの。v4.5 までは 8 辺食い違っており、とくに Session には
    `FAILED` からの復帰行が 1 つも無かった。**今後どちらかだけ直したら落ちる。**
    """
    table = spec_transition_edges(entity)
    without_self = {edge for edge in table if edge[0] != edge[1]}
    assert without_self == spec_mermaid_edges(section)


def test_specific_edges_exist() -> None:
    """個別に確認したい辺（いずれも取りこぼしやすいもの）。"""
    # #1 で SPEC へ追記した辺
    assert can_transition(PartStatus.DISCOVERED, PartStatus.SKIPPED)
    # Phase 7 の後追い削除（§17.1 `cleanup --backlog`）
    assert can_transition(PartStatus.COMPLETED, PartStatus.SOURCE_DELETING)
    # SHA-256 衝突（DUPLICATE_CONTENT）
    assert can_transition(PartStatus.NORMALIZING, PartStatus.SKIPPED)
    # 有効な Part が 0 件（session_empty）
    assert can_transition(SessionStatus.MERGING, SessionStatus.COMPLETED)
    # 再オープン（allow_reopen）
    assert can_transition(SessionStatus.SAVED, SessionStatus.MERGING)
    assert can_transition(SessionStatus.COMPLETED, SessionStatus.MERGING)
    # デバイス再接続
    assert can_transition(SessionStatus.SOURCE_DELETE_PENDING, SessionStatus.SOURCE_DELETING)


def test_open_self_transition_is_allowed() -> None:
    """`OPEN → OPEN`（Part 追加）は §9.3 が「のまま」と書いていない唯一の自己遷移。"""
    assert can_transition(SessionStatus.OPEN, SessionStatus.OPEN)


@pytest.mark.parametrize("status", [SessionStatus.READY, SessionStatus.SAVED])
def test_stay_rows_are_not_transitions(status: SessionStatus) -> None:
    """`READY のまま` / `SAVED のまま` は遷移ではない（§9.3 の注記）。

    `events` に書くと定期評価のたびに行が増え、§8.4 の「1 日約 260 行」を桁で超える。
    """
    assert not can_transition(status, status)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (PartStatus.DISCOVERED, PartStatus.RAW_SAVED),
        (PartStatus.DISCOVERED, PartStatus.COMPLETED),
        (PartStatus.SKIPPED, PartStatus.NORMALIZING),
        (PartStatus.COMPLETED, PartStatus.DISCOVERED),
        (PartStatus.NORMALIZED, PartStatus.RAW_WRITING),
        (SessionStatus.OPEN, SessionStatus.SAVED),
        (SessionStatus.READY, SessionStatus.WRITING),
    ],
)
def test_rejects_invalid_transitions(current: object, target: object) -> None:
    assert not can_transition(current, target)  # type: ignore[arg-type]


def test_transitions_for() -> None:
    assert transitions_for(PartStatus.RAW_SAVED) == frozenset(
        {PartStatus.SOURCE_DELETING, PartStatus.COMPLETED}
    )
    assert transitions_for(PartStatus.SKIPPED) == frozenset()


# --- entity の混在（StrEnum の落とし穴） --------------------------------


def test_mixing_entities_raises() -> None:
    with pytest.raises(TypeError, match="entity の違う状態"):
        can_transition(PartStatus.COMPLETED, SessionStatus.COMPLETED)


def test_str_enums_collapse_in_sets() -> None:
    """**`StrEnum` は値で比較しハッシュする。**表を 1 つにまとめてはならない理由の記録。

    `PartStatus.COMPLETED` と `SessionStatus.COMPLETED` は等価でハッシュも同じなので、
    1 つの `frozenset` に入れると Part の `(SOURCE_DELETING, COMPLETED)` と Session の
    同名タプルが**同一要素に潰れる**。`type()` だけが両者を区別できる。
    """
    # mypy は「型が重ならない」と正しく言う。**実行時にはそうならない**ことを示すのが
    # このテストの目的なので、ここだけ静的検査を黙らせる
    assert PartStatus.COMPLETED == SessionStatus.COMPLETED  # type: ignore[comparison-overlap]
    assert hash(PartStatus.COMPLETED) == hash(SessionStatus.COMPLETED)
    assert SessionStatus.COMPLETED in {PartStatus.COMPLETED}  # type: ignore[comparison-overlap]
    part_type: type = type(PartStatus.COMPLETED)
    session_type: type = type(SessionStatus.COMPLETED)
    assert part_type is not session_type

    collapsed = {
        (PartStatus.SOURCE_DELETING, PartStatus.COMPLETED),
        (SessionStatus.SOURCE_DELETING, SessionStatus.COMPLETED),
    }
    assert len(collapsed) == 1, "潰れないなら表を分ける理由が変わる"


def test_tables_are_separate() -> None:
    assert PART_TRANSITIONS is not SESSION_TRANSITIONS  # type: ignore[comparison-overlap]
    assert states._TABLES[PartStatus] is PART_TRANSITIONS
    assert states._TABLES[SessionStatus] is SESSION_TRANSITIONS


# --- 終端と削除可否 ------------------------------------------------------


def test_part_terminal_matches_spec() -> None:
    """§9.1 の終端状態（6 件）。段落と §14.1 のタプルの両方と一致すること。"""
    assert {status.value for status in PART_TERMINAL} == spec_part_terminal_paragraph()
    assert {status.value for status in PART_TERMINAL} == set(spec_status_tuple("PART_TERMINAL"))
    assert len(PART_TERMINAL) == 6


def test_part_deletable_matches_spec() -> None:
    assert {status.value for status in PART_DELETABLE} == set(spec_status_tuple("PART_DELETABLE"))
    assert len(PART_DELETABLE) == 4


def test_deletable_is_a_strict_subset_of_terminal() -> None:
    """**削除可能 ⊊ 終端**であり、差は `FAILED` / `SKIPPED` の 2 件。

    混同して `PART_TERMINAL` を削除条件に使うと、**文字起こし本文が保存されていない
    Part の元音声を削除する**。ND-01〜28 のどれも検出しない経路である（v4.6 / K-1）。
    """
    assert PART_DELETABLE < PART_TERMINAL
    assert frozenset({PartStatus.FAILED, PartStatus.SKIPPED}) == PART_TERMINAL - PART_DELETABLE


def test_terminal_states_have_no_outgoing_work() -> None:
    """終端状態から進めるのは削除関連と再オープンだけであること。

    **`SKIPPED` に出る辺は無い。**根拠 B（§14.1）で元音声を消すときも `SKIPPED` の
    まま進める —— `record_transition()` が `error_code` を上書きするので、辺を足すと
    `NO_SPEECH_DETECTED` が消える（`cleaner.awaits_delete_result()`）。
    """
    assert transitions_for(PartStatus.FAILED) == PART_RETRYABLE_FROM_FAILED
    assert transitions_for(PartStatus.SKIPPED) == frozenset()


# --- FAILED からの復帰（§15.2） -----------------------------------------


@pytest.mark.parametrize(
    ("table", "failed", "retryable"),
    [
        (PART_TRANSITIONS, PartStatus.FAILED, PART_RETRYABLE_FROM_FAILED),
        (SESSION_TRANSITIONS, SessionStatus.FAILED, SESSION_RETRYABLE_FROM_FAILED),
    ],
)
def test_failed_recovery_is_symmetric(
    table: frozenset[tuple[object, object]], failed: object, retryable: frozenset[object]
) -> None:
    """**`FAILED` へ入れる状態と `FAILED` から戻れる状態が一致すること。**

    §15.2 の「リトライは最初からやり直さない」を集合の等式で固定する。
    """
    into_failed = {source for source, target in table if target == failed and source != failed}
    out_of_failed = {target for source, target in table if source == failed}
    assert into_failed == out_of_failed
    assert out_of_failed == set(retryable)


# --- 巻き戻し（§9.4） --------------------------------------------------


def test_recovery_matches_spec() -> None:
    """§9.4 の 8 行と一致すること（entity ごとに分けると 9 件）。"""
    spec = set(spec_recovery_map())
    ours = {(str(k), str(v)) for k, v in PART_RECOVERY.items()} | {
        (str(k), str(v)) for k, v in SESSION_RECOVERY.items()
    }
    assert ours == spec
    assert len(spec) == 8
    assert len(PART_RECOVERY) + len(SESSION_RECOVERY) == 9, (
        "SOURCE_DELETING → SOURCE_DELETE_PENDING が Part と Session の両方にある"
    )


@pytest.mark.parametrize(
    ("in_progress", "recovery"),
    [(PART_IN_PROGRESS, PART_RECOVERY), (SESSION_IN_PROGRESS, SESSION_RECOVERY)],
    ids=["part", "session"],
)
def test_every_in_progress_state_has_recovery(
    in_progress: frozenset[object], recovery: Mapping[object, object]
) -> None:
    """進行中の全状態に巻き戻し先があること。

    無いと #31 のクラッシュリカバリで行が**永久に固まる**。
    """
    assert set(in_progress) == set(recovery), (
        f"巻き戻し先の無い進行中状態: {set(in_progress) - set(recovery)}"
    )


@pytest.mark.parametrize(
    ("enum_type", "in_progress"),
    [(PartStatus, PART_IN_PROGRESS), (SessionStatus, SESSION_IN_PROGRESS)],
    ids=["part", "session"],
)
def test_ing_states_are_in_progress_or_waiting(
    enum_type: type[PartStatus] | type[SessionStatus],
    in_progress: frozenset[object],
) -> None:
    """名前が `ING` で終わる状態は、進行中か `WAITING` のどちらかであること。

    **取りこぼしを防ぐ。**新しく `*ING` の状態を足したときに、巻き戻し先を決めないまま
    通ってしまうことを避ける。`SOURCE_DELETE_PENDING` は `PENDING` が `ING` で終わるが
    進行中ではない（巻き戻しの行き先そのもの）。
    """
    for status in enum_type:
        if not str(status).endswith(IN_PROGRESS_SUFFIX):
            continue
        assert status in in_progress or str(status) in WAITING, (
            f"{status} の扱いが決まっていない（進行中か WAITING に入れる）"
        )


def test_in_progress_states_are_not_terminal() -> None:
    """進行中の状態は終端ではないこと。"""
    assert not (PART_IN_PROGRESS & PART_TERMINAL - {PartStatus.SOURCE_DELETING})
    assert PartStatus.SOURCE_DELETING in PART_TERMINAL, "§9.1 は SOURCE_DELETING を終端に含む"


@pytest.mark.parametrize("recovery", [PART_RECOVERY, SESSION_RECOVERY], ids=["part", "session"])
def test_recovery_targets_are_reachable(recovery: Mapping[object, object]) -> None:
    """巻き戻しが不正遷移を作らないこと。"""
    for current, target in recovery.items():
        assert can_transition(current, target) or (current, target) in {  # type: ignore[arg-type]
            (PartStatus.NORMALIZING, PartStatus.DISCOVERED),
            (PartStatus.TRANSCRIBING, PartStatus.NORMALIZED),
            (PartStatus.RAW_WRITING, PartStatus.TRANSCRIBED),
            (SessionStatus.MERGING, SessionStatus.READY),
            (SessionStatus.ANALYZING, SessionStatus.MERGED),
            (SessionStatus.WRITING, SessionStatus.ANALYZED),
            (SessionStatus.CLEANUP, SessionStatus.SAVED),
        }, f"巻き戻し {current} → {target} が遷移表にも巻き戻し許可にも無い"


def test_recovery_target_helper() -> None:
    assert recovery_target(PartStatus.NORMALIZING) is PartStatus.DISCOVERED
    assert recovery_target(SessionStatus.CLEANUP) is SessionStatus.SAVED
    assert recovery_target(PartStatus.COMPLETED) is None


# --- retry_count のリセット（§15.2） ------------------------------------


def test_retry_reset_matches_spec() -> None:
    from tests.spec_sync import spec_retry_reset_statuses

    assert {status.value for status in PART_RETRY_RESET | SESSION_RETRY_RESET} == (
        spec_retry_reset_statuses()
    )
    assert len(PART_RETRY_RESET) == 3
    assert len(SESSION_RETRY_RESET) == 3


def test_retry_reset_statuses_are_str_compatible() -> None:
    """`db.record_transition` は entity 非依存で素の文字列を照合する。"""
    assert "NORMALIZED" in RETRY_RESET_STATUSES
    assert "SAVED" in RETRY_RESET_STATUSES
    assert "FAILED" not in RETRY_RESET_STATUSES
    assert FAILED_STATUS == "FAILED"


def test_reset_statuses_are_stage_passes() -> None:
    """リセット対象が「工程を通過した」状態であること（進行中ではない）。"""
    for status in PART_RETRY_RESET | SESSION_RETRY_RESET:
        assert not str(status).endswith(IN_PROGRESS_SUFFIX)


# --- 到達性 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("enum_type", "initial", "table"),
    [
        (PartStatus, PartStatus.DISCOVERED, PART_TRANSITIONS),
        (SessionStatus, SessionStatus.OPEN, SESSION_TRANSITIONS),
    ],
)
def test_no_unreachable_states(
    enum_type: type[PartStatus] | type[SessionStatus],
    initial: object,
    table: frozenset[tuple[object, object]],
) -> None:
    """初期状態から全状態へ到達できること（孤立した状態が無い）。"""
    reachable = {initial}
    frontier = [initial]
    while frontier:
        current = frontier.pop()
        for source, target in table:
            if source == current and target not in reachable:
                reachable.add(target)
                frontier.append(target)
    assert reachable == set(enum_type), f"到達できない状態: {set(enum_type) - reachable}"


# --- 状態名の文字列が states.py 以外に無いこと --------------------------


def test_status_strings_only_in_states_py() -> None:
    """状態名の文字列リテラルが `states.py` 以外に無いこと。

    **終端集合の再掲を禁止する。**§14.1 の削除条件と §9.1 の終端状態は別集合であり、
    どこかで文字列を書き写すと食い違いが削除事故になる（v4.6 / K-1）。
    """
    names = {status.value for status in PartStatus} | {status.value for status in SessionStatus}
    offenders: dict[str, list[str]] = {}
    for module in sorted(SRC_DIR.glob("*.py")):
        if module.name == "states.py":
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        found = sorted(
            {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in names
            }
        )
        if found:
            offenders[module.name] = found
    assert offenders == {}, f"状態名は states.py だけに置く（§9）: {offenders}"


def test_the_static_check_would_catch_a_reprint() -> None:
    """検査そのものが効くこと。"""
    names = {status.value for status in PartStatus}
    tree = ast.parse('TERMINAL = ("RAW_SAVED", "COMPLETED")')
    found = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names
    }
    assert found == {"RAW_SAVED", "COMPLETED"}


def test_states_does_not_import_voicedock() -> None:
    """`states.py` は `voicedock` の他モジュールを import しないこと。"""
    source = (SRC_DIR / "states.py").read_text(encoding="utf-8")
    assert not re.search(r"^from voicedock", source, re.M)
    assert not re.search(r"^import voicedock", source, re.M)


def test_db_uses_states_constants() -> None:
    """`db.py` が `states` から定数を取っていること（文字列を再掲していない）。"""
    source = (SRC_DIR / "db.py").read_text(encoding="utf-8")
    assert "from voicedock.states import" in source


def test_spec_paths_exist() -> None:
    """テストが読む SPEC が実在すること（マウント漏れを skip せず落とす）。"""
    assert (REPO_ROOT / "docs" / "SPEC.md").is_file()
    assert isinstance(Path(SRC_DIR), Path)
