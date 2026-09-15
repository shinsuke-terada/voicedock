"""`voicedock status` が SPEC §17.2 の書式と内容を満たすことを固定する。

**GUI は作らず（§1.6）CLI も 5 本に絞った（§17.1）ので、運用の可視性はすべてここに
集約される。**だから「落ちないこと」が機能要件そのものである — `status` が落ちると、
**Helper が死んだときにこそ何も分からなくなる**（§22 R-23 と同じ形）。
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import write_heartbeat
from tests.spec_sync import spec_section_code
from voicedock import db, paths, status
from voicedock.config import Config
from voicedock.db import Database, Recording, Session
from voicedock.errors import EXIT_OK, ErrorCode
from voicedock.paths import DevicePath, partkey_for
from voicedock.states import PART_TERMINAL, PartStatus, SessionStatus

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 9, 13, 9, 0, 0, tzinfo=JST)
DEVICE = "DJIMIC3"


@pytest.fixture
def cfg(make_config: Callable[..., Config], tmp_path: Path, db_path: Path) -> Config:
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    staging = tmp_path / "data" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    return make_config(
        {
            "database": {"path": str(db_path)},
            "import": {"inbox_root": str(inbox), "staging_root": str(staging)},
        }
    )


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    queue = tmp_path / "queue"
    (queue / "delete").mkdir(parents=True, exist_ok=True)
    (queue / "result").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "DATA_ROOT", data)
    monkeypatch.setattr(paths, "QUEUE_ROOT", queue)


def part(
    database: Database,
    *,
    stem: str = "TX00_MIC001_20260912_120950",
    folder: str = "TX_MIC001_20260912_120950",
    state: str = PartStatus.DISCOVERED,
    duration: float | None = 1800.0,
    error_code: str | None = None,
    retry_count: int = 0,
) -> Recording:
    relpath = f"{folder}/{stem}_orig.wav"
    row = Recording(
        partkey=partkey_for(DEVICE, DevicePath(PurePosixPath(relpath))),
        device_id=DEVICE,
        source_folder=folder,
        transmitter_id="TX00",
        mic_index=1,
        started_at="2026-09-12T12:09:50+09:00",
        status=state,
        updated_at="2026-09-12T12:09:50+09:00",
        duration_seconds=duration,
        source_path=relpath,
        error_code=error_code,
        retry_count=retry_count,
    )
    database.insert_recording(row)
    return row


def session_row(database: Database, *, state: str = SessionStatus.OPEN) -> None:
    database.insert_session(
        Session(
            session_key=f"{DEVICE}:2026091{len(state) % 9}",
            day_date="2026-09-12",
            device_id=DEVICE,
            status=state,
            updated_at="2026-09-12T12:00:00+09:00",
        )
    )


def write_inventory(
    state_root: Path,
    *,
    devices: dict[str, list[str]],
    readonly: bool = True,
    free_bytes: dict[str, int] | None = None,
) -> None:
    """§7.5 の `inventory.json`。**コンテナの唯一のデバイス視界**である。"""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "inventory.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "generated_at": NOW.isoformat(),
                "mount_readonly": readonly,
                "device_free_bytes": free_bytes or {},
                "devices": devices,
            }
        ),
        encoding="utf-8",
    )


def text(cfg: Config, state_root: Path, **kwargs: object) -> str:
    snapshot = status.collect(cfg, state_root=state_root, now=NOW, **kwargs)
    return "\n".join(status.render(snapshot))


def row_value(rendered: str, label: str) -> str:
    for line in rendered.splitlines():
        if line.startswith(label):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{label} の行がありません:\n{rendered}")


# --- SPEC 整合（§17.2） -------------------------------------------------


def test_every_label_in_the_spec_example_is_rendered(cfg: Config, state_root: Path) -> None:
    """**§17.2 の例に並ぶ行ラベルがすべて出ること。**

    SPEC に 1 行足したらここが落ちる。値は環境で変わるので**ラベルだけを突き合わせる。**
    """
    example = spec_section_code("17.2", "text")
    labels = {
        match.group(1).strip()
        for match in re.finditer(r"^([A-Za-z][A-Za-z /()]*?)\s*:", example, re.M)
        if match.group(1).strip() not in {"$ voicedock status"}
    }
    rendered = text(cfg, state_root)
    missing = [
        label for label in labels if not re.search(rf"^\s*{re.escape(label)}\s*:", rendered, re.M)
    ]
    assert missing == [], f"§17.2 の行が出ていません: {missing}\n{rendered}"


def test_the_header_has_the_version(cfg: Config, state_root: Path) -> None:
    from voicedock import __version__

    assert text(cfg, state_root).splitlines()[0] == f"VoiceDock v{__version__}"
    assert text(cfg, state_root).splitlines()[1] == status.SEPARATOR


def test_every_part_status_has_a_bucket(cfg: Config, state_root: Path) -> None:
    """**状態名を CLI 側で書き写さない**（`states.py` の enum を回す）。

    SPEC の例は 8 バケットだけを示すが、**全状態を出す。**出ていない状態に Part が
    溜まっていると、`status` を見ても詰まりが分からない。
    """
    rendered = text(cfg, state_root)
    for state in PartStatus:
        assert re.search(rf"^  {state.value}\s*:", rendered, re.M), state


def test_every_session_status_has_a_bucket(cfg: Config, state_root: Path) -> None:
    rendered = text(cfg, state_root)
    for state in SessionStatus:
        assert re.search(rf"^  {state.value}\s*:", rendered, re.M), state


def test_the_spec_notes_are_attached(cfg: Config, state_root: Path) -> None:
    """§17.2 が添えている注記（`（無音）` / `（次回接続時に再試行）`）。"""
    rendered = text(cfg, state_root)
    assert re.search(r"SKIPPED\s*: 0\s+（無音）", rendered)
    parts, _sep, sessions = rendered.partition("\nSessions")
    assert re.search(r"FAILED\s*: 0\s+（次回接続時に再試行）", parts)
    assert "（次回接続時に再試行）" not in sessions, (
        "Session の FAILED に Part 用の注記が付いている"
        "（§15.2 はリトライの契機を entity で分けている）"
    )


# --- 件数 ---------------------------------------------------------------


def test_the_part_counts_come_from_the_database(
    cfg: Config, state_root: Path, database: Database
) -> None:
    part(database, state=PartStatus.DISCOVERED)
    part(database, stem="TX00_MIC001_20260912_121000", state=PartStatus.COMPLETED)
    part(database, stem="TX00_MIC001_20260912_121500", state=PartStatus.COMPLETED)
    snapshot = status.collect(cfg, state_root=state_root, now=NOW)
    assert snapshot.parts[PartStatus.DISCOVERED] == 1
    assert snapshot.parts[PartStatus.COMPLETED] == 2
    assert snapshot.parts[PartStatus.FAILED] == 0


def test_the_session_counts_come_from_the_database(
    cfg: Config, state_root: Path, database: Database
) -> None:
    session_row(database, state=SessionStatus.OPEN)
    snapshot = status.collect(cfg, state_root=state_root, now=NOW)
    assert snapshot.sessions[SessionStatus.OPEN] == 1


# --- Backlog（§22 R-16） ------------------------------------------------


def test_the_backlog_sums_unfinished_parts(
    cfg: Config, state_root: Path, database: Database
) -> None:
    """**未処理の定義は「終端状態でない Part」。**

    1 日 32 Part の直列処理が追いつかないと溜まる。閾値超過は Whisper モデルを下げる
    判断材料になる（§22 R-16）。
    """
    part(database, duration=3600.0)
    part(database, stem="TX00_MIC001_20260912_130000", duration=1800.0)
    part(database, stem="TX00_MIC001_20260912_140000", state=PartStatus.COMPLETED, duration=9999.0)
    snapshot = status.collect(cfg, state_root=state_root, now=NOW)
    assert snapshot.backlog.parts == 2
    assert snapshot.backlog.seconds == pytest.approx(5400.0)
    assert "1.5 時間" in snapshot.backlog.render()


def test_a_failed_part_is_not_in_the_backlog(
    cfg: Config, state_root: Path, database: Database
) -> None:
    """**`FAILED` は終端だが再試行される。**`Failed parts` の行で別に出す（§17.2）。"""
    part(database, state=PartStatus.FAILED, duration=1800.0)
    assert status.collect(cfg, state_root=state_root, now=NOW).backlog.parts == 0


def test_a_null_duration_is_counted_separately(
    cfg: Config, state_root: Path, database: Database
) -> None:
    """**`duration_seconds` が NULL の件数を併記する**（§10.5）。

    NULL は ffprobe 失敗で残る恒久状態なので、黙って 0 にすると**過小に見える。**
    """
    part(database, duration=None)
    snapshot = status.collect(cfg, state_root=state_root, now=NOW)
    assert snapshot.backlog.unknown_parts == 1
    assert "長さ不明" in snapshot.backlog.render()


def test_an_empty_backlog_says_so(cfg: Config, state_root: Path, database: Database) -> None:
    assert status.collect(cfg, state_root=state_root, now=NOW).backlog.render() == "未処理なし"


# --- Failed parts（§17.2） ----------------------------------------------


def test_failed_parts_are_listed(cfg: Config, state_root: Path, database: Database) -> None:
    row = part(
        database,
        state=PartStatus.FAILED,
        error_code=ErrorCode.WHISPER_TIMEOUT,
        retry_count=3,
    )
    rendered = text(cfg, state_root)
    assert "Failed parts" in rendered
    assert row.partkey in rendered, "partkey が出ていない"
    assert "2026-09-12 12:09" in rendered
    assert "WHISPER_TIMEOUT" in rendered
    assert f"retry 3/{cfg.retry.max_attempts}" in rendered


def test_the_retry_hint_is_shown(cfg: Config, state_root: Path, database: Database) -> None:
    """**再試行が自動であることを明示する**（§15.2）。

    書かないと、利用者が「手で何かしなければならないのか」を調べる時間を取られる。
    `retry` サブコマンドは v5.0 で削除されているので、**探しても見つからない。**
    """
    part(database, state=PartStatus.FAILED, error_code=ErrorCode.WHISPER_FAILED)
    assert status.RETRY_HINT in text(cfg, state_root)


def test_no_failed_section_when_there_are_none(
    cfg: Config, state_root: Path, database: Database
) -> None:
    assert "Failed parts" not in text(cfg, state_root)


def test_the_failed_list_is_capped(cfg: Config, state_root: Path, database: Database) -> None:
    """**全部出すと 1 日分のログが埋まる。**残りの件数を出す。"""
    for n in range(status.FAILED_LIMIT + 5):
        part(
            database,
            stem=f"TX00_MIC001_20260912_1{n:05d}",
            folder=f"TX_MIC001_20260912_1{n:05d}",
            state=PartStatus.FAILED,
            error_code=ErrorCode.WHISPER_FAILED,
        )
    rendered = text(cfg, state_root)
    assert "ほか 5 件" in rendered


def test_a_missing_error_code_is_unknown(cfg: Config, state_root: Path, database: Database) -> None:
    part(database, state=PartStatus.FAILED, error_code=None)
    assert status.UNKNOWN in text(cfg, state_root)


# --- 三重ロック（§14.2 / §17.2） ----------------------------------------


def test_the_locks_are_rendered_individually(cfg: Config, state_root: Path) -> None:
    write_heartbeat(
        state_root,
        delete_source_audio=False,
        reaper_installed=False,
        mount_readonly=True,
        mount_mode="ro",
    )
    value = row_value(text(cfg, state_root), "Source deletion")
    assert value.startswith("DISABLED")
    assert "lock1=false" in value
    assert "lock2A=reaper absent" in value
    assert "lock2B=readOnly" in value


def test_lock1_requires_both_settings(make_config: Callable[..., Config]) -> None:
    """**ロック 1 は `config.yaml` と `helper.conf` の両方**（§14.2）。

    片方だけで true にすると、**設定の片側を変えただけで削除が起きる。**
    """
    assert not status.Locks(True, False, True, False).lock1
    assert not status.Locks(False, True, True, False).lock1
    assert not status.Locks(True, None, True, False).lock1, "不明は安全側"
    assert status.Locks(True, True, True, False).lock1


def test_deletion_is_enabled_only_when_every_lock_is_released() -> None:
    """三重ロックが全部外れたときだけ `ENABLED`（§14.2）。"""
    assert status.Locks(True, True, True, False).enabled
    assert not status.Locks(True, True, True, True).enabled, "lock2B が効いている"
    assert not status.Locks(True, True, False, False).enabled, "reaper が無い"
    assert not status.Locks(True, True, None, False).enabled, "不明は安全側"
    assert not status.Locks(True, True, True, None).enabled, "不明は安全側"


def test_unknown_locks_are_rendered_as_unknown(cfg: Config, state_root: Path) -> None:
    value = row_value(text(cfg, state_root), "Source deletion")
    assert value.startswith("DISABLED")
    assert f"lock2A={status.UNKNOWN}" in value


# --- Helper（§7.5 / §22 R-23） ------------------------------------------


def test_a_running_helper_is_reported(cfg: Config, state_root: Path) -> None:
    write_heartbeat(
        state_root,
        updated_at=(NOW - timedelta(seconds=42)).isoformat(),
        helper_version="5.2.0",
        mount_mode="ro",
        mount_readonly=True,
    )
    value = row_value(text(cfg, state_root), "Helper")
    assert value.startswith("running")
    assert "42s ago" in value
    assert "5.2.0" in value
    assert "mount=readOnly" in value
    assert "MOUNT_MODE=ro" in value


@pytest.mark.parametrize(
    ("mount_readonly", "expected"),
    [(True, "readOnly"), (False, "writable"), (None, status.UNKNOWN)],
)
def test_the_helper_row_shows_the_observed_mount(
    cfg: Config, state_root: Path, mount_readonly: bool | None, expected: str
) -> None:
    """`mount=` は**実測**である（#107）。

    2026-09-14 の実機で、`status` が `mount=ro`（設定の意図）と表示している間に
    `doctor` が `mount=writable`（実測）と表示していた。**同じラベルで逆のことを
    言っていた**。毎日見るのは `status` の方である。
    """
    write_heartbeat(
        state_root,
        updated_at=NOW.isoformat(),
        mount_mode="ro",
        mount_readonly=mount_readonly,
    )
    assert f"mount={expected}" in row_value(text(cfg, state_root), "Helper")


def test_the_helper_row_shows_the_intent_next_to_the_observation(
    cfg: Config, state_root: Path
) -> None:
    """**食い違いが見えること。**食い違い自体が異常の徴候である（#107）。"""
    write_heartbeat(
        state_root,
        updated_at=NOW.isoformat(),
        mount_mode="ro",
        mount_readonly=False,
    )
    value = row_value(text(cfg, state_root), "Helper")
    assert "mount=writable" in value
    assert "MOUNT_MODE=ro" in value


