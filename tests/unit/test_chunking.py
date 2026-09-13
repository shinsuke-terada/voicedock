"""`voicedock.llm.split_chunks` が SPEC §12.4 の分割規則に従うことを固定する。

**1 日 16 時間の録音は約 350,000 文字になるため、通常は必ず Map-Reduce が走る。**

分割規則には上限が 2 つある。文字数（`max_chars_per_request`）**かつ**実時間
（`max_seconds_per_request`）。後者は **Timeline の粒度を時間的に揃えるために必要**で、
文字数だけで割ると時間帯が不均等な Timeline になる。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from voicedock.config import Config
from voicedock.llm import Chunk, split_chunks
from voicedock.session import AbsoluteSegment, SessionTranscript

JST = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 9, 12)
START = datetime(2026, 9, 12, 8, 0, 0, tzinfo=JST)


def transcript(*segments: AbsoluteSegment) -> SessionTranscript:
    return SessionTranscript(day_date=DAY, segments=list(segments), blocks=[], excluded_partkeys=[])


def segment(offset_seconds: float, text: str, *, length: float = 5.0) -> AbsoluteSegment:
    at = START + timedelta(seconds=offset_seconds)
    return AbsoluteSegment(at=at, end_at=at + timedelta(seconds=length), text=text)


def synth(*, chars: int, hours: float, per_segment: int = 40) -> SessionTranscript:
    """実測に近い合成 transcript（§5.1: 1 日 16 時間で約 35 万文字）。"""
    count = max(1, chars // per_segment)
    span = hours * 3600 / count
    return transcript(
        *[
            segment(index * span, "あ" * per_segment, length=min(span, 5.0))
            for index in range(count)
        ]
    )


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config()


# --- segment 境界でのみ分割する（§12.4） --------------------------------


def test_splitting_happens_only_at_segment_boundaries(cfg: Config) -> None:
    """**文の途中で切らない**（§12.4）。

    切ると、**チャンクの先頭と末尾が意味を成さない断片になり、LLM がそこから
    事実を捏造する余地が生まれる。**
    """
    texts = [f"文{index}です。" * 200 for index in range(20)]
    source = transcript(*[segment(index * 10, text) for index, text in enumerate(texts)])
    chunks = split_chunks(source, cfg)
    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.text.splitlines():
            assert line in texts, "segment の途中で切れている"


def test_every_segment_appears_somewhere(cfg: Config) -> None:
    """**取りこぼさない。**重なりで 2 度現れるのは許すが、消えてはならない。"""
    source = synth(chars=60_000, hours=2)
    chunks = split_chunks(source, cfg)
    covered = {segment.at for chunk in chunks for segment in chunk.segments}
    assert covered == {segment.at for segment in source.segments}


# --- 2 つの上限（§12.4） -----------------------------------------------


def test_chunks_respect_the_character_limit(cfg: Config) -> None:
    chunks = split_chunks(synth(chars=350_000, hours=16), cfg)
    limit = cfg.llm.max_chars_per_request
    for chunk in chunks:
        assert len(chunk.text) <= limit + cfg.llm.chunk_overlap_chars, len(chunk.text)


def test_chunks_respect_the_time_limit(cfg: Config) -> None:
    """**実時間の上限が要る**（§12.4）。

    文字数だけで割ると、**話した量の少ない時間帯が 1 チャンクに何時間も入り**、
    Timeline の粒度が時間的に不均等になる。
    """
    # 30 秒ごとに短い発話（文字数では割れないが、実時間では割れる）
    sparse = transcript(*[segment(index * 30, "はい。") for index in range(1000)])
    chunks = split_chunks(sparse, cfg)
    assert len(chunks) > 1, "実時間で割れていない"
    for chunk in chunks:
        assert chunk.seconds <= cfg.llm.max_seconds_per_request + 60, chunk.seconds


def test_a_long_quiet_stretch_is_split_by_time(make_config: Callable[..., Config]) -> None:
    """発話が少なくても時間で割れること。"""
    cfg = make_config({"llm": {"max_seconds_per_request": 600}})
    sparse = transcript(*[segment(index * 100, "ええ。") for index in range(40)])
    chunks = split_chunks(sparse, cfg)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.seconds <= 600 + 100


# --- 重なり（§12.4） ----------------------------------------------------


def test_the_overlap_is_carried_to_the_next_chunk(cfg: Config) -> None:
    """直近 `chunk_overlap_chars` を次チャンクの先頭に重ねる（§12.4）。"""
    source = synth(chars=60_000, hours=2)
    chunks = split_chunks(source, cfg)
    assert len(chunks) > 1
    for previous, following in pairwise(chunks):
        shared = set(previous.segments) & set(following.segments)
        assert shared, "重なりが無い（文脈が切れる）"


def test_the_overlap_is_made_of_whole_segments(cfg: Config) -> None:
    """**重なりも segment 単位である。**文字数で切ると文の途中から始まる。"""
    source = synth(chars=60_000, hours=2)
    chunks = split_chunks(source, cfg)
    known = {segment.text for segment in source.segments}
    for chunk in chunks:
        for line in chunk.text.splitlines():
            assert line in known


def test_zero_overlap_is_allowed(make_config: Callable[..., Config]) -> None:
    """`chunk_overlap_chars: 0` で重なりが無くなること（V-10 は 0 を許す）。"""
    cfg = make_config({"llm": {"chunk_overlap_chars": 0}})
    chunks = split_chunks(synth(chars=60_000, hours=2), cfg)
    for previous, following in pairwise(chunks):
        assert not set(previous.segments) & set(following.segments)


def test_no_chunk_is_only_overlap(cfg: Config) -> None:
    """**重なりだけの末尾チャンクを作らない。**

    作ると同じ内容を 2 回 LLM へ投げ、**Timeline にも同じ時間帯が 2 度現れる。**
    """
    source = synth(chars=41_000, hours=2)
    chunks = split_chunks(source, cfg)
    for previous, following in pairwise(chunks):
        assert not set(following.segments) <= set(previous.segments)


# --- 時刻範囲（Timeline の素材。§13.4） ---------------------------------


def test_each_chunk_keeps_its_time_range(cfg: Config) -> None:
    """**Timeline はこの情報から生成する。LLM には時刻を出力させない**（§12.4 / §13.4）。"""
    source = synth(chars=60_000, hours=2)
    for chunk in split_chunks(source, cfg):
        assert chunk.start_at == chunk.segments[0].at
        assert chunk.end_at == max(segment.end_at for segment in chunk.segments)
        assert chunk.end_at >= chunk.start_at


def test_chunks_are_in_time_order(cfg: Config) -> None:
    chunks = split_chunks(synth(chars=60_000, hours=2), cfg)
    starts = [chunk.start_at for chunk in chunks]
    assert starts == sorted(starts)


def test_the_chunk_text_carries_no_timestamps(cfg: Config) -> None:
    """**LLM に時刻を渡さない。**渡すと出力に時刻が混ざり、幻覚の余地ができる。"""
    source = transcript(segment(0, "おはようございます。"), segment(10, "会議です。"))
    chunk = split_chunks(source, cfg)[0]
    assert "08:00" not in chunk.text
    assert "2026" not in chunk.text


# --- 単一チャンク（E2E-01 の経路。§12.4） -------------------------------


def test_a_short_session_is_a_single_chunk(cfg: Config) -> None:
    """**チャンクが 1 個なら Map-Reduce を使わない**（§12.4）。

    E2E-01（1 分録音）は必ずこの経路を通る。
    """
    source = transcript(segment(0, "おはようございます。"), segment(10, "以上です。"))
    chunks = split_chunks(source, cfg)
    assert len(chunks) == 1
    assert chunks[0].text == "おはようございます。\n以上です。"


def test_an_empty_transcript_has_no_chunks(cfg: Config) -> None:
    assert split_chunks(transcript(), cfg) == []


def test_a_single_oversized_segment_becomes_one_chunk(cfg: Config) -> None:
    """**1 つの segment が上限を超えても割らない**（segment 境界でのみ分割する）。

    whisper の 1 区間が 20,000 文字になることは実際には無いが、**割ると
    「segment 境界でのみ」の規則が破れる。**
    """
    huge = transcript(segment(0, "あ" * 30_000))
    chunks = split_chunks(huge, cfg)
    assert len(chunks) == 1
    assert len(chunks[0].text) == 30_000


# --- 実測に近い規模（§21.2 Phase 3） ------------------------------------


def test_a_full_day_splits_into_roughly_eighteen_chunks(cfg: Config) -> None:
    """1 日分（約 35 万文字 / 16 時間）で `chunks` が 18 前後になること（§12.4）。

    **20,000 文字 × 18 = 360,000** なので、文字数の上限が支配的である。
    """
    chunks = split_chunks(synth(chars=350_000, hours=16), cfg)
    assert 15 <= len(chunks) <= 25, len(chunks)


def test_the_chunk_dataclass_exposes_seconds() -> None:
    chunk = Chunk(
        text="x",
        start_at=START,
        end_at=START + timedelta(seconds=90),
        segments=(segment(0, "x"),),
    )
    assert chunk.seconds == 90.0
