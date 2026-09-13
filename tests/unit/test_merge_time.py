"""`voicedock.session.build_session_transcript` の時刻モデルを固定する（SPEC §10.8）。

**ここが v3.0 の既知バグの再発防止点である。**時刻を「相対オフセットの累積加算」で
持つと、(1) Part 間の無録音区間が詰まり、(2) `duration` が NULL の Part で例外になり、
(3) 32 Part の末尾で誤差が累積する。**絶対時刻にすれば 3 つとも起こらない。**

§10.8: `at = part.started_at + timedelta(seconds=seg.start)`
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from tests.spec_sync import spec_section_code
from voicedock import paths
from voicedock.db import Recording
from voicedock.paths import DevicePath
from voicedock.session import AbsoluteSegment, build_session_transcript
from voicedock.states import PartStatus

JST = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 9, 12)
GAP = 600


@dataclass(frozen=True)
class Seg:
    """`transcribe.TranscriptSegment`（§11.2）を構造的に満たす。**相対秒**。"""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Tx:
    """`transcribe.Transcript`（§11.2）を構造的に満たす。"""

    segments: tuple[Seg, ...]


def part(
    hour: int,
    minute: int = 0,
    *,
    duration: float | None = 1800.0,
    status: str = PartStatus.TRANSCRIBED,
) -> Recording:
    started = datetime(2026, 9, 12, hour, minute, tzinfo=JST)
    stem = f"TX00_MIC001_20260912_{hour:02d}{minute:02d}00"
    relpath = f"TX_MIC001_20260912_{hour:02d}{minute:02d}00/{stem}_orig.wav"
    return Recording(
        partkey=paths.partkey_for("DJIMIC3", DevicePath(PurePosixPath(relpath))),
        device_id="DJIMIC3",
        source_folder=relpath.split("/")[0],
        transmitter_id="TX00",
        mic_index=1,
        started_at=started.isoformat(timespec="seconds"),
        status=status,
        updated_at=started.isoformat(timespec="seconds"),
        duration_seconds=duration,
        ended_at=(
            (started + timedelta(seconds=duration)).isoformat(timespec="seconds")
            if duration is not None
            else None
        ),
        source_path=relpath,
    )


def loader(mapping: dict[str, Tx]):  # type: ignore[no-untyped-def]
    def load(recording: Recording) -> Tx | None:
        return mapping.get(recording.partkey)

    return load


def times(segments: list[AbsoluteSegment]) -> list[str]:
    return [s.at.strftime("%H:%M:%S") for s in segments]


# --- SPEC 整合 -----------------------------------------------------------


def test_the_spec_uses_absolute_time() -> None:
    """§10.8 のコードブロックが `part.started_at + timedelta(...)` であること。

    **`offset += duration` に書き換えられたらここが落ちる。**
    """
    block = spec_section_code("10.8", "python")
    assert "part.started_at + timedelta(seconds=seg.start)" in block
    assert "part.started_at + timedelta(seconds=seg.end)" in block
    assert "offset" not in block


# --- 絶対時刻（§10.8 の中核） -------------------------------------------


def test_a_gap_between_parts_is_preserved() -> None:
    """**Part 間の無録音区間が保たれる。**`offset += duration` 方式だと詰まる。

    09:00 開始の 30 分 Part と、**2 時間後**の 11:00 開始の Part。
    累積オフセット方式では 2 本目が 09:30 から始まってしまう。
    """
    first, second = part(9), part(11)
    mapping = {
        first.partkey: Tx((Seg(0.0, 5.0, "おはようございます"),)),
        second.partkey: Tx((Seg(0.0, 5.0, "午前の会議です"),)),
    }
    result = build_session_transcript(
        [first, second], day_date=DAY, load=loader(mapping), gap_seconds=GAP
    )
    assert result is not None
    assert times(result.segments) == ["09:00:00", "11:00:00"]

    # 累積オフセット方式なら 2 本目が 09:30:00 になる（対比）
    naive = datetime.fromisoformat(first.started_at) + timedelta(
        seconds=first.duration_seconds or 0.0
    )
    assert naive.strftime("%H:%M:%S") == "09:30:00"
    assert times(result.segments)[1] != "09:30:00", "ギャップが詰まっている"


def test_the_offset_inside_a_part_is_added_to_its_start() -> None:
    """`at = part.started_at + seg.start`（§10.8）。"""
    only = part(9)
    mapping = {only.partkey: Tx((Seg(0.0, 2.0, "A"), Seg(120.5, 125.0, "B")))}
    result = build_session_transcript([only], day_date=DAY, load=loader(mapping), gap_seconds=GAP)
    assert result is not None
    assert times(result.segments) == ["09:00:00", "09:02:00"]
    assert result.segments[1].at.second == 0
    assert result.segments[1].end_at.strftime("%H:%M:%S") == "09:02:05"


def test_no_error_accumulates_over_32_parts() -> None:
    """**誤差が累積しない。**32 Part（終日録音 1 日分。§5.1）の末尾がずれないこと。

    累積加算では浮動小数の duration を 32 回足すので、末尾が実時刻からずれる。
    """
    # **実測に合わせて「録音長 < 分割間隔」にする。**DJI の分割は 30 分ごとだが
    # ファイル長はちょうど 1800 秒ではない（`docs/POC.md` §6.2）。累積加算では
    # この差が 32 回積もって末尾が約 3 秒ずれ、さらに浮動小数の誤差が乗る
    parts = [part(hour, minute, duration=1799.9) for hour in range(8, 24) for minute in (0, 30)]
    assert len(parts) == 32, "終日録音 1 日分（§5.1）"
    mapping = {p.partkey: Tx((Seg(0.1, 1.0, "x"),)) for p in parts}
    result = build_session_transcript(parts, day_date=DAY, load=loader(mapping), gap_seconds=GAP)
    assert result is not None
    assert len(result.segments) == len(parts)
    last = result.segments[-1]
    expected = datetime.fromisoformat(parts[-1].started_at) + timedelta(seconds=0.1)
    assert last.at == expected, "末尾の時刻が Part の started_at から直接決まっていない"


def test_the_result_is_sorted_by_time_regardless_of_input_order() -> None:
    """**呼び手の順序に依存しない。**2 台の送信機が同時に録ることがある（§5.1）。"""
    first, second = part(9), part(11)
    mapping = {
        first.partkey: Tx((Seg(0.0, 1.0, "A"),)),
        second.partkey: Tx((Seg(0.0, 1.0, "B"),)),
    }
    forward = build_session_transcript(
        [first, second], day_date=DAY, load=loader(mapping), gap_seconds=GAP
    )
    backward = build_session_transcript(
        [second, first], day_date=DAY, load=loader(mapping), gap_seconds=GAP
    )
    assert forward == backward


# --- duration が NULL -----------------------------------------------------


def test_a_null_duration_does_not_raise() -> None:
    """**`duration_seconds` が NULL でも例外にならない**（§10.5 / §1.3）。

    累積オフセット方式は `offset += None` で落ちる。絶対時刻は `started_at` だけを使う。
    """
    only = part(9, duration=None)
    mapping = {only.partkey: Tx((Seg(0.0, 3.0, "ffprobe が失敗した Part"),))}
    result = build_session_transcript([only], day_date=DAY, load=loader(mapping), gap_seconds=GAP)
    assert result is not None
    assert times(result.segments) == ["09:00:00"]
    assert result.blocks == [
        (
            datetime.fromisoformat(only.started_at),
            datetime.fromisoformat(only.started_at),
        )
    ]


def test_a_null_duration_part_mixed_with_others() -> None:
    parts = [part(9), part(9, 30, duration=None), part(10)]
    mapping = {p.partkey: Tx((Seg(0.0, 1.0, "x"),)) for p in parts}
    result = build_session_transcript(parts, day_date=DAY, load=loader(mapping), gap_seconds=GAP)
    assert result is not None
    assert len(result.segments) == 3
    assert len(result.blocks) == 2, "NULL の Part の直後で区切る（§10.4）"


# --- 除外（§10.8） ------------------------------------------------------


def test_failed_and_skipped_parts_are_excluded() -> None:
    """`valid_parts` は `FAILED` / `SKIPPED` を除いた Part（§10.8）。"""
    good = part(9)
    failed = part(10, status=PartStatus.FAILED)
    skipped = part(11, status=PartStatus.SKIPPED)
    mapping = {p.partkey: Tx((Seg(0.0, 1.0, "x"),)) for p in (good, failed, skipped)}
    result = build_session_transcript(
        [good, failed, skipped], day_date=DAY, load=loader(mapping), gap_seconds=GAP
    )
    assert result is not None
    assert len(result.segments) == 1
    assert set(result.excluded_partkeys) == {failed.partkey, skipped.partkey}


def test_the_blocks_ignore_excluded_parts() -> None:
    """除外した Part は Block にも入らない（Timeline に現れてはならない）。"""
    good = part(9)
    failed = part(15, status=PartStatus.FAILED)
    mapping = {good.partkey: Tx((Seg(0.0, 1.0, "x"),))}
    result = build_session_transcript(
        [good, failed], day_date=DAY, load=loader(mapping), gap_seconds=GAP
    )
    assert result is not None
    assert len(result.blocks) == 1


# --- 空（`session_empty` の判定材料。§9.3） -----------------------------


def test_no_parts_returns_none() -> None:
    """**空の `SessionTranscript` を返さない。**呼び手が本文の空なノートを書きうる。"""
    assert build_session_transcript([], day_date=DAY, load=loader({}), gap_seconds=GAP) is None


def test_all_parts_excluded_returns_none() -> None:
    failed = part(9, status=PartStatus.FAILED)
    assert (
        build_session_transcript(
            [failed],
            day_date=DAY,
            load=loader({failed.partkey: Tx((Seg(0, 1, "x"),))}),
            gap_seconds=GAP,
        )
        is None
    )


def test_a_missing_transcript_is_skipped() -> None:
    """transcript が読めない Part は飛ばす。**1 件の失敗で全体を止めない**（§1.3）。"""
    good, missing = part(9), part(10)
    mapping = {good.partkey: Tx((Seg(0.0, 1.0, "x"),))}
    result = build_session_transcript(
        [good, missing], day_date=DAY, load=loader(mapping), gap_seconds=GAP
    )
    assert result is not None
    assert len(result.segments) == 1


def test_only_blank_segments_returns_none() -> None:
    """本文が 1 文字も無ければ `None`（`session_empty`）。"""
    only = part(9)
    mapping = {only.partkey: Tx((Seg(0.0, 1.0, "   "), Seg(1.0, 2.0, "\n")))}
    assert (
        build_session_transcript([only], day_date=DAY, load=loader(mapping), gap_seconds=GAP)
        is None
    )


def test_blank_segments_are_dropped_but_others_kept() -> None:
    only = part(9)
    mapping = {only.partkey: Tx((Seg(0.0, 1.0, ""), Seg(1.0, 2.0, " 本文 ")))}
    result = build_session_transcript([only], day_date=DAY, load=loader(mapping), gap_seconds=GAP)
    assert result is not None
    assert [s.text for s in result.segments] == ["本文"]


# --- 永続化しない（§10.8） ----------------------------------------------


def test_nothing_is_written_anywhere(database, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """**統合結果は永続化しない**（§10.8）。DB にもファイルにも書かない。"""
    only = part(9)
    database.insert_recording(only)
    before = database.counts()
    files_before = sorted(p.name for p in tmp_path.iterdir())

    mapping = {only.partkey: Tx((Seg(0.0, 1.0, "x"),))}
    assert (
        build_session_transcript([only], day_date=DAY, load=loader(mapping), gap_seconds=GAP)
        is not None
    )

    assert database.counts() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before


def test_the_day_date_is_carried_through() -> None:
    only = part(9)
    mapping = {only.partkey: Tx((Seg(0.0, 1.0, "x"),))}
    result = build_session_transcript([only], day_date=DAY, load=loader(mapping), gap_seconds=GAP)
    assert result is not None
    assert result.day_date == DAY
