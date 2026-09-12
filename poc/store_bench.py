#!/usr/bin/env python3
"""ストア方式（SQLite / ファイル）の照会・書き込み性能を実測する。

`docs/STORE.md` の表の出所である。**数字を誰でも取り直せるようにするためにある。**

```bash
docker build --target dev -t voicedock:dev .
docker run --rm -v "$PWD/poc:/poc:ro" voicedock:dev python /poc/store_bench.py /tmp/bench
```

計測するのは、SPEC が実際に行う 4 つの操作だけである。

| # | 操作 | どこで使うか |
|---|---|---|
| 1 | 状態別の件数集計 | §17.2 `status` の Parts / Sessions のバケット |
| 2 | partkey の重複判定 | §10.2 の Part 登録（論理的な二重登録の防止） |
| 3 | sha256 の重複判定 | §10.5 の `DUPLICATE_CONTENT` |
| 4 | 1 件の状態遷移 | §8.4 `record_transition()`（status UPDATE + events INSERT） |

**標準ライブラリだけを使う。**このスクリプトはイメージに入らず、`poc/` から
読み取り専用でマウントして走らせる（`poc/mkimg.sh` / `poc/tmo` と同じ扱い）。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import sqlite3
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

YEARS = 3
PARTS_PER_DAY = 32
EVENTS_PER_REC = 7
DAYS = 365 * YEARS
N_REC = DAYS * PARTS_PER_DAY
N_SES = DAYS

# §9.1 の状態の出方をおおよそ模す。100 個の配列から選ぶので百分率になる。
STATUS_MIX = ["COMPLETED"] * 90 + ["SKIPPED"] * 6 + ["FAILED"] * 2 + ["RAW_SAVED", "DISCOVERED"]

DEVICE_ID = "DJIMIC3"
REPEAT = 5
"""1 回目を cold、残りを warm として中央値を取る。"""

REPEAT_WRITE = 21
"""書き込みは分散が大きいので回数を増やす。"""

random.seed(0)


def partkey(i: int) -> str:
    """§8.2 の `partkey`（`<device_id>/<source_folder>/<filename>`）を模す。"""
    folder = f"TX_MIC001_{i:08d}"
    name = f"TX01_MIC{i % 4 + 1:03d}_{i:08d}_orig.wav"
    return f"{DEVICE_ID}/{folder}/{name}"


def timed(label: str, fn: Callable[[], Any], repeat: int = REPEAT) -> None:
    cold: float | None = None
    warm: list[float] = []
    out: Any = None
    for i in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        dt = (time.perf_counter() - t0) * 1000
        if i == 0:
            cold = dt
        else:
            warm.append(dt)
    assert cold is not None
    median = statistics.median(warm) if warm else cold
    shown = out if not isinstance(out, dict) else {k: out[k] for k in sorted(out)}
    print(f"  {label:<48} cold {cold:8.2f} ms   warm {median:8.2f} ms   {shown}")


# --------------------------------------------------------------- SQLite

SCHEMA_KEY = """
CREATE TABLE recordings (
    partkey          TEXT PRIMARY KEY NOT NULL,
    device_id        TEXT NOT NULL,
    source_folder    TEXT NOT NULL,
    transmitter_id   TEXT NOT NULL,
    mic_index        INTEGER NOT NULL,
    started_at       TEXT NOT NULL,
    duration_seconds REAL,
    source_path      TEXT,
    source_size      INTEGER,
    source_mtime     REAL,
    sha256           TEXT,
    session_key      TEXT REFERENCES sessions(session_key) ON DELETE SET NULL,
    status           TEXT NOT NULL,
    retry_count      INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_recordings_sha ON recordings (sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX idx_recordings_status  ON recordings (status);
CREATE INDEX idx_recordings_session ON recordings (session_key);
CREATE INDEX idx_recordings_started ON recordings (started_at);
CREATE TABLE sessions (
    session_key TEXT PRIMARY KEY NOT NULL,
    day_date    TEXT NOT NULL,
    status      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_key  TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    error_code  TEXT,
    detail      TEXT
);
CREATE INDEX idx_events_entity ON events (entity_type, entity_key, id);
"""

SCHEMA_INT = SCHEMA_KEY.replace("entity_key  TEXT NOT NULL", "entity_key  INTEGER NOT NULL")


def open_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def build_sqlite(path: Path, *, entity_key_is_text: bool) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    conn = open_rw(path)
    conn.executescript(SCHEMA_KEY if entity_key_is_text else SCHEMA_INT)
    moment = "2026-01-01T00:00:00+09:00"
    with conn:
        conn.executemany(
            "INSERT INTO sessions (session_key, day_date, status, updated_at) VALUES (?,?,?,?)",
            [(f"{DEVICE_ID}:2026{d:04d}", "2026-01-01", "COMPLETED", moment) for d in range(N_SES)],
        )
        conn.executemany(
            "INSERT INTO recordings (partkey, device_id, source_folder, transmitter_id,"
            " mic_index, started_at, duration_seconds, source_path, source_size, source_mtime,"
            " sha256, session_key, status, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    key := partkey(i),
                    DEVICE_ID,
                    f"TX_MIC001_{i:08d}",
                    "TX01",
                    i % 4 + 1,
                    f"2026-01-01T{i % 24:02d}:00:00+09:00",
                    1800.0,
                    key.split("/", 1)[1],
                    345_600_000,
                    1_787_000_000.0,
                    f"{i:064x}",
                    f"{DEVICE_ID}:2026{i // PARTS_PER_DAY:04d}",
                    random.choice(STATUS_MIX),
                    moment,
                )
                for i in range(N_REC)
            ],
        )
        conn.executemany(
            "INSERT INTO events (created_at, entity_type, entity_key, from_status, to_status)"
            " VALUES (?,?,?,?,?)",
            [
                (
                    moment,
                    "recording",
                    partkey(i // EVENTS_PER_REC) if entity_key_is_text else i // EVENTS_PER_REC,
                    "NORMALIZING",
                    "NORMALIZED",
                )
                for i in range(N_REC * EVENTS_PER_REC)
            ],
        )
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


# ---------------------------------------------------------------- files


def build_files(root: Path) -> None:
    """1 エンティティ 1 JSON。`events` を中に持つ（os.replace 1 回で原子性を得る形）。"""
    if root.exists():
        shutil.rmtree(root)
    moment = "2026-01-01T00:00:00+09:00"
    for i in range(N_REC):
        key = partkey(i)
        day = i // PARTS_PER_DAY
        directory = root / "recordings" / f"{day:05d}"
        directory.mkdir(parents=True, exist_ok=True)
        doc = {
            "partkey": key,
            "device_id": DEVICE_ID,
            "source_folder": f"TX_MIC001_{i:08d}",
            "transmitter_id": "TX01",
            "mic_index": i % 4 + 1,
            "started_at": f"2026-01-01T{i % 24:02d}:00:00+09:00",
            "duration_seconds": 1800.0,
            "source_path": key.split("/", 1)[1],
            "source_size": 345_600_000,
            "source_mtime": 1_787_000_000.0,
            "sha256": f"{i:064x}",
            "session_key": f"{DEVICE_ID}:2026{day:04d}",
            "status": random.choice(STATUS_MIX),
            "retry_count": 0,
            "updated_at": moment,
            "events": [
                {"created_at": moment, "from": "NORMALIZING", "to": "NORMALIZED"}
                for _ in range(EVENTS_PER_REC)
            ],
        }
        (directory / f"{hashlib.sha256(key.encode()).hexdigest()[:16]}.json").write_text(
            json.dumps(doc), encoding="utf-8"
        )
    # sha256 の一意性索引（O_EXCL マーカー）。エンティティ本体とは別の書き込みになる
    shadir = root / "by-sha"
    shadir.mkdir(parents=True, exist_ok=True)
    for i in range(0, N_REC, 1000):  # 代表だけ作る（計測には 1 件あれば足りる）
        (shadir / f"{i:064x}").write_text(partkey(i), encoding="utf-8")


def count_by_reading(root: Path) -> dict[str, int]:
    """全 JSON を読んで status を数える。"""
    counts: dict[str, int] = {}
    for day in os.scandir(root / "recordings"):
        for entry in os.scandir(day.path):
            with open(entry.path, "rb") as handle:
                status = json.loads(handle.read())["status"]
            counts[status] = counts.get(status, 0) + 1
    return counts


def count_by_name(root: Path) -> int:
    """ファイル名だけを見る（status をファイル名に載せた場合の下限）。"""
    return sum(len(os.listdir(day.path)) for day in os.scandir(root / "recordings"))


def dir_size(root: Path) -> int:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


# ----------------------------------------------------------------- main


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/store_bench")
    root.mkdir(parents=True, exist_ok=True)
    db_key = root / "key.db"
    db_int = root / "int.db"
    files = root / "store"

    print(f"規模: {YEARS} 年分 = recordings {N_REC:,} / sessions {N_SES:,} "
          f"/ events {N_REC * EVENTS_PER_REC:,}")
    print()

    t0 = time.perf_counter()
    build_sqlite(db_key, entity_key_is_text=True)
    key_bytes = db_key.stat().st_size
    print(f"  sqlite（events.entity_key TEXT）    構築 {time.perf_counter() - t0:5.1f}s"
          f"  {key_bytes / 2**20:6.1f} MiB")

    t0 = time.perf_counter()
    build_sqlite(db_int, entity_key_is_text=False)
    int_bytes = db_int.stat().st_size
    print(f"  sqlite（events.entity_key INTEGER） 構築 {time.perf_counter() - t0:5.1f}s"
          f"  {int_bytes / 2**20:6.1f} MiB"
          f"   ← 自然キー化の代償 +{(key_bytes - int_bytes) / 2**20:.1f} MiB")

    t0 = time.perf_counter()
    build_files(files)
    print(f"  files                              構築 {time.perf_counter() - t0:5.1f}s"
          f"  {dir_size(files) / 2**20:6.1f} MiB  ({N_REC:,} ファイル)")
    print()

    conn = sqlite3.connect(f"file:{db_key}?mode=ro", uri=True)

    print("1. 状態別の件数集計（§17.2 status）")
    timed("SQLite  GROUP BY status", lambda: dict(
        conn.execute("SELECT status, COUNT(*) FROM recordings GROUP BY status")))
    timed("ファイル 全 JSON を読む", lambda: count_by_reading(files))
    timed("ファイル 名前だけ（下限）", lambda: count_by_name(files))
    print()

    print("2. partkey の重複判定（§10.2）")
    probe_key = partkey(12_345)
    timed("SQLite  PRIMARY KEY で引く",
          lambda: conn.execute(
              "SELECT partkey FROM recordings WHERE partkey = ?", (probe_key,)).fetchone())
    probe_file = (files / "recordings" / f"{12_345 // PARTS_PER_DAY:05d}"
                  / f"{hashlib.sha256(probe_key.encode()).hexdigest()[:16]}.json")
    timed("ファイル パスの存在確認", lambda: probe_file.exists())
    print()

    print("3. sha256 の重複判定（§10.5 DUPLICATE_CONTENT）")
    timed("SQLite  部分 UNIQUE 索引で引く",
          lambda: conn.execute(
              "SELECT partkey FROM recordings WHERE sha256 = ?", (f"{12000:064x}",)).fetchone())
    marker = files / "by-sha" / f"{12000:064x}"
    timed("ファイル マーカーの存在確認", lambda: marker.exists())
    print()

    print("4. 1 件の状態遷移（§8.4 record_transition）")
    writer = open_rw(db_key)
    first_key = partkey(0)

    def sqlite_transition() -> str:
        with writer:
            current = writer.execute(
                "SELECT status FROM recordings WHERE partkey = ?", (first_key,)).fetchone()[0]
            cursor = writer.execute(
                "UPDATE recordings SET status = ?, updated_at = ?"
                " WHERE partkey = ? AND status = ?",
                ("COMPLETED", "2026-01-02T00:00:00+09:00", first_key, current))
            if cursor.rowcount != 1:
                raise RuntimeError("遷移できませんでした")
            writer.execute(
                "INSERT INTO events (created_at, entity_type, entity_key, from_status, to_status)"
                " VALUES (?,?,?,?,?)",
                ("2026-01-02T00:00:00+09:00", "recording", first_key, current, "COMPLETED"))
        return "ok"

    timed("SQLite  1 トランザクション", sqlite_transition, repeat=REPEAT_WRITE)

    target = (files / "recordings" / "00000"
              / f"{hashlib.sha256(first_key.encode()).hexdigest()[:16]}.json")
    doc = json.loads(target.read_text(encoding="utf-8"))

    def file_transition() -> str:
        doc["status"] = "COMPLETED"
        doc["events"].append({"created_at": "x", "from": "RAW_SAVED", "to": "COMPLETED"})
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(json.dumps(doc).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        dirfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return "ok"

    timed("ファイル os.replace + fsync ×2", file_transition, repeat=REPEAT_WRITE)

    writer.close()
    conn.close()


if __name__ == "__main__":
    main()
