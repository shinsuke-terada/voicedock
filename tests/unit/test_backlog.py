"""`voicedock cleanup` の後追い削除（SPEC §17.1 / §21.2 / #38）。

**2 つの操作をまとめない。**

| 操作 | 判断 |
|---|---|
| `--backlog` | **消してよい**（§14.1 が真の `COMPLETED`） |
| `--resolve-absent` | **もう無い**（実体が消えた `SOURCE_DELETE_PENDING`） |

**不在のファイルは同定できない。**`target_is_identical()` が使えないので、
`--resolve-absent` は**同名の別デバイスが挿さっていれば全件を誤認する**
（ND-31 の同名衝突）。**だから自動では起きない形にしてある。**

**このモジュールはデバイスに触れない。**要求を書くだけで、実行は reaper である（N-16）。
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest

from voicedock import backlog, cleaner, db
from voicedock.config import Config
from voicedock.db import Database, Recording, Session
from voicedock.device import DeviceInventory
from voicedock.log import Logger
from voicedock.paths import DevicePath, SessionKey, partkey_for
from voicedock.states import PartStatus, SessionStatus

NOW = datetime(2026, 9, 16, 9, 0, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Tokyo"))
DEVICE_ID = "DJIMIC3"
FOLDER = "TX_MIC001_20260912_090000"
RELPATH = f"{FOLDER}/TX00_MIC001_20260912_090000_orig.wav"
PARTKEY = partkey_for(DEVICE_ID, DevicePath(PurePosixPath(RELPATH)))
SESSION_KEY = SessionKey(f"{DEVICE_ID}:20260912")


@pytest.fixture
def cfg(make_config: Callable[..., Config], tmp_path: Path, db_path: Path) -> Config:
    return make_config(
        {
            "database": {"path": str(db_path)},
            "cleanup": {"delete_source_audio": True, "queue_root": str(tmp_path / "queue")},
        }
    )


@pytest.fixture
def log() -> Logger:
    return Logger(level="DEBUG", fmt="text", stream=io.StringIO())


def add(database: Database, *, status: str, deleted_at: str | None = None) -> None:
    database.insert_session(
        Session(
            session_key=str(SESSION_KEY),
            day_date="2026-09-12",
            device_id=DEVICE_ID,
            status=SessionStatus.COMPLETED,
            updated_at="2026-09-12T12:00:00+09:00",
        )
    )
    database.insert_recording(
        Recording(
            partkey=PARTKEY,
            device_id=DEVICE_ID,
            source_folder=FOLDER,
            transmitter_id="TX00",
            mic_index=1,
            started_at="2026-09-12T09:00:00+09:00",
            status=status,
            updated_at="2026-09-12T09:00:00+09:00",
            source_path=RELPATH,
            source_size=4096,
            source_mtime=1787000000.0,
            session_key=str(SESSION_KEY),
            source_deleted_at=deleted_at,
        )
    )


def present() -> DeviceInventory:
    return DeviceInventory(
        generated_at=NOW,
        mount_readonly=False,
        devices={DEVICE_ID: frozenset({DevicePath(PurePosixPath(RELPATH))})},
    )


def absent() -> DeviceInventory:
    return DeviceInventory(generated_at=NOW, mount_readonly=False, devices={DEVICE_ID: frozenset()})


def state_with(root: Path, inventory: DeviceInventory) -> Path:
    """`state/inventory.json` を置く。**`read_inventory()` が読む形にする。**"""
    root.mkdir(parents=True, exist_ok=True)
    devices = {name: sorted(str(p) for p in paths_) for name, paths_ in inventory.devices.items()}
    (root / "inventory.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "generated_at": NOW.isoformat(),
                "mount_readonly": inventory.mount_readonly,
                "device_free_bytes": {},
                "devices": devices,
            }
        ),
        encoding="utf-8",
    )
    return root


# --- --backlog ----------------------------------------------------------


def test_the_backlog_skips_a_part_that_fails_the_formula(database: Database, cfg: Config) -> None:
    """**§14.1 を通さずに `COMPLETED` を消さない。**

    削除 OFF の期間に処理した Part は Raw ノートの検証を経ていないことがある。
    **「`COMPLETED` だから消してよい」ではない。**
    """
    add(database, status=PartStatus.COMPLETED)

    plan = backlog.plan_backlog(database, cfg, present(), vault_root=Path(cfg.obsidian.root))

    assert plan.eligible == ()
    assert [reason for _key, reason in plan.skipped] == ["not_deletable"]


def test_an_already_deleted_part_is_skipped(database: Database, cfg: Config) -> None:
    """**一度消したものを二度数えない。**`source_deleted_at` が入っていれば対象外。"""
    add(database, status=PartStatus.COMPLETED, deleted_at="2026-09-13T09:00:00+09:00")

    plan = backlog.plan_backlog(database, cfg, present(), vault_root=Path(cfg.obsidian.root))

    assert plan.eligible == ()
    assert [reason for _key, reason in plan.skipped] == ["already_deleted"]


@pytest.mark.parametrize(
    "status", [PartStatus.RAW_SAVED, PartStatus.SOURCE_DELETING, PartStatus.FAILED]
)
def test_only_completed_parts_are_considered(database: Database, cfg: Config, status: str) -> None:
    """**通常フローの担当分に手を出さない。**`--backlog` は `COMPLETED` だけを見る。"""
    add(database, status=status)

    plan = backlog.plan_backlog(database, cfg, present(), vault_root=Path(cfg.obsidian.root))

    assert plan.eligible == ()
    assert plan.skipped == ()


def test_the_dry_run_writes_nothing(
    database: Database, cfg: Config, log: Logger, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**`--dry-run` は 1 バイトも書かない。**列挙して終わる。

    **対象が 1 件以上ある状態で試す。**対象 0 件では「何も書かない」のは当然であり、
    **`--dry-run` を外しても落ちない**（§20.5）。
    """
    add(database, status=PartStatus.SOURCE_DELETE_PENDING)
    state = state_with(tmp_path / "state", absent())

    plan = backlog.plan_absent(database, absent())
    assert plan.eligible == (PARTKEY,), "対象が 0 件では何も確かめていない"

    code = backlog.run(
        backlog=False,
        resolve_absent=True,
        dry_run=True,
        cfg=cfg,
        log=log,
        state_root=state,
        now=NOW,
    )

    assert code == 0
    part = database.get_recording(PARTKEY)
    assert part is not None
    assert part.status == PartStatus.SOURCE_DELETE_PENDING, "--dry-run なのに書き換えた"
    assert "何も書きません" in capsys.readouterr().out


