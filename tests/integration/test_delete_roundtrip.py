"""**コンテナが入れた値で reaper が通ること**を、通しで確かめる（SPEC §14.1.1 / §10.2）。

**ここが空いていたから、4 時間半のずれが実機まで通った**（#151）。

- `tests/unit/test_no_delete.py` は `source_size` / `source_mtime` を**DB へ直接書く**
- `tests/unit/test_reaper.py` は要求を**手で組み立てる**
- **「`discover` が入れた値で reaper が通るか」を見るものが 1 つも無かった**

そのため `device.py` が inbox のコピーを `stat` していても（墓標ではなく）、
**どのテストも落ちなかった。**実機で三重ロックを全部外して初めて
`reason=mtime_mismatch` として現れた。

**この試験は「消えること」を見る。**消えないことを見る ND 群（§20.4）の対照である。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.fake_tree import DEVICE_ID, build_fake_inbox
from tests.helpers import REPO_ROOT
from voicedock import device
from voicedock.paths import DevicePath, partkey_for

REAPER = REPO_ROOT / "helper" / "voicedock-reaper"

pytestmark = pytest.mark.skipif(not REAPER.is_file(), reason="helper/ がマウントされていない")


def scan_one(inbox: Path) -> device.PartCandidate:
    """`discover` と同じ経路で inbox を走査し、`_orig` の候補を 1 件返す。"""
    found = device.list_inbox_parts(inbox, tz=ZoneInfo("Asia/Tokyo"))
    assert found.parts, found.skipped
    return found.parts[0]


def test_the_container_values_pass_the_reaper(tmp_path: Path) -> None:
    """**墓標 → discover → 要求 → reaper が通り、ファイルが消えること。**

    デバイス側のファイルには**墓標の `mtime`** を与える（それが実機の姿である）。
    inbox のコピーは**別の時刻**を持つ —— `device.py` がそちらを読んだら
    §14.1.1 の検証 10 が落ちる。
    """
    inbox = build_fake_inbox(tmp_path / "inbox", with_noise=False)
    candidate = scan_one(inbox)

    # デバイス側を組み立てる。**墓標が言う時刻とサイズにする**
    volumes = tmp_path / "Volumes"
    target = volumes / DEVICE_ID / str(candidate.source.relpath)
    target.parent.mkdir(parents=True)
    inbox_copy = Path(str(candidate.source.inbox_path))
    target.write_bytes(inbox_copy.read_bytes())
    os.utime(target, (candidate.source.mtime, candidate.source.mtime))

    # **inbox のコピーと mtime が違うことを先に確かめる。**同じなら、この試験は
    # 「どちらを読んでも通る」状態を見ているだけになる
    assert abs(inbox_copy.stat().st_mtime - candidate.source.mtime) > 2.0, (
        "墓標と inbox コピーの mtime が近すぎる。これでは出どころの違いを検出できない"
    )

    home = _host(tmp_path, volumes)
    _write_request(home, candidate)

    result = subprocess.run(  # noqa: S603 - argv はリスト。shell=True を使わない
        ["/bin/bash", str(REAPER)],
        capture_output=True,
        text=True,
        env={**os.environ, "VOICEDOCK_HELPER_CONF": str(home / "helper.conf")},
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not target.exists(), f"削除されていない: {result.stdout}{result.stderr}"
    assert [r["status"] for r in _results(home)] == ["DELETED"]


def test_a_copy_timestamp_would_be_rejected(tmp_path: Path) -> None:
    """**陰性対照。**inbox コピーの `mtime` を使うと reaper が拒否すること。

    これが #151 で実際に起きていた姿である。**この試験が落ちるなら、上の試験は
    「どちらを読んでも通る」状態を見ているだけ**ということになる。
    """
    inbox = build_fake_inbox(tmp_path / "inbox", with_noise=False)
    candidate = scan_one(inbox)

    volumes = tmp_path / "Volumes"
    target = volumes / DEVICE_ID / str(candidate.source.relpath)
    target.parent.mkdir(parents=True)
    inbox_copy = Path(str(candidate.source.inbox_path))
    target.write_bytes(inbox_copy.read_bytes())
    os.utime(target, (candidate.source.mtime, candidate.source.mtime))

    home = _host(tmp_path, volumes)
    _write_request(home, candidate, mtime=inbox_copy.stat().st_mtime)

    subprocess.run(  # noqa: S603
        ["/bin/bash", str(REAPER)],
        capture_output=True,
        text=True,
        env={**os.environ, "VOICEDOCK_HELPER_CONF": str(home / "helper.conf")},
        timeout=60,
        check=False,
    )

    assert target.exists(), "コピーの時刻でも削除された（検証 10 が効いていない）"
    assert [r["status"] for r in _results(home)] == ["SOURCE_IDENTITY_MISMATCH"]


# --- 組み立て -----------------------------------------------------------


def _host(tmp_path: Path, volumes: Path) -> Path:
    """**三重ロックをすべて外した**ホスト（§20.4）。"""
    home = tmp_path / "VoiceDock"
    for name in ("queue/delete", "queue/result", "state", "log"):
        (home / name).mkdir(parents=True)
    (home / "helper.conf").write_text(
        f"VOLUMES_ROOT={volumes}\n"
        "INCLUDE_VOLUMES=()\n"
        'EXCLUDE_VOLUMES=(".*")\n'
        "MOUNT_MODE=rw\n"
        "DELETE_SOURCE_AUDIO=true\n"
        f'VOICEDOCK_HOME="{home}"\n',
        encoding="utf-8",
    )
    (home / "state" / "heartbeat.json").write_text(
        '{\n  "schema": 1,\n  "mount_readonly": false\n}\n', encoding="utf-8"
    )
    return home


def _write_request(
    home: Path, candidate: device.PartCandidate, *, mtime: float | None = None
) -> None:
    """`cleaner` が書くのと同じ形の要求（§14.1.1）。**値は候補から取る。**"""
    relpath = str(candidate.source.relpath)
    partkey = partkey_for(DEVICE_ID, DevicePath(PurePosixPath(relpath)))
    payload = {
        "schema": 1,
        "request_id": "20260913T090000-abcdef0123456789-a1b2c3",
        "created_at": "2026-09-13T09:00:00+09:00",
        "device_id": DEVICE_ID,
        "partkey": str(partkey),
        "session_key": f"{DEVICE_ID}:20260912",
        "targets": [
            {
                "relpath": relpath,
                "size": candidate.source.size,
                "mtime": candidate.source.mtime if mtime is None else mtime,
            }
        ],
    }
    path = home / "queue" / "delete" / f"{payload['request_id']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _results(home: Path) -> list[dict[str, str]]:
    directory = home / "queue" / "result"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]
