"""削除禁止テスト ND-18〜20 / 24〜29 の reaper 層（SPEC §20.4 / §14.1.1）。

**`voicedock-reaper` はデバイス上のファイルを削除できる唯一のプログラムである**（N-16）。
ここで assert するのは 1 つだけ —— **偽装・改竄した要求を置いても偽ボリューム上の
ファイルが消えないこと。**

**すべて三重ロックをすべて解除した状態で実行する。**ロックが掛かっているから消えない、
では検証にならない（§20.4）。

reaper を pytest から実行できるのは、**マウント操作を一切行わない**（N-19）ためである。
`stat` の BSD / GNU 差だけを内部の薄いラッパで吸収するので、Linux のテストコンテナでも
そのまま動く。これにより `make test` 1 コマンドで ND 群が全部回る。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT

REAPER = REPO_ROOT / "helper" / "voicedock-reaper"

pytestmark = pytest.mark.skipif(not REAPER.is_file(), reason="helper/ がマウントされていない")

DEVICE_ID = "DJIMIC3"
FOLDER = "TX_MIC001_20260912_090000"
FILENAME = "TX00_MIC001_20260912_090000_orig.wav"
RELPATH = f"{FOLDER}/{FILENAME}"
PARTKEY = f"{DEVICE_ID}/{RELPATH}"
CONTENT = b"x" * 4096
MTIME = 1787000000.0


@dataclass
class Bench:
    """**三重ロックをすべて解除した**ホスト。"""

    home: Path
    volumes: Path

    @property
    def target(self) -> Path:
        return self.volumes / DEVICE_ID / RELPATH

    @property
    def queue(self) -> Path:
        return self.home / "queue" / "delete"

    def results(self) -> list[dict[str, str]]:
        directory = self.home / "queue" / "result"
        if not directory.is_dir():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]

    def request(self, **overrides: object) -> Path:
        payload: dict[str, object] = {
            "schema": 1,
            "request_id": "20260913T090000-abcdef0123456789-a1b2c3",
            "created_at": "2026-09-13T09:00:00+09:00",
            "device_id": DEVICE_ID,
            "partkey": PARTKEY,
            "session_key": f"{DEVICE_ID}:20260912",
            "targets": [{"relpath": RELPATH, "size": len(CONTENT), "mtime": MTIME}],
        }
        target = dict(payload["targets"][0])  # type: ignore[index]
        for key, value in overrides.items():
            if key in ("relpath", "size", "mtime"):
                target[key] = value
            else:
                payload[key] = value
        payload["targets"] = [target]
        self.queue.mkdir(parents=True, exist_ok=True)
        path = self.queue / f"{payload['request_id']}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def run(self) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["VOICEDOCK_HELPER_CONF"] = str(self.home / "helper.conf")
        return subprocess.run(  # noqa: S603 - argv はリスト。shell=True を使わない
            ["/bin/bash", str(REAPER)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
            check=False,
        )


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    """**ロックを全部外した**状態を組み立てる（§20.4）。"""
    home = tmp_path / "VoiceDock"
    for name in ("queue/delete", "queue/result", "state", "log"):
        (home / name).mkdir(parents=True)
    volumes = tmp_path / "Volumes"
    target = volumes / DEVICE_ID / RELPATH
    target.parent.mkdir(parents=True)
    target.write_bytes(CONTENT)
    os.utime(target, (MTIME, MTIME))

    (home / "helper.conf").write_text(
        f"VOLUMES_ROOT={volumes}\n"
        "INCLUDE_VOLUMES=()\n"
        'EXCLUDE_VOLUMES=(".*")\n'
        # **安全ロック 1 と 2-B を外す**
        "MOUNT_MODE=rw\n"
        "DELETE_SOURCE_AUDIO=true\n"
        f'VOICEDOCK_HOME="{home}"\n',
        encoding="utf-8",
    )
    (home / "state" / "heartbeat.json").write_text(
        '{\n  "schema": 1,\n  "mount_readonly": false\n}\n', encoding="utf-8"
    )
    yield Bench(home=home, volumes=volumes)


# --- 正の対照（これが無いと ND 群は何も検証していない） ------------------


def test_a_valid_request_actually_deletes(bench: Bench) -> None:
    """**すべて揃っていれば削除されること。**

    §20.4 が要求する「テストが本当に効いていることの証明」である。これが緑でなければ、
    **ND が全部緑なのは「安全だから」ではなく「そもそも reaper が何もしていないから」**
    である。
    """
    bench.request()
    result = bench.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not bench.target.exists(), "削除されていない"
    assert [r["status"] for r in bench.results()] == ["DELETED"]
    assert "source_deleted" in result.stdout
    assert bench.queue.glob("*.json") is not None
    assert list(bench.queue.glob("*.json")) == [], "処理済みの要求が残っている"


# --- ND-18 / ND-19: 直前に中身が変わった（検証 10） ---------------------


def test_nd18_a_size_change_blocks_the_delete(bench: Bench) -> None:
    """ND-18: 削除直前にファイルサイズが変わる → `SOURCE_IDENTITY_MISMATCH`。"""
    bench.request(size=len(CONTENT) + 1)
    bench.run()
    assert bench.target.exists()
    assert [r["status"] for r in bench.results()] == ["SOURCE_IDENTITY_MISMATCH"]
    assert bench.results()[0]["detail"] == "size_mismatch"


def test_nd19_an_mtime_change_blocks_the_delete(bench: Bench) -> None:
    """ND-19: 削除直前に mtime が変わる。**許容差は 2.0 秒未満**（§14.1.1 検証 10）。"""
    bench.request(mtime=MTIME + 120)
    bench.run()
    assert bench.target.exists()
    assert bench.results()[0]["detail"] == "mtime_mismatch"


def test_a_small_mtime_drift_is_tolerated(bench: Bench) -> None:
    """**2 秒未満のずれは許す。**ファイルシステムの粒度でここを厳密にすると通らない。"""
    bench.request(mtime=MTIME + 1)
    bench.run()
    assert not bench.target.exists()


# --- ND-20 / ND-25: symlink（検証 5 / 6） -------------------------------


def test_nd20_a_symlink_target_is_never_deleted(bench: Bench) -> None:
    """ND-20: 対象自身が symlink → **他ボリュームのファイルに触れない**（検証 6）。"""
    outside = bench.volumes.parent / "outside.wav"
    outside.write_bytes(CONTENT)
    bench.target.unlink()
    bench.target.symlink_to(outside)
    bench.request()

    bench.run()
    assert outside.exists(), "symlink の先を消している"
    assert bench.target.is_symlink()
    assert bench.results()[0]["detail"] == "target_is_symlink"


def test_nd25_a_symlink_in_the_path_is_never_followed(bench: Bench) -> None:
    """ND-25: **経路の途中に symlink**（`/Volumes/Macintosh HD` 経由など。検証 5）。

    macOS には `/Volumes/Macintosh HD` が `/` への symlink として実在する。
    **字句的な判定では「デバイス配下」と偽装されたパスを拒否できない**
    （§22 R-19。`docs/POC.md` §5.1 で実証済み）。
    """
    outside_root = bench.volumes.parent / "Elsewhere"
    (outside_root / FOLDER).mkdir(parents=True)
    victim = outside_root / RELPATH
    victim.write_bytes(CONTENT)
    os.utime(victim, (MTIME, MTIME))

    escape = bench.volumes / DEVICE_ID / "link"
    escape.symlink_to(outside_root)
    bench.request(relpath=f"link/{RELPATH}", partkey=f"{DEVICE_ID}/link/{RELPATH}")

    bench.run()
    assert victim.exists(), "symlink 経由でボリューム外のファイルを消している"


# --- ND-24: パストラバーサル（検証 4） ---------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        f"../Elsewhere/{RELPATH}",
        f"{FOLDER}/../../{RELPATH}",
        f"/{RELPATH}",
        f"{FOLDER}//{FILENAME}",
    ],
)
def test_nd24_a_traversal_relpath_is_refused(bench: Bench, rel: str) -> None:
    """ND-24: 要求の `relpath` に `../` / 先頭 `/` / 空要素（検証 4）。

    → **ボリューム外のファイルに触れない。**
    """
    bench.request(relpath=rel, partkey=f"{DEVICE_ID}/{rel}")
    bench.run()
    assert bench.target.exists()
    assert bench.results()[0]["status"] == "SOURCE_IDENTITY_MISMATCH"


# --- ND-26: reaper が存在しない（ロック 2-A） --------------------------


def test_nd26_without_the_reaper_nothing_happens(bench: Bench) -> None:
    """ND-26: `voicedock-reaper` が存在しない（安全ロック 2-A）。

    → **何も起きない。**要求はキューに残り、コンテナ側がタイムアウトで
    `SOURCE_DELETE_PENDING` にする（§10.12）。**これは Phase 7 前の正常な状態である。**

    ここでは「reaper を実行しなければ何も消えない」ことを確かめる。**配置されて
    いないことそのもの**は `test_helper_portability.py` の
    `test_install_does_not_place_the_reaper_by_default` が見ている。
    """
    request = bench.request()
    assert bench.target.exists()
    assert request.is_file(), "要求はキューに残る"
    assert bench.results() == []


# --- ND-27: リプレイ（検証 11） ----------------------------------------


def test_nd27_a_replayed_request_id_is_refused(bench: Bench) -> None:
    """ND-27: 同じ `request_id` を 2 回投入する（検証 11）。

    → **既に削除済みの ID で別のファイルを消せない。**
    """
    bench.request()
    bench.run()
    assert not bench.target.exists()

    # 同じ ID で、別のファイルを指す要求をもう一度置く
    other_folder = "TX_MIC001_20260912_100000"
    other_rel = f"{other_folder}/TX00_MIC001_20260912_100000_orig.wav"
    victim = bench.volumes / DEVICE_ID / other_rel
    victim.parent.mkdir(parents=True)
    victim.write_bytes(CONTENT)
    os.utime(victim, (MTIME, MTIME))
    bench.request(relpath=other_rel, partkey=f"{DEVICE_ID}/{other_rel}")

    bench.run()
    assert victim.exists(), "リプレイで別のファイルを消している"


# --- ND-28: `.Trashes` など（検証 9） ----------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        f".Trashes/501/{RELPATH}",
        f".Spotlight-V100/{RELPATH}",
        f"{FOLDER}/.{FILENAME}",
    ],
)
def test_nd28_a_dot_prefixed_element_is_refused(bench: Bench, rel: str) -> None:
    """ND-28: `relpath` の各要素が `.` で始まらないこと（検証 9）。

    → **ユーザーが意図的に捨てた録音に触れない**（§22 R-22）。`.Spotlight-V100` /
    `.fseventsd` も同じ規則で守られる。
    """
    victim = bench.volumes / DEVICE_ID / rel
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(CONTENT)
    os.utime(victim, (MTIME, MTIME))
    bench.request(relpath=rel, partkey=f"{DEVICE_ID}/{rel}")

    bench.run()
    assert victim.exists(), f"{rel} を消している"


# --- ND-29: 親ディレクトリ名の規則（検証 8） ---------------------------


def test_nd29_a_folder_that_breaks_the_rule_is_refused(bench: Bench) -> None:
    """ND-29: 親ディレクトリ名が `RECORDING_FOLDER_RE` に一致しない（検証 8）。

    **v3.x に無かった追加の防壁である。**ファイル名だけを見ていると、DJI の
    フォルダ構造の外に置かれた同名ファイルを消しうる。
    """
    rel = f"Downloads/{FILENAME}"
    victim = bench.volumes / DEVICE_ID / rel
    victim.parent.mkdir(parents=True)
    victim.write_bytes(CONTENT)
    os.utime(victim, (MTIME, MTIME))
    bench.request(relpath=rel, partkey=f"{DEVICE_ID}/{rel}")

    bench.run()
    assert victim.exists()
    assert bench.results()[0]["detail"] == "folder_rule"


def test_a_filename_that_breaks_the_rule_is_refused(bench: Bench) -> None:
    """検証 7: ファイル名が `RECORDING_FILENAME_RE` に一致しないこと。"""
    rel = f"{FOLDER}/NOTES.txt"
    victim = bench.volumes / DEVICE_ID / rel
    victim.write_bytes(CONTENT)
    os.utime(victim, (MTIME, MTIME))
    bench.request(relpath=rel, partkey=f"{DEVICE_ID}/{rel}")

    bench.run()
    assert victim.exists()
    assert bench.results()[0]["detail"] == "filename_rule"


# --- 検証 3 / 12 -------------------------------------------------------


def test_an_absent_device_leaves_the_request_in_the_queue(bench: Bench) -> None:
    """検証 3: `device_id` と同名のボリュームが現在マウントされていない。

    **要求は消さない。**デバイスを繋ぎ直せば次回処理できる（§10.12）。
    """
    request = bench.request(device_id="OTHER", partkey=f"OTHER/{RELPATH}")
    bench.run()
    assert bench.target.exists()
    assert request.is_file(), "要求をキューから取り下げている"


def test_a_partkey_that_disagrees_is_refused(bench: Bench) -> None:
    """検証 12: `<device_id>/<relpath>` が要求の `partkey` と一致すること。

    **一致していなければ「ノートで確認したファイル」と「消すファイル」が別物である**
    （v5.1 で追加）。
    """
    other = "TX_MIC001_20260101_000000/TX00_MIC001_20260101_000000_orig.wav"
    bench.request(partkey=f"{DEVICE_ID}/{other}")
    bench.run()
    assert bench.target.exists()
    assert bench.results()[0]["detail"] == "partkey_mismatch"


# --- ND-22 / ND-23: 三重ロック（reaper 側） ----------------------------


def test_nd22_lock_one_stops_the_reaper(bench: Bench) -> None:
    """ND-22: `helper.conf` の `DELETE_SOURCE_AUDIO=false`（検証 1）。"""
    conf = bench.home / "helper.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8").replace(
            "DELETE_SOURCE_AUDIO=true", "DELETE_SOURCE_AUDIO=false"
        )
    )
    bench.request()
    result = bench.run()
    assert bench.target.exists()
    assert "reaper_disabled" in result.stdout


def test_nd23_lock_two_b_stops_the_reaper(bench: Bench) -> None:
    """ND-23: `MOUNT_MODE=ro`（検証 2）。"""
    conf = bench.home / "helper.conf"
    conf.write_text(conf.read_text(encoding="utf-8").replace("MOUNT_MODE=rw", "MOUNT_MODE=ro"))
    bench.request()
    result = bench.run()
    assert bench.target.exists()
    assert "reaper_disabled" in result.stdout


def test_nd23_a_readonly_heartbeat_stops_the_reaper(bench: Bench) -> None:
    """ND-23: `heartbeat.json` の `mount_readonly` が真（検証 2 の後半）。

    **`helper.conf` が `rw` でも、実際に readOnly で載っていれば削除しない。**
    OS が拒否するが、**試みる前に落とす。**
    """
    (bench.home / "state" / "heartbeat.json").write_text(
        '{\n  "schema": 1,\n  "mount_readonly": true\n}\n', encoding="utf-8"
    )
    bench.request()
    result = bench.run()
    assert bench.target.exists()
    assert "reaper_disabled" in result.stdout


# --- N-16 / N-19 の静的確認 --------------------------------------------


def test_the_reaper_never_recurses_or_mounts() -> None:
    """**再帰削除もマウント操作も書かない**（N-16 / N-19）。

    再帰削除を許すと、1 つの壊れた `relpath` でフォルダごと消えうる。マウント操作を
    書くと Linux で動かせなくなり、**§20.4 を CI で回せなくなる。**
    """
    body = REAPER.read_text(encoding="utf-8")
    assert "diskutil" not in body
    assert re.search(r"rm\s+-[a-zA-Z]*[rR]", body) is None
    assert "rmdir" not in body
    assert "shutil" not in body
