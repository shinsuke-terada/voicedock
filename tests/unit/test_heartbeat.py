"""`voicedock.heartbeat` が SPEC §7.5 と一致することを固定する。

**このモジュールは例外を投げない。**壊れた JSON も欠落したキーも「不明」として扱い、
不明は常に安全側（削除しない / 警告する）へ倒す。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from voicedock.heartbeat import Heartbeat, read_heartbeat

NOW = datetime(2026, 8, 30, 7, 0, 12, tzinfo=ZoneInfo("Asia/Tokyo"))

# SPEC §7.5 の例そのまま
SPEC_EXAMPLE: dict[str, Any] = {
    "schema": 1,
    "updated_at": "2026-08-30T07:00:12+09:00",
    "helper_version": "4.2.0",
    "mount_mode": "ro",
    "mount_readonly": True,
    "delete_source_audio": False,
    "reaper_installed": False,
    "include_volumes": [],
    "exclude_volumes": ["Macintosh HD", "com.apple.TimeMachine.*", ".*"],
    "config_error": None,
}


def write(state_root: Path, text: str) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "heartbeat.json").write_text(text, encoding="utf-8")


def test_reads_the_spec_example(tmp_path: Path) -> None:
    write(tmp_path, json.dumps(SPEC_EXAMPLE))
    beat = read_heartbeat(tmp_path)
    assert beat is not None
    assert beat.schema_version == 1
    assert beat.updated_at == NOW
    assert beat.helper_version == "4.2.0"
    assert beat.mount_mode == "ro"
    assert beat.mount_readonly is True
    assert beat.delete_source_audio is False
    assert beat.reaper_installed is False
    assert beat.include_volumes == ()
    assert beat.exclude_volumes == ("Macintosh HD", "com.apple.TimeMachine.*", ".*")
    assert beat.config_error is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_heartbeat(tmp_path / "nope") is None


@pytest.mark.parametrize(
    "text",
    [
        "{ not json",
        "",
        "[1, 2]",
        '"a string"',
        '{"updated_at": "not-a-timestamp"}',
        '{"mount_readonly": "maybe"}',
    ],
)
def test_unreadable_content_returns_none(tmp_path: Path, text: str) -> None:
    """**例外を投げない**（§7.5）。Helper の書き込み途中を読む可能性がある。"""
    write(tmp_path, text)
    assert read_heartbeat(tmp_path) is None


def test_unknown_fields_are_ignored(tmp_path: Path) -> None:
    """`config.yaml` と非対称に `extra="ignore"`。Helper の版が進んでも壊れない（§7.5）。"""
    write(tmp_path, json.dumps({**SPEC_EXAMPLE, "brand_new_field": 42}))
    beat = read_heartbeat(tmp_path)
    assert beat is not None
    assert beat.mount_readonly is True


def test_absent_fields_are_unknown(tmp_path: Path) -> None:
    write(tmp_path, "{}")
    beat = read_heartbeat(tmp_path)
    assert beat is not None
    assert beat.updated_at is None
    assert beat.delete_source_audio is None
    assert beat.mount_readonly is None
    assert beat.include_volumes == ()


def test_age_seconds(tmp_path: Path) -> None:
    write(tmp_path, json.dumps({"updated_at": NOW.isoformat()}))
    beat = read_heartbeat(tmp_path)
    assert beat is not None
    assert beat.age_seconds(NOW + timedelta(seconds=42)) == 42.0


def test_age_seconds_is_none_without_updated_at(tmp_path: Path) -> None:
    write(tmp_path, "{}")
    beat = read_heartbeat(tmp_path)
    assert beat is not None
    assert beat.age_seconds(NOW) is None


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(0, False), (300, False), (301, True)],
)
def test_is_stale_boundary(elapsed: int, expected: bool) -> None:
    beat = Heartbeat.model_validate({"updated_at": NOW.isoformat()})
    assert beat.is_stale(NOW + timedelta(seconds=elapsed), 300) is expected


def test_is_stale_without_updated_at() -> None:
    """`updated_at` が無ければ古い扱い。**不明は安全側へ倒す**（§7.5）。"""
    assert Heartbeat.model_validate({}).is_stale(NOW, 300) is True
