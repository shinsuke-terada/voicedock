"""削除禁止テスト ND-01〜ND-31 のコンテナ層（SPEC §20.4）。

**本プロジェクトで最も重要なテスト群である。**§20.4 は「このテスト群は CI で必ず実行し、
1 件でも失敗したらリリースしない」と規定している。

**すべて三重ロックをすべて解除した状態で実行する**（`config.yaml` と `helper.conf` の
`delete_source_audio` が `true`、reaper が配置済み、`MOUNT_MODE=rw`）。
**ロックが掛かっているから消えない、では検証にならない。**

コンテナ層が assert するのは 1 つだけである — **§14.1 が偽のとき
`queue/delete/` に要求が 1 件も書かれないこと。**デバイス上のファイルを消すのは
reaper（#54）であり、要求が無ければ reaper は何もしない。

**reaper 層（ND-18〜21 / 24〜29）は #54 と同時に入る。**ここには無い。

> **`test_deletion_actually_happens_when_everything_is_valid` を必ず先に読むこと。**
> これが無いと、**ND が全部緑なのは「安全だから」ではなく「そもそも要求が
> 書かれない設定になっているから」**かもしれない。
"""

from __future__ import annotations

import inspect
import io
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from voicedock import cleaner, device, llm, notes, paths, pipeline
from voicedock.config import Config
from voicedock.db import Database, Recording, Session
from voicedock.device import DeviceInventory
from voicedock.log import Logger
from voicedock.paths import DevicePath, PartKey, SessionKey, partkey_for
from voicedock.pipeline import Pipeline
from voicedock.states import PartStatus, SessionStatus

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 9, 13, 9, 0, 0, tzinfo=JST)
DEVICE_ID = "DJIMIC3"
SESSION_KEY = SessionKey(f"{DEVICE_ID}:20260912")
FOLDER = "TX_MIC001_20260912_090000"
FILENAME = "TX00_MIC001_20260912_090000_orig.wav"
RELPATH = f"{FOLDER}/{FILENAME}"
PARTKEY = partkey_for(DEVICE_ID, DevicePath(PurePosixPath(RELPATH)))
SOURCE_SIZE = 345600
SOURCE_MTIME = 1787000000.0

ANALYSIS: dict[str, object] = {
    "title": "削除条件の整理",
    "summary": "削除条件を論理式へ落とした。",
    "key_points": ["テキストの保全を根拠にする"],
    "tasks": [],
    "decisions": [],
    "ideas": [],
    "tags": ["VoiceDock"],
}


@dataclass
class Scene:
    """**三重ロックをすべて解除した**状態で `SAVED` まで進んだ 1 セッション。"""

    database: Database
    cfg: Config
    runner: Pipeline
    queue: Path
    vault: Path
    inventory: DeviceInventory
    log: io.StringIO

    def requests(self) -> list[Path]:
        directory = self.queue / cleaner.DELETE_DIRNAME
        return sorted(directory.glob("*.json")) if directory.is_dir() else []

    def evaluate(self) -> None:
        """削除の評価だけをもう一度回す（§14.3）。"""
        self.runner.delete_sources_if_safe(SESSION_KEY)

    def rearm(self) -> None:
        """要求を捨て、**もう一度評価できる状態**に戻す。

        `status` を直接書くのはテストの都合である（`record_transition()` を通すと
        `events` が積み上がって読みにくくなる）。**壊すのはこのあとである。**
        """
        for path in self.requests():
            path.unlink()
        self.database.conn.execute(
            "UPDATE recordings SET status = ? WHERE partkey = ?",
            (PartStatus.RAW_SAVED, PARTKEY),
        )
        self.database.conn.execute(
            "UPDATE sessions SET status = ? WHERE session_key = ?",
            (SessionStatus.SAVED, SESSION_KEY),
        )
        self.database.conn.commit()

    def set_part(self, **columns: object) -> None:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        self.database.conn.execute(
            f"UPDATE recordings SET {assignments} WHERE partkey = ?",  # noqa: S608
            (*columns.values(), PARTKEY),
        )
        self.database.conn.commit()

    def set_session(self, **columns: object) -> None:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        self.database.conn.execute(
            f"UPDATE sessions SET {assignments} WHERE session_key = ?",  # noqa: S608
            (*columns.values(), SESSION_KEY),
        )
        self.database.conn.commit()

    def daily_path(self) -> Path:
        row = self.session()
        assert row.output_path is not None
        return self.vault / row.output_path

    def raw_path(self) -> Path:
        row = self.session()
        assert row.raw_output_path is not None
        return self.vault / row.raw_output_path

    def part(self) -> Recording:
        row = self.database.get_recording(PARTKEY)
        assert row is not None
        return row

    def session(self) -> Session:
        row = self.database.get_session(SESSION_KEY)
        assert row is not None
        return row


