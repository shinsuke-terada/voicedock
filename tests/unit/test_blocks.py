"""`voicedock.session.compute_blocks` が SPEC §10.4 の Block 規則に従うことを固定する。

**Block は Timeline の見出し単位である**（§2 / §13.4）。DB には持たず、レンダリング時に
算出する。終日録音では 30 分分割が途切れず続くため、**ギャップで区切らないと
「1 日ぶんの見出しが 1 つ」になり振り返りに使えない**（v3.0 の既知の失敗）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from voicedock.session import compute_blocks

JST = ZoneInfo("Asia/Tokyo")
GAP = 600
"""`session.block_gap_seconds` の既定値（§7.2）。"""


@dataclass(frozen=True)
class FakePart:
    """`db.Recording` の Block 算出に要る 2 つの値だけ（`BlockPart` を満たす）。"""

    started_at: str
    ended_at: str | None


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, 12, hour, minute, second, tzinfo=JST)


def recorded(hour: int, minute: int = 0, *, minutes: float = 30) -> FakePart:
    start = at(hour, minute)
    return FakePart(
        started_at=start.isoformat(timespec="seconds"),
        ended_at=(start + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
    )


def unknown_end(hour: int, minute: int = 0) -> FakePart:
    """`duration_seconds` が `NULL` の Part（ffprobe 失敗。§10.5）。"""
    return FakePart(started_at=at(hour, minute).isoformat(timespec="seconds"), ended_at=None)


def spans(blocks: list[tuple[datetime, datetime]]) -> list[tuple[str, str]]:
    return [(a.strftime("%H:%M:%S"), b.strftime("%H:%M:%S")) for a, b in blocks]


# --- 基本 ---------------------------------------------------------------


def test_no_parts_is_no_blocks() -> None:
    assert compute_blocks([], gap_seconds=GAP) == []


def test_one_part_is_one_block() -> None:
    assert spans(compute_blocks([recorded(9)], gap_seconds=GAP)) == [("09:00:00", "09:30:00")]


def test_back_to_back_parts_are_one_block() -> None:
    """**終日録音の 30 分分割は 1 つの Block になる。**ギャップが 0 秒である。"""
    parts = [recorded(9), recorded(9, 30), recorded(10)]
    assert spans(compute_blocks(parts, gap_seconds=GAP)) == [("09:00:00", "10:30:00")]


def test_a_gap_over_the_threshold_splits() -> None:
    parts = [recorded(9), recorded(9, 41)]  # 11 分の無録音 > 600 秒
    assert spans(compute_blocks(parts, gap_seconds=GAP)) == [
        ("09:00:00", "09:30:00"),
        ("09:41:00", "10:11:00"),
    ]


def test_a_gap_exactly_at_the_threshold_does_not_split() -> None:
    """**境界は「超えたら」である**（§10.4）。ちょうど `gap_seconds` は同じ Block。"""
    parts = [recorded(9), recorded(9, 40)]  # 10 分 = 600 秒
    assert len(compute_blocks(parts, gap_seconds=GAP)) == 1


def test_a_gap_one_second_over_the_threshold_splits() -> None:
    parts = [
        recorded(9),
        FakePart(
            started_at=(at(9, 40) + timedelta(seconds=1)).isoformat(timespec="seconds"),
            ended_at=(at(10, 10) + timedelta(seconds=1)).isoformat(timespec="seconds"),
        ),
    ]
    assert len(compute_blocks(parts, gap_seconds=GAP)) == 2


def test_order_does_not_matter() -> None:
    """**呼び手の順序に依存しない。**`started_at` で並べ替える。"""
    ordered = compute_blocks([recorded(9), recorded(9, 41)], gap_seconds=GAP)
    shuffled = compute_blocks([recorded(9, 41), recorded(9)], gap_seconds=GAP)
    assert ordered == shuffled


def test_three_blocks() -> None:
    parts = [recorded(9), recorded(9, 30), recorded(11), recorded(15)]
    assert spans(compute_blocks(parts, gap_seconds=GAP)) == [
        ("09:00:00", "10:00:00"),
        ("11:00:00", "11:30:00"),
        ("15:00:00", "15:30:00"),
    ]


# --- `ended_at` が NULL（§10.4 / §10.5） --------------------------------


def test_an_unknown_end_always_splits() -> None:
    """**`ended_at` が NULL の Part はそこで必ず区切る**（§10.4「確信が持てない場合は分ける」）。

    終端が分からないまま繋ぐと、**無録音区間を録音中だったことにしてしまう。**
    `duration_seconds` は ffprobe 失敗時に `NULL` のまま続行するので実際に存在する（§10.5）。
    """
    parts = [unknown_end(9), recorded(9, 1)]  # ギャップは 1 分しかない
    assert spans(compute_blocks(parts, gap_seconds=GAP)) == [
        ("09:00:00", "09:00:00"),
        ("09:01:00", "09:31:00"),
    ]


def test_an_unknown_end_uses_its_start_as_the_block_end() -> None:
    """終端が不明なら `started_at` を終端として扱う（**推測で延ばさない**）。"""
    assert spans(compute_blocks([unknown_end(9)], gap_seconds=GAP)) == [("09:00:00", "09:00:00")]


def test_an_unknown_end_in_the_middle_splits_once() -> None:
    parts = [recorded(9), unknown_end(9, 30), recorded(9, 31)]
    assert spans(compute_blocks(parts, gap_seconds=GAP)) == [
        ("09:00:00", "09:30:00"),
        ("09:31:00", "10:01:00"),
    ]


def test_only_unknown_ends_never_raises() -> None:
    """**例外にしない**（§1.3: 記録を残すことを優先する）。"""
    blocks = compute_blocks([unknown_end(9), unknown_end(10), unknown_end(11)], gap_seconds=GAP)
    assert len(blocks) == 3


# --- 重なり -------------------------------------------------------------


def test_overlapping_parts_are_one_block() -> None:
    """**2 台の送信機が同時に録る**（§5.1 は TX01 / TX02 を想定）。重なりは 1 Block。"""
    parts = [recorded(9, minutes=30), recorded(9, 10, minutes=30)]
    assert spans(compute_blocks(parts, gap_seconds=GAP)) == [("09:00:00", "09:40:00")]


def test_a_contained_part_does_not_shorten_the_block() -> None:
    """短い Part が長い Part の内側にあっても終端を縮めない（`max` を取る）。"""
    parts = [recorded(9, minutes=60), recorded(9, 10, minutes=5)]
    assert spans(compute_blocks(parts, gap_seconds=GAP)) == [("09:00:00", "10:00:00")]


# --- 設定 ---------------------------------------------------------------


@pytest.mark.parametrize(("gap", "expected"), [(0, 2), (60, 1), (3600, 1)])
def test_the_threshold_comes_from_the_configuration(gap: int, expected: int) -> None:
    """`gap_seconds: 0` なら**隙間なく続く Part 以外すべてが別 Block**になる。"""
    parts = [recorded(9), recorded(9, 31)]  # 1 分の隙間
    assert len(compute_blocks(parts, gap_seconds=gap)) == expected


def test_zero_gap_keeps_back_to_back_parts_together() -> None:
    """`gap_seconds: 0` でも隙間 0 秒の Part は繋がる（`> 0` は偽）。"""
    assert len(compute_blocks([recorded(9), recorded(9, 30)], gap_seconds=0)) == 1