def test_a_stale_helper_is_reported(cfg: Config, state_root: Path) -> None:
    """**Helper の死を表示する**（§22 R-23）。

    気づけないと「新しい録音が無い」と解釈して静かに待つことになる。
    """
    old = NOW - timedelta(seconds=cfg.import_.helper_heartbeat_max_age_seconds + 1)
    write_heartbeat(state_root, updated_at=old.isoformat())
    assert row_value(text(cfg, state_root), "Helper").startswith("stale")


def test_a_missing_heartbeat_does_not_crash(cfg: Config, state_root: Path) -> None:
    """**`status` が落ちると異常時にこそ何も分からない。**"""
    value = row_value(text(cfg, state_root), "Helper")
    assert value.startswith(status.UNKNOWN)


def test_a_config_error_is_surfaced(cfg: Config, state_root: Path) -> None:
    write_heartbeat(state_root, updated_at=NOW.isoformat(), config_error="VOICEDOCK_HOME が未設定")
    assert "VOICEDOCK_HOME が未設定" in text(cfg, state_root)


# --- デバイスと inbox ---------------------------------------------------


def test_connected_devices_come_from_the_inventory(cfg: Config, state_root: Path) -> None:
    write_inventory(
        state_root,
        devices={DEVICE: ["TX_MIC001_20260912_120950/TX00_MIC001_20260912_120950_orig.wav"]},
    )
    value = row_value(text(cfg, state_root), "Devices connected")
    assert value.startswith("1")
    assert DEVICE in value
    assert "readOnly" in value