# --- --resolve-absent ---------------------------------------------------


def test_resolve_absent_needs_the_inventory(database: Database) -> None:
    """**不在を確認できないものを「消えた」ことにしない**（§7.5）。"""
    add(database, status=PartStatus.SOURCE_DELETE_PENDING)

    plan = backlog.plan_absent(database, None)

    assert plan.eligible == ()
    assert [reason for _key, reason in plan.skipped] == ["no_inventory"]


def test_resolve_absent_leaves_a_file_that_is_still_there(database: Database) -> None:
    """**まだ在るものに触れない。**"""
    add(database, status=PartStatus.SOURCE_DELETE_PENDING)

    plan = backlog.plan_absent(database, present())

    assert plan.eligible == ()
    assert [reason for _key, reason in plan.skipped] == ["still_present"]


def test_resolve_absent_completes_a_gone_file(database: Database) -> None:
    add(database, status=PartStatus.SOURCE_DELETE_PENDING)

    plan = backlog.plan_absent(database, absent())

    assert plan.eligible == (PARTKEY,)


def test_resolve_absent_does_not_record_a_deletion_time(
    database: Database, cfg: Config, log: Logger, tmp_path: Path
) -> None:
    """**`source_deleted_at` を入れない**（#38 の判断）。

    VoiceDock が消したのではなく、**消えていた**のである。**不可逆操作の記録に
    嘘を混ぜない** —— この列は §14.1 の根拠に使われる。
    """
    add(database, status=PartStatus.SOURCE_DELETE_PENDING)

    backlog.run(
        backlog=False,
        resolve_absent=True,
        dry_run=False,
        cfg=cfg,
        log=log,
        state_root=state_with(tmp_path / "state", absent()),
        now=NOW,
    )

    part = database.get_recording(PARTKEY)
    assert part is not None
    assert part.status == PartStatus.COMPLETED
    assert part.source_deleted_at is None, "消していないのに削除時刻を入れた"