@pytest.fixture
def scene(
    database: Database,
    make_config: Callable[..., Config],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Scene]:
    """**ロックを全部外した**うえで `SAVED` まで進める。

    ここから 1 つずつ壊して「それでも要求が書かれない」ことを見る。
    """
    data = tmp_path / "data"
    for sub in ("transcripts/parts", "analysis", "staging"):
        (data / sub).mkdir(parents=True)
    monkeypatch.setattr(paths, "DATA_ROOT", data)
    monkeypatch.setattr(paths, "TRANSCRIPTS_ROOT", data / "transcripts" / "parts")
    monkeypatch.setattr(paths, "ANALYSIS_ROOT", data / "analysis")

    vault = tmp_path / "obsidian"
    vault.mkdir()
    queue = tmp_path / "queue"
    state = tmp_path / "state"
    state.mkdir()

    cfg = make_config(
        {
            "obsidian": {"root": str(vault)},
            # **安全ロック 1 を外す**（§14.2）
            "cleanup": {"delete_source_audio": True, "queue_root": str(queue)},
        }
    )
    stream = io.StringIO()
    log = Logger(level="DEBUG", fmt="text", stream=stream)
    runner = Pipeline(database=database, cfg=cfg, log=log, now=NOW, state_root=state)

    # **安全ロック 2-B を外す**（`mount_readonly: false`）。ロック 2-A（reaper の有無）は
    # ホスト側なので、コンテナ層のテストには現れない
    inventory = DeviceInventory(
        generated_at=NOW,
        mount_readonly=False,
        devices={DEVICE_ID: frozenset({DevicePath(PurePosixPath(RELPATH))})},
    )
    monkeypatch.setattr(device, "read_inventory", lambda _root=None: inventory)

    _add_session(database)
    _add_part(database)
    _stub_llm(monkeypatch, cfg)

    record = database.get_recording(PARTKEY)
    assert runner.ensure_raw_note(record), "Raw ノートを用意できていない"
    runner.process_session(SESSION_KEY)

    yield Scene(
        database=database,
        cfg=cfg,
        runner=runner,
        queue=queue,
        vault=vault,
        inventory=inventory,
        log=stream,
    )


def _add_session(database: Database) -> None:
    database.insert_session(
        Session(
            session_key=SESSION_KEY,
            day_date="2026-09-12",
            device_id=DEVICE_ID,
            status=SessionStatus.READY,
            updated_at="2026-09-12T12:00:00+09:00",
        )
    )