def test_no_inventory_is_unknown(cfg: Config, state_root: Path) -> None:
    assert row_value(text(cfg, state_root), "Devices connected").startswith(status.UNKNOWN)


def test_an_empty_inventory_is_zero(cfg: Config, state_root: Path) -> None:
    write_inventory(state_root, devices={})
    assert row_value(text(cfg, state_root), "Devices connected") == "0"


def test_the_inbox_counts_wav_files_only(cfg: Config, state_root: Path) -> None:
    """**墓標（`.meta.json` だけ）は数えない**（§10.2）。処理済みの印である。"""
    root = Path(cfg.import_.inbox_root) / DEVICE / "TX_MIC001_20260912_120950"
    root.mkdir(parents=True)
    (root / "a_orig.wav").write_bytes(b"x" * 1024)
    (root / "a_orig.wav.meta.json").write_text("{}", encoding="utf-8")
    (root / "b_orig.wav.meta.json").write_text("{}", encoding="utf-8")
    assert row_value(text(cfg, state_root), "Inbox").startswith("1 parts pending")


def test_device_free_space_is_unknown_without_the_field(cfg: Config, state_root: Path) -> None:
    """**`inventory.json` に項目が無ければ `unknown`**（§7.5 / §17.2）。

    **コンテナはデバイスに到達できない**（N-3）ので、自分で測る経路は作らない。
    """
    write_inventory(state_root, devices={DEVICE: []})
    assert row_value(text(cfg, state_root), "Device free space") == status.UNKNOWN