def test_resolve_absent_never_touches_the_queue(
    database: Database, cfg: Config, log: Logger, tmp_path: Path
) -> None:
    """**何も削除しない。**削除要求を 1 件も書かない。"""
    add(database, status=PartStatus.SOURCE_DELETE_PENDING)

    backlog.run(
        backlog=False,
        resolve_absent=True,
        dry_run=False,
        cfg=cfg,
        log=log,
        state_root=state_with(tmp_path / "state", absent()),
        now=NOW,
    )

    directory = Path(cfg.cleanup.queue_root) / cleaner.DELETE_DIRNAME
    assert not directory.is_dir() or list(directory.glob("*.json")) == []


def test_resolve_absent_withdraws_the_result_too(
    database: Database, cfg: Config, log: Logger, tmp_path: Path
) -> None:
    """**結果も取り下げる**（#160）。要求だけ消すと、対応する試行が無い結果が残る。"""
    add(database, status=PartStatus.SOURCE_DELETE_PENDING)
    results = Path(cfg.cleanup.queue_root) / cleaner.RESULT_DIRNAME
    results.mkdir(parents=True, exist_ok=True)
    stale = results / "20260101T000000-old-000000.json"
    stale.write_text(
        json.dumps({"schema": 1, "request_id": "x", "partkey": PARTKEY, "status": "DELETED"}),
        encoding="utf-8",
    )

    backlog.run(
        backlog=False,
        resolve_absent=True,
        dry_run=False,
        cfg=cfg,
        log=log,
        state_root=state_with(tmp_path / "state", absent()),
        now=NOW,
    )

    assert not stale.exists(), "結果を残した"


# --- BH-3: 計画と実行の間に状態が変わる ---------------------------------


OTHER_RELPATH = f"{FOLDER}/TX01_MIC001_20260912_091500_orig.wav"
OTHER_PARTKEY = partkey_for(DEVICE_ID, DevicePath(PurePosixPath(OTHER_RELPATH)))


def add_second(database: Database, *, status: str) -> None:
    """同じセッションの 2 本目。**「残りは処理される」ことを見るのに要る。**"""
    database.insert_recording(
        Recording(
            partkey=OTHER_PARTKEY,
            device_id=DEVICE_ID,
            source_folder=FOLDER,
            transmitter_id="TX01",
            mic_index=1,
            started_at="2026-09-12T09:15:00+09:00",
            status=status,
            updated_at="2026-09-12T09:15:00+09:00",
            source_path=OTHER_RELPATH,
            source_size=4096,
            source_mtime=1787000000.0,
            session_key=str(SESSION_KEY),
        )
    )


def test_a_status_change_between_plan_and_action_does_not_crash(
    database: Database, cfg: Config, log: Logger
) -> None:
    """**計画と実行の間に状態が変わっても中断しない**（変更 BH-3）。

    サービス稼働中に `cleanup --resolve-absent` を流すと起きる —— 計画を取った後、
    worker が `request_deletions()` でその Part を `SOURCE_DELETING` へ動かす。

    v5.45 は `TransitionConflict` が `backlog.run()` → `cli._cleanup()` を**貫通し、
    スタックトレースでコマンドが落ちた。**残りの Part は未処理、
    **既に書いた分は半適用**である。`pipeline.py` の 3 箇所は既に捕捉していた。
    """
    add(database, status=PartStatus.SOURCE_DELETE_PENDING)
    add_second(database, status=PartStatus.SOURCE_DELETE_PENDING)

    plan = backlog.plan_absent(database, absent())
    assert set(plan.eligible) == {PARTKEY, OTHER_PARTKEY}

    # **計画の後に 1 本目だけ動かす**（worker がやること）
    database.record_transition(
        db.EntityType.RECORDING,
        PARTKEY,
        from_status=PartStatus.SOURCE_DELETE_PENDING,
        to_status=PartStatus.SOURCE_DELETING,
        now=NOW,
    )

    # **例外が飛ばないこと**
    done = backlog._mark_absent(database, plan, cfg, absent(), log=log, now=NOW)

    assert done == 1, "動いた 1 本だけを飛ばし、残りは処理すること"
    moved = database.get_recording(PARTKEY)
    other = database.get_recording(OTHER_PARTKEY)
    assert moved is not None and other is not None
    assert moved.status == PartStatus.SOURCE_DELETING, "他所の遷移を上書きした"
    assert other.status == PartStatus.COMPLETED, "残りが処理されていない"
