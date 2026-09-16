"""後追いの一括削除（SPEC §17.1 / §21.2 / #38）。

**削除 OFF の期間に処理した Part は `COMPLETED` で終わる。**通常フローは
`RAW_SAVED` から評価するので（§14.3）、**Phase 7 で削除を有効にしても対象にならない。**
1 日で本体が満杯になる運用では、この後追い経路が無いと溜まった録音を消せない。

**このモジュールはデバイスに触れない。**要求を書くだけで、実行は `voicedock-reaper`
である（§14.4 N-16）。**`cleanup` が消すのではない。**

**2 つの操作をまとめない。**

| 操作 | 何をするか |
|---|---|
| `--backlog` | **溜まったものを消す。**§14.1 が真の `COMPLETED` を削除要求へ回す |
| `--resolve-absent` | **消えたことにする。**実体が無い `SOURCE_DELETE_PENDING` を `COMPLETED` へ |

**判断が違う。**前者は「消してよい」、後者は「もう無い」である。**不在のファイルは
同定できない**（`target_is_identical()` が使えない）ので、後者は**同名の別デバイスが
挿さっていれば全件を誤認する**（ND-31 の同名衝突）。**だから別のコマンドにしてある。**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from voicedock import cleaner, db, device
from voicedock.config import Config
from voicedock.db import Database
from voicedock.device import DeviceInventory
from voicedock.errors import EXIT_ERROR, EXIT_OK
from voicedock.heartbeat import DEFAULT_STATE_ROOT
from voicedock.log import Logger
from voicedock.paths import PartKey, SessionKey
from voicedock.states import PartStatus, SessionStatus


@dataclass(frozen=True)
class Plan:
    """対象の一覧。**`--dry-run` はこれを出して終わる。**"""

    eligible: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    """`(partkey, 理由)`。**なぜ対象外かを出す** — 出さないと利用者が判断できない。"""


def plan_backlog(
    database: Database, cfg: Config, inventory: DeviceInventory | None, *, vault_root: Path
) -> Plan:
    """`COMPLETED` のうち §14.1 が真のものを選ぶ。

    **判定は `cleaner.can_delete_source()` をそのまま使う。**論理式を 2 本に分けない
    —— 分けた瞬間に、片方だけ直った状態が生まれる（§14.1）。
    """
    eligible: list[str] = []
    skipped: list[tuple[str, str]] = []
    for row in database.sessions_with_status(SessionStatus.COMPLETED):
        session_key = SessionKey(row.session_key)
        parts = database.recordings_for_session(session_key)
        for part in parts:
            if part.status != PartStatus.COMPLETED:
                continue
            if part.source_deleted_at is not None:
                skipped.append((part.partkey, "already_deleted"))
                continue
            if cleaner.can_delete_source(part, row, parts, cfg, inventory, vault_root=vault_root):
                eligible.append(part.partkey)
            else:
                skipped.append((part.partkey, "not_deletable"))
    return Plan(eligible=tuple(eligible), skipped=tuple(skipped))


def plan_absent(database: Database, inventory: DeviceInventory | None) -> Plan:
    """実体がもう無い `SOURCE_DELETE_PENDING` を選ぶ。

    **`inventory` が読めなければ 1 件も選ばない。**不在を確認できないものを
    「消えた」ことにしない（§7.5）。
    """
    eligible: list[str] = []
    skipped: list[tuple[str, str]] = []
    for part in database.recordings_with_status(PartStatus.SOURCE_DELETE_PENDING):
        if inventory is None:
            skipped.append((part.partkey, "no_inventory"))
        elif cleaner.source_is_gone(part, inventory):
            eligible.append(part.partkey)
        else:
            skipped.append((part.partkey, "still_present"))
    return Plan(eligible=tuple(eligible), skipped=tuple(skipped))


def run(
    *,
    backlog: bool,
    resolve_absent: bool,
    dry_run: bool,
    cfg: Config,
    log: Logger,
    state_root: Path = DEFAULT_STATE_ROOT,
    now: datetime | None = None,
) -> int:
    """`voicedock cleanup` の本体。戻り値が終了コードになる。"""
    inventory = device.read_inventory(state_root)
    moment = now if now is not None else datetime.now(cfg.tz)
    with db.connect(
        cfg.database.path, busy_timeout_ms=cfg.database.busy_timeout_ms, tz=cfg.tz
    ) as database:
        if backlog:
            plan = plan_backlog(database, cfg, inventory, vault_root=Path(cfg.obsidian.root))
            action = _request_deletions
        else:
            plan = plan_absent(database, inventory)
            action = _mark_absent

        _render(plan, dry_run=dry_run, resolve_absent=resolve_absent)
        if dry_run or not plan.eligible:
            return EXIT_OK
        done = action(database, plan, cfg, inventory, log=log, now=moment)
    print(f"処理しました: {done} 件")
    return EXIT_OK if done == len(plan.eligible) else EXIT_ERROR


def _render(plan: Plan, *, dry_run: bool, resolve_absent: bool) -> None:
    label = "COMPLETED にする" if resolve_absent else "削除要求を書く"
    head = "対象（--dry-run。何も書きません）" if dry_run else f"対象（{label}）"
    print(f"{head}: {len(plan.eligible)} 件")
    for partkey in plan.eligible:
        print(f"  {partkey}")
    if plan.skipped:
        print(f"対象外: {len(plan.skipped)} 件")
        for partkey, reason in plan.skipped:
            print(f"  {partkey}  ({reason})")


def _request_deletions(
    database: Database,
    plan: Plan,
    cfg: Config,
    inventory: DeviceInventory | None,
    *,
    log: Logger,
    now: datetime,
) -> int:
    """`COMPLETED → SOURCE_DELETING`（§9.3 の後追い削除）。"""
    done = 0
    for partkey in plan.eligible:
        part = database.get_recording(PartKey(partkey))
        if part is None or part.session_key is None:
            continue
        session = database.get_session(SessionKey(part.session_key))
        if session is None:
            continue
        parts = database.recordings_for_session(SessionKey(part.session_key))
        request = cleaner.request_part_deletion(
            part,
            session,
            parts,
            cfg,
            inventory,
            vault_root=Path(cfg.obsidian.root),
            log=log,
            now=now,
        )
        if request is None:
            continue
        database.record_transition(
            db.EntityType.RECORDING,
            partkey,
            from_status=PartStatus.COMPLETED,
            to_status=PartStatus.SOURCE_DELETING,
            now=now,
        )
        done += 1
    return done


def _mark_absent(
    database: Database,
    plan: Plan,
    cfg: Config,
    inventory: DeviceInventory | None,
    *,
    log: Logger,
    now: datetime,
) -> int:
    """`SOURCE_DELETE_PENDING → SOURCE_DELETING → COMPLETED`。

    **`source_deleted_at` を入れない。**VoiceDock が消したのではない —— **消えていた**
    のである。**不可逆操作の記録に嘘を混ぜない**（§14.1 の根拠に使われる列である）。
    """
    done = 0
    for partkey in plan.eligible:
        database.record_transition(
            db.EntityType.RECORDING,
            partkey,
            from_status=PartStatus.SOURCE_DELETE_PENDING,
            to_status=PartStatus.SOURCE_DELETING,
            detail="resolve_absent",
            now=now,
        )
        database.record_transition(
            db.EntityType.RECORDING,
            partkey,
            from_status=PartStatus.SOURCE_DELETING,
            to_status=PartStatus.COMPLETED,
            detail="already_absent",
            now=now,
        )
        cleaner.withdraw_request(partkey, cfg=cfg)
        log.info("source_delete_skipped", recording_key=partkey, reason="already_absent")
        done += 1
    return done