def test_device_free_space_is_shown_when_reported(cfg: Config, state_root: Path) -> None:
    write_inventory(state_root, devices={DEVICE: []}, free_bytes={DEVICE: int(4.2 * 1024**3)})
    assert row_value(text(cfg, state_root), "Device free space") == f"{DEVICE} 4.2 GiB"


def test_device_free_space_lists_every_device(cfg: Config, state_root: Path) -> None:
    """**デバイスごとに出す。**複数が同時に接続されうる（§5.4 の同名衝突の注記）。

    1 つの数では表せないので、`heartbeat.json`（Helper 1 つの状態）ではなく
    `inventory.json`（デバイスごと）に置いた（v5.3→v5.4 の変更 P-2）。
    """
    write_inventory(
        state_root,
        devices={DEVICE: [], "NO NAME": []},
        free_bytes={DEVICE: 4 * 1024**3, "NO NAME": 1024**3},
    )
    value = row_value(text(cfg, state_root), "Device free space")
    assert value == f"{DEVICE} 4.0 GiB, NO NAME 1.0 GiB"


def test_the_delete_queue_is_counted(cfg: Config, state_root: Path) -> None:
    (paths.QUEUE_ROOT / "delete" / "r1.json").write_text("{}", encoding="utf-8")
    (paths.QUEUE_ROOT / "result" / "r0.json").write_text("{}", encoding="utf-8")
    assert row_value(text(cfg, state_root), "Delete queue") == "1 requested, 1 awaiting result"