def _add_part(database: Database, *, status: str = PartStatus.TRANSCRIBED) -> None:
    started = "2026-09-12T09:00:00+09:00"
    database.insert_recording(
        Recording(
            partkey=PARTKEY,
            device_id=DEVICE_ID,
            source_folder=FOLDER,
            transmitter_id="TX00",
            mic_index=1,
            started_at=started,
            status=status,
            updated_at=started,
            duration_seconds=60.0,
            ended_at="2026-09-12T09:01:00+09:00",
            source_path=RELPATH,
            source_size=SOURCE_SIZE,
            source_mtime=SOURCE_MTIME,
            session_key=SESSION_KEY,
            transcript_path=str(paths.transcript_path_for(PartKey(PARTKEY))),
        )
    )
    target = paths.transcript_path_for(PartKey(PARTKEY))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "partkey": PARTKEY,
                "language": "ja",
                "duration_seconds": 60.0,
                "started_at": started,
                "text": "おはようございます。",
                "segments": [{"start": 0.0, "end": 3.0, "text": "おはようございます。"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _stub_llm(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """**ネットワークを使わない**（§20.2）。"""

    def fake(_transcript: object, **_kwargs: object) -> llm.SessionAnalysis:
        return llm.SessionAnalysis(
            result=llm.build_schema(cfg).model_validate(ANALYSIS),
            chunks=(),
            partials=(),
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(llm, "analyze_session", fake)


# --- 正の対照（これが無いと ND 群は何も検証していない） ------------------


def test_deletion_actually_happens_when_everything_is_valid(scene: Scene) -> None:
    """**すべて揃っていれば要求が書かれること。**

    §20.4 が要求する「テストが本当に効いていることの証明」である。これが緑でなければ、
    **ND が全部緑なのは「安全だから」ではなく「そもそも要求が書かれない設定だから」**
    である（v3.0 の ND 群が実際にその状態だった）。
    """
    written = scene.requests()
    assert len(written) == 1, [p.name for p in written]
    payload = json.loads(written[0].read_text(encoding="utf-8"))

    assert payload["schema"] == cleaner.QUEUE_SCHEMA
    assert payload["partkey"] == PARTKEY, "reaper の検証 12 が見る"
    assert payload["device_id"] == DEVICE_ID
    assert payload["session_key"] == SESSION_KEY
    assert payload["targets"] == [{"relpath": RELPATH, "size": SOURCE_SIZE, "mtime": SOURCE_MTIME}]
    assert scene.part().status == PartStatus.SOURCE_DELETING
    assert "delete_requested" in scene.log.getvalue()


def test_rearming_alone_still_produces_a_request(scene: Scene) -> None:
    """**`rearm()` 自体が要求を止めていないこと。**

    ND 群は「`rearm()` → 1 つ壊す → 要求が無い」という形をしている。`rearm()` が
    何も壊さなくても要求が出ないなら、**ND は全部「壊したから止まった」ではなく
    「最初から止まっていた」を見ているだけ**になる。
    """
    scene.rearm()
    assert scene.requests() == [], "rearm() が要求を消していない"
    scene.evaluate()
    assert len(scene.requests()) == 1, "壊していないのに要求が書かれない"


def test_the_request_never_contains_an_absolute_path(scene: Scene) -> None:
    """**要求は絶対パスを持たない**（§14.1.1 / N-17）。

    reaper は `device_id` と名前が一致する**現在マウント中のボリューム**に対してのみ
    解決する。これにより**「要求がボリューム外のパスを指名すること」自体が
    構造的に不可能**になる。
    """
    payload = json.loads(scene.requests()[0].read_text(encoding="utf-8"))
    blob = json.dumps(payload, ensure_ascii=False)
    assert "/Volumes" not in blob
    for target in payload["targets"]:
        rel = PurePosixPath(target["relpath"])
        assert not rel.is_absolute()
        assert all(part not in ("", ".", "..") and not part.startswith(".") for part in rel.parts)


# --- ND-01〜ND-06: Part 自身の処理が完了していない（§14.1） --------------
#
# **これらの故障注入そのもの**（I/O エラー・長さずれ・重複・whisper 失敗・
# タイムアウト・無音）は `test_normalize.py` / `test_transcribe.py` /
# `test_pipeline_contract.py` が見ている。ここが見るのは**その結果の状態から
# 削除要求が出ないこと**である（§20.4 のコンテナ層の assert）。


@pytest.mark.parametrize(
    ("nd", "status"),
    [
        ("ND-01", PartStatus.NORMALIZING),
        ("ND-02", PartStatus.FAILED),
        ("ND-03", PartStatus.SKIPPED),
        ("ND-04", PartStatus.FAILED),
        ("ND-05", PartStatus.FAILED),
        ("ND-06", PartStatus.SKIPPED),
    ],
)
def test_nd01_to_nd06_a_part_that_did_not_finish_is_never_requested(
    scene: Scene, nd: str, status: str
) -> None:
    """ND-01〜06: `part.status not in PART_DELETABLE` なら要求が書かれない。

    | ND | 故障 | 行き着く状態 |
    |---|---|---|
    | ND-01 | 変換中に I/O エラー | 進行中のまま |
    | ND-02 | 変換結果の長さが 1 秒以上ずれる | `FAILED`（`NORMALIZE_VERIFY_FAILED`） |
    | ND-03 | 内容が同一の重複ファイル | `SKIPPED` |
    | ND-04 | Whisper 失敗 | `FAILED` |
    | ND-05 | Whisper タイムアウト | `FAILED` |
    | ND-06 | 発話が検出されない | `SKIPPED` |

    **`FAILED` / `SKIPPED` は §9.1 の終端状態だが `PART_DELETABLE` ではない**
    （本文が保存されていない）。v4.5 まで両者を同じ名前で呼んでいた。
    """
    scene.rearm()
    scene.set_part(status=status)
    scene.evaluate()
    assert scene.requests() == [], nd

    # **§14.1 の論理式そのものも偽であること。**`delete_sources_if_safe()` 側にも
    # 同じ絞り込みがあるので、要求が無いことだけを見ると
    # **`can_delete_source()` から条件を削っても通ってしまう**（実際に通った）
    assert not cleaner.can_delete_source(
        scene.part(),
        scene.session(),
        [scene.part()],
        scene.cfg,
        scene.inventory,
        vault_root=scene.vault,
    ), nd


# --- ND-07〜ND-09: Raw ノート ------------------------------------------


def test_nd07_a_missing_raw_note_path_blocks_the_request(scene: Scene) -> None:
    """ND-07: Raw ノート書き込み失敗 → `session.raw_output_path is None`。"""
    scene.rearm()
    scene.set_session(raw_output_path=None)
    scene.evaluate()
    assert scene.requests() == []


def test_nd08_a_raw_note_deleted_afterwards_blocks_the_request(scene: Scene) -> None:
    """ND-08: 保存後に外部から削除された → `verify_raw_note` が偽。

    **DB の `status` を信用しない**（N-12）。列は `RAW_SAVED` のままである。
    """
    scene.rearm()
    scene.raw_path().unlink()
    scene.evaluate()
    assert scene.requests() == []


def test_nd08_a_tampered_raw_note_blocks_the_request(scene: Scene) -> None:
    """ND-08: Raw ノートが改竄された（**鍵は載ったまま**）→ `verify_raw_note` が偽。

    **この形でないと `verify_raw_note()` を単独で確かめられない。**ファイルごと消すと
    鍵の包含判定も同時に偽になるので、**`verify_raw_note()` を論理式から削っても
    ND-08 は緑のまま通る**（意図的に削って確かめたら実際に通った）。
    """
    scene.rearm()
    path = scene.raw_path()
    path.write_text(path.read_text(encoding="utf-8") + "\n追記された行\n", encoding="utf-8")
    assert PARTKEY in notes.frontmatter_keys(path), "鍵は載ったままであること"
    scene.evaluate()
    assert scene.requests() == []


def test_nd09_a_raw_note_without_this_key_blocks_the_request(scene: Scene) -> None:
    """ND-09: Raw ノートの `voicedock_recording_keys` に当該 Part が無い。

    **鍵の包含が偽なら消さない**（§14.1）。「ノートがファイル X を含むと言っているとき、
    X を消す」の 1 段の論理である（v5.0→v5.1 の変更 M-1）。
    """
    scene.rearm()
    _rewrite_keys(
        scene, scene.raw_path(), f"{DEVICE_ID}/other/other.wav", column="raw_output_sha256"
    )
    scene.evaluate()
    assert scene.requests() == []


def _rewrite_keys(scene: Scene, path: Path, replacement: str, *, column: str) -> None:
    """ノートの鍵を差し替え、**DB の sha256 も合わせて更新する。**

    合わせないと保存検証が先に偽になり、**鍵の包含判定を論理式から削っても
    テストが緑のまま通る**（意図的に削って確かめたら実際に通った）。ここで見たいのは
    「ノートが別のファイルを指しているのに消す」ことが起きないかである。
    """
    body = path.read_text(encoding="utf-8").replace(PARTKEY, replacement)
    path.write_text(body, encoding="utf-8")
    digest = notes.sha256_bytes(path.read_bytes())
    scene.set_session(**{column: digest})


def test_nd09b_a_daily_note_without_this_key_blocks_the_request(scene: Scene) -> None:
    """ND-09（Daily 側）: Daily ノートの鍵に当該 Part が無い。

    **Raw と Daily で条件が別々に書かれている**ので、両方に個別のテストが要る。
    """
    scene.rearm()
    _rewrite_keys(scene, scene.daily_path(), f"{DEVICE_ID}/other/other.wav", column="output_sha256")
    scene.evaluate()
    assert scene.requests() == []


# --- ND-10〜ND-11: 解析 -------------------------------------------------


def test_nd10_an_unreachable_llm_blocks_the_request(scene: Scene) -> None:
    """ND-10: LLM 到達不可 → `session.analysis_path is None`。"""
    scene.rearm()
    scene.set_session(analysis_path=None)
    scene.evaluate()
    assert scene.requests() == []


def test_nd11_an_invalid_analysis_blocks_the_request(scene: Scene) -> None:
    """ND-11: LLM が不正 JSON を返す（repair も失敗） → スキーマ検証が偽。"""
    scene.rearm()
    row = scene.session()
    assert row.analysis_path is not None
    Path(row.analysis_path).write_text('{"title": 42}', encoding="utf-8")
    scene.evaluate()
    assert scene.requests() == []


# --- ND-12〜ND-16: Daily ノート -----------------------------------------


@pytest.mark.parametrize("nd", ["ND-12", "ND-13"])
def test_nd12_and_nd13_a_missing_daily_note_path_blocks_the_request(scene: Scene, nd: str) -> None:
    """ND-12 / ND-13: Vault が無い / 書き込み権限が無い → `output_path is None`。

    **一時ファイルも残らない**ことは `test_atomic_write.py` が見ている（§13.6）。
    """
    scene.rearm()
    scene.set_session(output_path=None)
    scene.evaluate()
    assert scene.requests() == [], nd


def test_nd14_a_daily_note_missing_frontmatter_blocks_the_request(scene: Scene) -> None:
    """ND-14: 保存検証失敗（frontmatter 欠落を注入） → `verify_daily_note` が偽。"""
    scene.rearm()
    path = scene.daily_path()
    body = path.read_text(encoding="utf-8")
    path.write_text(body.split("---\n", 2)[-1], encoding="utf-8")
    scene.evaluate()
    assert scene.requests() == []


def test_nd15_a_daily_note_deleted_afterwards_blocks_the_request(scene: Scene) -> None:
    """ND-15: 保存後に外部から削除された。"""
    scene.rearm()
    scene.daily_path().unlink()
    scene.evaluate()
    assert scene.requests() == []


def test_nd16_a_tampered_daily_note_blocks_the_request(scene: Scene) -> None:
    """ND-16: 保存後に改竄された → `output_sha256` 不一致。

    **利用者がノートを編集した場合もここで止まる**（§22 R-18）。削除が止まるだけで害はない。
    """
    scene.rearm()
    path = scene.daily_path()
    path.write_text(path.read_text(encoding="utf-8") + "\n編集した行\n", encoding="utf-8")
    scene.evaluate()
    assert scene.requests() == []


# --- ND-17: セッション内に進行中の Part --------------------------------


def test_nd17_an_unfinished_sibling_blocks_every_part(scene: Scene) -> None:
    """ND-17: セッション内に進行中の Part が残っている

    → **セッション内のどの Part も削除されない**（§14.1 の全 Part 終端条件）。
    """
    scene.rearm()
    other = partkey_for(
        DEVICE_ID,
        DevicePath(PurePosixPath("TX_MIC001_20260912_100000/TX00_MIC001_20260912_100000_orig.wav")),
    )
    scene.database.insert_recording(
        Recording(
            partkey=other,
            device_id=DEVICE_ID,
            source_folder="TX_MIC001_20260912_100000",
            transmitter_id="TX00",
            mic_index=1,
            started_at="2026-09-12T10:00:00+09:00",
            status=PartStatus.TRANSCRIBING,
            updated_at="2026-09-12T10:00:00+09:00",
            session_key=SESSION_KEY,
        )
    )
    scene.evaluate()
    assert scene.requests() == []


# --- ND-21: 空集合・空文字を真にしない（§14.1 の番犬） ------------------


def test_nd21_an_empty_session_is_not_deletable(scene: Scene) -> None:
    """ND-21（コンテナ側）: Part を 0 件持つ Session。**空集合の `all()` は真になる。**"""
    parts: list[Recording] = []
    assert not cleaner.can_delete_source(
        scene.part(), scene.session(), parts, scene.cfg, scene.inventory, vault_root=scene.vault
    )


@pytest.mark.parametrize("bad", [None, ""])
def test_nd21_a_null_or_empty_source_path_is_not_deletable(scene: Scene, bad: str | None) -> None:
    """ND-21（コンテナ側）: `source_path` が `NULL` または**空文字**。

    **`os.path.join(volume, "")` はボリュームのルートを指す**（§14.1）。
    `_orig` 固定でスカラーに退化したが、**この番犬は消さない。**
    """
    scene.rearm()
    scene.set_part(source_path=bad)
    scene.evaluate()
    assert scene.requests() == []


# --- ND-22 / ND-23: 三重ロック（コンテナ側） ----------------------------


def test_nd22_lock_one_blocks_the_request(scene: Scene, make_config: Callable[..., Config]) -> None:
    """ND-22: `config.yaml` の `delete_source_audio: false`（安全ロック 1）。

    **`helper.conf` 側は reaper の検証 1 が見る**（#54）。どちらか一方だけでも偽なら
    削除は起きない（§14.2）。
    """
    scene.rearm()
    cfg = make_config(
        {
            "obsidian": {"root": str(scene.vault)},
            "cleanup": {"delete_source_audio": False, "queue_root": str(scene.queue)},
        }
    )
    assert not cleaner.can_delete_source(
        scene.part(),
        scene.session(),
        [scene.part()],
        cfg,
        scene.inventory,
        vault_root=scene.vault,
    )


@pytest.mark.parametrize("readonly", [True, None])
def test_nd23_lock_two_b_blocks_the_request(scene: Scene, readonly: bool | None) -> None:
    """ND-23: `MOUNT_MODE=ro`（`heartbeat.json` の `mount_readonly` が真）。

    **不明（`None`）も安全側へ倒す**（§7.5）。実機では OS レベルでも書けない
    （`docs/POC.md` §4.1 で実証済み）。
    """
    inventory = DeviceInventory(
        generated_at=NOW,
        mount_readonly=readonly,
        devices={DEVICE_ID: frozenset({DevicePath(PurePosixPath(RELPATH))})},
    )
    assert not cleaner.can_delete_source(
        scene.part(),
        scene.session(),
        [scene.part()],
        scene.cfg,
        inventory,
        vault_root=scene.vault,
    )


def test_an_unreadable_inventory_blocks_the_request(scene: Scene) -> None:
    """`inventory.json` が読めないときも削除しない（§7.5 の「不明は安全側」）。"""
    assert not cleaner.can_delete_source(
        scene.part(), scene.session(), [scene.part()], scene.cfg, None, vault_root=scene.vault
    )


# --- ND-30: /state は読み取り専用（§18.2） ------------------------------


def test_nd30_the_state_mount_is_read_only_in_compose() -> None:
    """ND-30: `state/` をコンテナから書き換えて `mount_readonly` を偽装できないこと。

    **書き込み自体が失敗する**のが正しい（§18.2）。テストコンテナは `/state` を
    `rw` で持っていないので、**`compose.yaml` が `ro` を指定していることを静的に見る。**
    実際に書けないことは実機の E2E（#35）が確かめる。
    """
    import yaml

    from tests.helpers import REPO_ROOT

    document = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    volumes = document["services"]["voicedock"]["volumes"]
    state = [entry for entry in volumes if ":/state" in str(entry)]
    assert state, volumes
    for entry in state:
        assert str(entry).endswith(":ro"), f"/state は ro でマウントする（§18.2）: {entry}"


# --- ND-31: device_id だけが違う鍵（§5.4 の同名衝突） -------------------


def test_nd31_a_key_from_another_device_blocks_the_request(scene: Scene) -> None:
    """ND-31: ノートの鍵の **`device_id` だけが**対象と違う（`NO NAME` の同名衝突）。

    → **別デバイスの同名録音を消さない。**v5.0 までは整数 ID だったため、
    **この食い違いはノートを見ても分からなかった**（v5.1 で追加）。
    """
    scene.rearm()
    other_key = partkey_for("NO NAME", DevicePath(PurePosixPath(RELPATH)))
    assert other_key != PARTKEY
    assert other_key.endswith(RELPATH), "フォルダ名以降は完全に一致する"

    _rewrite_keys(scene, scene.raw_path(), other_key, column="raw_output_sha256")
    _rewrite_keys(scene, scene.daily_path(), other_key, column="output_sha256")
    scene.evaluate()
    assert scene.requests() == []


# --- `target_is_identical()` を単独で落とす（§14.1.1） ------------------


def test_a_file_absent_from_the_inventory_blocks_the_request(scene: Scene) -> None:
    """`state/inventory.json` にそのファイルが無ければ要求を書かない（§10.12）。

    **デバイスが外れている、あるいは既に消えている。**要求を書いても reaper は
    検証 3 で拒むだけであり、キューにゴミが溜まる。

    **この形でないと `target_is_identical()` を単独で確かめられない。**
    `source_path` を壊す経路は `is not None` / `!= ""` が先に受け止めるので、
    **`target_is_identical()` を論理式から削ってもテストが緑のまま通る**
    （意図的に削って確かめたら実際に通った）。
    """
    empty = DeviceInventory(
        generated_at=NOW, mount_readonly=False, devices={DEVICE_ID: frozenset()}
    )
    assert not cleaner.can_delete_source(
        scene.part(), scene.session(), [scene.part()], scene.cfg, empty, vault_root=scene.vault
    )


def test_a_key_that_disagrees_with_the_path_blocks_the_request(scene: Scene) -> None:
    """reaper の検証 12 をコンテナ側でも行う（§14.1.1）。

    `partkey` と `<device_id>/<relpath>` が食い違っていたら、**「ノートで確認した
    ファイル」と「消すファイル」が別物である。**
    """
    scene.rearm()
    scene.set_part(source_path="TX_MIC001_20260912_090000/TX00_MIC001_20260912_090000.wav")
    assert not cleaner.target_is_identical(scene.part(), scene.inventory)
    scene.evaluate()
    assert scene.requests() == []


# --- 空集合・空文字の番犬が論理式に在ること（§14.1） --------------------


def test_the_watchdog_terms_are_present_in_the_formula() -> None:
    """**振る舞いでは単独で落とせない項が論理式に在ること。**

    §14.1 は**冗長な番犬を意図的に置いている。**他の条件が必ず先に受け止めるので、
    削ってもどのテストも落ちない。だから「在ること」そのものを固定する。
    **消したくなったら §14.1 の本文を先に直すこと。**

    | 項 | なぜ振る舞いで落とせないか |
    |---|---|
    | `len(parts) >= 1` | 空集合だと保存検証（W-7 の完全一致）が先に偽になる |
    | `part.source_path != ""` | 空文字は `target_is_identical()` が先に偽にする |
    | Raw / Daily の鍵の包含 | `verify_note()` の R-6 / W-7 が**同じ鍵の集合**を見ている |

    **鍵の包含を残す理由。**R-6 / W-7 が見るのは `expected_keys`（この Session の
    全 Part から導く集合）であり、**この Part 1 件の話ではない。**導き方が変われば
    重なりは消える。§14.1 が両方を並べているのはそのためである。

    **空集合の `all()` は真になり、`os.path.join(volume, "")` はボリュームのルートを
    指す**（v3.0 の欠陥 A-14 と同型）。
    """
    source = inspect.getsource(cleaner.can_delete_source)
    assert "len(parts) >= 1" in source
    assert 'part.source_path != ""' in source
    assert "all(" in source, "全 Part 終端条件が消えている"
    assert source.count("_note_contains(") == 2, "Raw と Daily の両方で鍵の包含を見ること"
    assert "session.raw_output_path, part.partkey" in source
    assert "session.output_path, part.partkey" in source


# --- §20.4 の層の表との突き合わせ ---------------------------------------


def spec_layers() -> dict[str, set[str]]:
    """§20.4 の層の表を `層 → ND 番号の集合` で返す。

    **`ND-01〜ND-17` のような範囲表記を展開する。**件数のずれが最も危ない。
    """
    from tests.spec_sync import spec_section_text

    section = spec_section_text("20.4")
    layers: dict[str, set[str]] = {}
    for row in re.findall(r"^\| \*\*(コンテナ層|reaper 層|両層)\*\*（(.+?)） \|", section, re.M):
        name, listed = row
        numbers: set[str] = set()
        for piece in re.split(r"[、/]", listed):
            found = re.findall(r"ND-(\d+)", piece)
            if len(found) == 2 and "〜" in piece:
                numbers |= {f"ND-{n:02d}" for n in range(int(found[0]), int(found[1]) + 1)}
            else:
                numbers |= {f"ND-{int(n):02d}" for n in found}
        layers[name] = numbers
    return layers


def implemented_nd() -> set[str]:
    """このファイルが**実際にテストしている** ND 番号。

    **散文から拾わない。**テスト関数の名前と `parametrize` の ID だけを見る —
    docstring に「ND-18 は #54 で入る」と書いただけで「実装した」ことにならない。
    """
    body = Path(__file__).read_text(encoding="utf-8")
    found = re.findall(r"^def test_nd(\d+)", body, re.M)
    found += re.findall(r'"ND-(\d+)"', body)
    return {f"ND-{int(number):02d}" for number in found}


def test_every_nd_number_is_assigned_to_a_layer() -> None:
    """**ND-01〜ND-31 がすべてどれかの層に属すること**（§20.4 / v5.8→v5.9 の変更 U-2）。

    v5.8 まで表は ND-28 で止まっており、**ND-29 / 30 / 31 がどの層にも
    属していなかった。**割り当てが無い ND は誰も書かない。
    """
    layers = spec_layers()
    assigned: set[str] = set()
    for numbers in layers.values():
        assigned |= numbers
    expected = {f"ND-{n:02d}" for n in range(1, 32)}
    assert assigned == expected, sorted(expected - assigned)


def test_the_container_layer_is_implemented_here() -> None:
    """§20.4 のコンテナ層（+ 両層）の ND がこのファイルに在ること。

    **reaper 層は #54 と同時に入る。**ここには無い。
    """
    expected = spec_layers()["コンテナ層"] | spec_layers()["両層"]
    assert expected <= implemented_nd(), sorted(expected - implemented_nd())


def test_the_reaper_layer_lives_in_its_own_file() -> None:
    """**reaper 層をここで書いたつもりにならないこと。**

    reaper 層は `tests/unit/test_reaper.py` が `bash helper/voicedock-reaper` を
    実行して確かめる（§20.4）。書いていないものを「書いた」ことにすると、
    **最重要のテスト群に穴が空いたままリリース可能に見える。**
    """
    reaper_layer = spec_layers()["reaper 層"]
    assert reaper_layer & implemented_nd() == set(), "reaper 層をこのファイルに書かない"

    other = Path(__file__).with_name("test_reaper.py")
    found = re.findall(r"^def test_nd(\d+)", other.read_text(encoding="utf-8"), re.M)
    covered = {f"ND-{int(number):02d}" for number in found}
    assert reaper_layer <= covered, sorted(reaper_layer - covered)


# --- §10.12 結果の回収（reaper との往復） ------------------------------


def write_result(scene: Scene, *, status: str = "DELETED", detail: str = "", **extra: str) -> Path:
    """reaper が書いた体の結果を置く（§14.1.1 の結果の形式。v5.9→v5.10 の変更 X-1）。"""
    directory = scene.queue / cleaner.RESULT_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    request_id = extra.pop("request_id", "20260913T090000-slug-a1b2c3")
    payload = {
        "schema": 1,
        "request_id": request_id,
        "completed_at": "2026-09-13T09:00:00+09:00",
        "reaper_version": "5.9.0",
        "device_id": DEVICE_ID,
        "partkey": extra.pop("partkey", PARTKEY),
        "status": status,
        "detail": detail,
    }
    path = directory / f"{request_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def gone(scene: Scene) -> DeviceInventory:
    """デバイス上からそのファイルが消えた状態の `inventory.json`。"""
    return DeviceInventory(generated_at=NOW, mount_readonly=False, devices={DEVICE_ID: frozenset()})


def test_a_successful_result_completes_the_part(scene: Scene) -> None:
    """§10.12: 結果が成功 ∧ `inventory.json` でも不在 → `COMPLETED`。"""
    assert scene.part().status == PartStatus.SOURCE_DELETING
    result = write_result(scene)
    moved = scene.runner.collect_delete_results([scene.part()], gone(scene))

    assert moved == 1
    assert scene.part().status == PartStatus.COMPLETED
    assert scene.part().source_deleted_at is not None, "不可逆操作の記録が入らない"
    assert not result.exists(), "回収した結果を捨てていない"
    assert "source_deleted" in scene.log.getvalue()


def test_a_result_the_inventory_disagrees_with_goes_pending(scene: Scene) -> None:
    """**reaper の自己申告だけを信じない**（§10.12 / N-12）。

    `queue/result/` は reaper の申告であり、`state/inventory.json` は ingest が
    **独立に**走査した結果である。**両方が一致して初めて `COMPLETED` にする。**
    """
    write_result(scene)
    scene.runner.collect_delete_results([scene.part()], scene.inventory)

    assert scene.part().status == PartStatus.SOURCE_DELETE_PENDING
    assert scene.part().source_deleted_at is None, "消えていないのに記録している"
    assert "source_delete_pending" in scene.log.getvalue()


def test_an_identity_mismatch_goes_pending(scene: Scene) -> None:
    """reaper が同定に失敗 → `SOURCE_DELETE_PENDING`。**削除しない**（§10.12）。"""
    write_result(scene, status="SOURCE_IDENTITY_MISMATCH", detail="size_mismatch")
    scene.runner.collect_delete_results([scene.part()], gone(scene))

    assert scene.part().status == PartStatus.SOURCE_DELETE_PENDING
    assert "reason=size_mismatch" in scene.log.getvalue()


def test_an_unknown_inventory_does_not_complete(scene: Scene) -> None:
    """`inventory.json` が読めないときは `COMPLETED` にしない（§7.5 の「不明は安全側」）。"""
    write_result(scene)
    scene.runner.collect_delete_results([scene.part()], None)
    assert scene.part().status == PartStatus.SOURCE_DELETE_PENDING


def test_a_result_for_another_part_is_left_alone(scene: Scene) -> None:
    """**別の Part の結果は捨てない。**そのセッションを処理する周回で回収される。"""
    result = write_result(scene, partkey=f"{DEVICE_ID}/other/other.wav")
    moved = scene.runner.collect_delete_results([scene.part()], gone(scene))

    assert moved == 0
    assert result.exists()
    assert scene.part().status == PartStatus.SOURCE_DELETING


def test_a_result_for_a_part_that_moved_on_is_left_alone(scene: Scene) -> None:
    """**`SOURCE_DELETING` でない Part の結果には触れない。**

    **`partkey` が一致するかどうかだけでは足りない**（意図的に状態の条件を外したら
    テストが緑のまま通った）。再投入で新しい要求を出したあとに古い結果が届くと、
    **1 回の削除で 2 回 `COMPLETED` へ進めてしまう。**
    """
    scene.set_part(status=PartStatus.COMPLETED)
    result = write_result(scene)
    moved = scene.runner.collect_delete_results([scene.part()], gone(scene))

    assert moved == 0
    assert result.exists(), "別の周回で回収されるべき結果を捨てている"
    assert scene.part().status == PartStatus.COMPLETED
    assert scene.part().source_deleted_at is None


def test_a_missing_result_expires_and_withdraws_the_request(scene: Scene) -> None:
    """結果が来ないまま `delete_result_timeout_seconds` を過ぎたら取り下げる（§9.3）。

    **reaper が存在しない場合もここへ落ちる**（安全ロック 2-A）。§10.12 は
    「**これは正常な状態である**（Phase 7 前）」と規定している。

    **要求をキューに残したままにしない** — 再試行は新しい `request_id` で投入する
    （reaper の検証 11 がリプレイを拒むので、同じ ID では 2 度と通らない）。
    """
    assert scene.requests(), "要求が無い"
    old = datetime.fromisoformat(scene.part().updated_at)
    later = old.timestamp() + scene.cfg.cleanup.delete_result_timeout_seconds + 1

    runner = pipeline.Pipeline(
        database=scene.database,
        cfg=scene.cfg,
        log=Logger(level="DEBUG", fmt="text", stream=scene.log),
        now=datetime.fromtimestamp(later, tz=JST),
        state_root=scene.runner.state_root,
    )
    assert runner.collect_delete_results([scene.part()], scene.inventory) == 1

    assert scene.part().status == PartStatus.SOURCE_DELETE_PENDING
    assert scene.requests() == [], "古い要求がキューに残っている"


def test_a_fresh_request_is_not_expired(scene: Scene) -> None:
    """**タイムアウト前に取り下げない。**reaper は接続のたびにしか動かない。"""
    assert scene.runner.collect_delete_results([scene.part()], scene.inventory) == 0
    assert scene.part().status == PartStatus.SOURCE_DELETING
    assert len(scene.requests()) == 1