# --- DB が無い / 壊れている ---------------------------------------------


def test_a_missing_database_does_not_crash(cfg: Config, state_root: Path, db_path: Path) -> None:
    assert not db_path.exists()
    rendered = text(cfg, state_root)
    assert re.search(r"^  COMPLETED\s*: 0", rendered, re.M)


def test_status_does_not_create_the_database(cfg: Config, state_root: Path, db_path: Path) -> None:
    """**`status` が DB を作ってしまうと「まだ 1 度も動いていない」ことを報告できない。**

    doctor D-2 と同じ理由である（診断は副作用を持たない）。
    """
    text(cfg, state_root)
    assert not db_path.exists(), "status が DB を作った"


def test_a_broken_database_does_not_crash(cfg: Config, state_root: Path, db_path: Path) -> None:
    db_path.write_bytes(b"not a database")
    assert "Parts" in text(cfg, state_root)


def test_an_unmigrated_database_does_not_crash(
    cfg: Config, state_root: Path, db_path: Path
) -> None:
    with db.connect(db_path, migrate=False):
        pass
    assert "Parts" in text(cfg, state_root)


# --- 終了コードと CLI（§17.3 / §17.1） ---------------------------------


def test_run_returns_zero(cfg: Config, state_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**`FAILED` が在っても 0 を返す**（§17.3）。

    1 は「一般的なエラー」であり、**失敗の一覧が出ていることは `status` 自身の
    失敗ではない。**1 を返すと `make status` が常に赤くなり、見る習慣が失われる。
    """
    monkeypatch.setattr(status, "load_config", lambda: cfg)
    buf = io.StringIO()
    assert status.run(buf, state_root=state_root, now=NOW) == EXIT_OK
    assert "VoiceDock v" in buf.getvalue()


def test_status_is_implemented_in_the_cli() -> None:
    from voicedock.cli import IMPLEMENTED, SUBCOMMANDS

    assert "status" in SUBCOMMANDS
    assert "status" in IMPLEMENTED


def test_the_two_formats_keep_their_own_constants() -> None:
    """**書式の定数を doctor と共有しない。**

    §17.2（`status`）と §19.2（`doctor`）は桁が違う。共有すると片方を変えたときに
    もう片方が崩れる。`mypy` が両者の `Literal` を比べて「重なりが無い」と言う
    ほど違う、というのがこのテストの趣旨である。
    """
    from voicedock import doctor

    assert status.LABEL_WIDTH == 22
    assert doctor.LABEL_WIDTH == 21
    assert status.SEPARATOR == doctor.SEPARATOR, "区切り線だけは §19.2 / §17.2 で同じ 56 桁"


# --- inbox の取り残し（#120） -------------------------------------------
# **`pending` は「これから処理される」という意味である。**partkey が終端状態の
# ファイルは二度と処理されないので、同じ数に混ぜてはならない。
# 2026-09-15 に実機で、処理されない 259 MB を `1 parts pending` と報告し続けた
# （`Backlog : 未処理なし` と同時に出るので、どちらを信じればよいか分からない）。


def inbox_pair(
    cfg: Config,
    *,
    stem: str = "TX00_MIC001_20260912_120950",
    folder: str = "TX_MIC001_20260912_120950",
    size: int = 1024,
) -> Path:
    """Helper が置いた形（`.wav` + `.meta.json`）を作る（§10.2）。"""
    root = Path(cfg.import_.inbox_root) / DEVICE / folder
    root.mkdir(parents=True, exist_ok=True)
    wav = root / f"{stem}_orig.wav"
    wav.write_bytes(b"x" * size)
    (root / f"{stem}_orig.wav.meta.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "device_id": DEVICE,
                "relpath": f"{folder}/{stem}_orig.wav",
                "size": size,
                "mtime": 1789000000.0,
                "sha256": "0" * 64,
                "copied_at": "2026-09-12T12:09:50+09:00",
                "helper_version": "5.5.0",
            }
        ),
        encoding="utf-8",
    )
    return wav


def test_orphaned_states_is_terminal_minus_failed() -> None:
    """**定数そのものを固定する。**

    下の parametrize は `ORPHANED_STATES` を元にしているので、
    **空にするとテストケースが 0 個になって黙って消える**（意図的破壊で踏んだ）。
    検証対象を parametrize の元にしてはならない。
    """
    assert PartStatus.COMPLETED in status.ORPHANED_STATES
    assert PartStatus.SKIPPED in status.ORPHANED_STATES
    assert PartStatus.RAW_SAVED in status.ORPHANED_STATES
    # **`FAILED` は取り残しにしない**（§15.2 で再投入される）
    assert PartStatus.FAILED not in status.ORPHANED_STATES
    assert len(status.ORPHANED_STATES) == len(PART_TERMINAL) - 1


@pytest.mark.parametrize(
    "state",
    sorted(status.ORPHANED_STATES),
)
def test_a_terminal_part_is_reported_as_an_orphan(
    cfg: Config, state_root: Path, database: Database, state: str
) -> None:
    """**終端状態の原本は `pending` に数えない。**二度と処理されない（#120）。"""
    part(database, state=state)
    inbox_pair(cfg)

    value = row_value(text(cfg, state_root), "Inbox")
    assert value.startswith("0 parts pending")
    assert "取り残し 1 件" in value


def test_a_failed_part_is_still_pending(cfg: Config, state_root: Path, database: Database) -> None:
    """**`FAILED` は取り残しにしない。**§15.2 でデバイス再接続とサービス起動のたびに
    再投入されるので、**処理待ちである。**
    """
    part(database, state=PartStatus.FAILED)
    inbox_pair(cfg)

    value = row_value(text(cfg, state_root), "Inbox")
    assert value.startswith("1 parts pending")
    assert "取り残し" not in value


def test_an_unknown_part_is_pending(cfg: Config, state_root: Path, database: Database) -> None:
    """**DB に行が無いものは処理待ちである。**まだ discover していないだけ。"""
    inbox_pair(cfg)

    value = row_value(text(cfg, state_root), "Inbox")
    assert value.startswith("1 parts pending")
    assert "取り残し" not in value


def test_no_orphans_prints_no_parenthesis(
    cfg: Config, state_root: Path, database: Database
) -> None:
    """**0 件なら括弧ごと出さない。**正常な状態に注意を引かない。"""
    part(database, state=PartStatus.DISCOVERED)
    inbox_pair(cfg)

    assert "(" not in row_value(text(cfg, state_root), "Inbox")
