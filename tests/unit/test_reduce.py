"""Map-Reduce の Reduce 段と重複除去を固定する（SPEC §12.4）。

**Reduce の入力は `PartialAnalysis` の JSON 配列であり、原文 transcript を再送しない。**
再送するとトークンが倍になり、**1 日分で `max_output_tokens` を超えて途中で切れる。**

**多段 Reduce の中間段は `PartialAnalysis` を出す**（v5.4→v5.5 の変更 Q-1）。
`title` は「その日を表す簡潔な日本語」であり（§12.5）、**束の一部に対して付けた題は
最終段では邪魔になる。**

テストは**ネットワークを一切使わない**（§20.2）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from voicedock import llm
from voicedock.config import Config
from voicedock.errors import ErrorCode
from voicedock.llm import (
    REDUCE_MAX_DEPTH,
    Endpoint,
    analyze_session,
    build_schema,
    dedupe,
    normalize_for_dedupe,
    reduce_phase,
)
from voicedock.session import AbsoluteSegment, SessionTranscript

JST = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 9, 12)
START = datetime(2026, 9, 12, 8, 0, 0, tzinfo=JST)
BASE_URL = "http://model-runner.docker.internal/engines/v1"
ENV = {"VOICEDOCK_LLM_URL": BASE_URL, "VOICEDOCK_LLM_MODEL": "ai/test-model"}

PARTIAL = {
    "summary": "午前の作業をまとめた。",
    "key_points": ["削除条件を整理した"],
    "tasks": [{"text": "ND テストを書く", "due": None}],
    "decisions": [],
    "ideas": [],
}
FINAL = {
    "title": "開発の一日",
    "summary": "一日の要約。",
    "key_points": ["削除条件を整理した"],
    "tasks": [{"text": "ND テストを書く", "due": None}],
    "decisions": [],
    "ideas": [],
    "tags": ["VoiceDock"],
}


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config()


class Recorder:
    """`MockTransport` のハンドラ。system プロンプトと本文を全部とっておく。"""

    def __init__(self, *bodies: str) -> None:
        self.requests: list[httpx.Request] = []
        self._bodies = list(bodies)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._bodies:
            body = self._bodies.pop(0)
        else:
            # **system プロンプトで段を見分ける。**`title` を求めているのが最終段
            # （`reduce_ja.txt` + `AnalysisResult`）で、中間段は `map_ja.txt` +
            # `PartialAnalysis` なので `title` を返すと `extra="forbid"` で弾かれる
            payload = json.loads(request.content)
            final = '"title"' in payload["messages"][0]["content"]
            body = json.dumps(FINAL if final else PARTIAL, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})

    @property
    def payloads(self) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.requests]

    def systems(self) -> list[str]:
        return [p["messages"][0]["content"] for p in self.payloads]  # type: ignore[index]

    def users(self) -> list[str]:
        return [p["messages"][1]["content"] for p in self.payloads]  # type: ignore[index]


def client_for(recorder: Recorder) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(recorder))


def transcript(*, chars: int, hours: float, per_segment: int = 40) -> SessionTranscript:
    count = max(1, chars // per_segment)
    span = hours * 3600 / count
    segments = []
    for index in range(count):
        at = START + timedelta(seconds=index * span)
        segments.append(
            AbsoluteSegment(
                at=at, end_at=at + timedelta(seconds=min(span, 5.0)), text="あ" * per_segment
            )
        )
    return SessionTranscript(day_date=DAY, segments=segments, blocks=[], excluded_partkeys=[])


def partials(cfg: Config, count: int, *, size: int = 20):  # type: ignore[no-untyped-def]
    """`PartialAnalysis` を `count` 件。

    **大きさは `key_points` に載せる。**`summary` は 4,000 文字の上限を持つので
    （§12.2）、そこで膨らませるとスキーマ検証で落ちる。
    """
    model = build_schema(cfg, partial=True)
    return [
        model.model_validate(PARTIAL | {"summary": f"束 {index}", "key_points": ["あ" * size]})
        for index in range(count)
    ]


# --- 単一パス（§12.4 / E2E-01） -----------------------------------------


def test_a_single_chunk_uses_the_analyze_prompt(cfg: Config) -> None:
    """**チャンクが 1 個なら Map-Reduce を使わない**（§12.4）。

    E2E-01（1 分録音）は必ずこの経路を通る。
    """
    recorder = Recorder(json.dumps(FINAL, ensure_ascii=False))
    with client_for(recorder) as client:
        result = analyze_session(transcript(chars=200, hours=0.02), cfg=cfg, client=client, env=ENV)
    assert result.ok
    assert result.single_pass, "単一パスでないと Timeline の素材が変わる（§13.4）"
    assert len(recorder.requests) == 1
    assert '"tags"' in recorder.systems()[0], "analyze_ja.txt を使っていない（title/tags が要る）"


def test_a_single_chunk_has_no_partials(cfg: Config) -> None:
    """**Map 中間結果が存在しない。**Timeline は Part / Block の時刻範囲から組む（§13.4）。"""
    with client_for(Recorder()) as client:
        result = analyze_session(transcript(chars=200, hours=0.02), cfg=cfg, client=client, env=ENV)
    assert result.partials == ()
    assert len(result.chunks) == 1


# --- Map 段（§12.4） ----------------------------------------------------


def test_multiple_chunks_run_map_then_reduce(cfg: Config) -> None:
    source = transcript(chars=60_000, hours=2)
    expected_chunks = len(llm.split_chunks(source, cfg))
    assert expected_chunks > 1

    bodies = [json.dumps(PARTIAL, ensure_ascii=False)] * expected_chunks
    recorder = Recorder(*bodies, json.dumps(FINAL, ensure_ascii=False))
    with client_for(recorder) as client:
        result = analyze_session(source, cfg=cfg, client=client, env=ENV)

    assert result.ok
    assert not result.single_pass
    assert len(result.partials) == expected_chunks
    assert len(recorder.requests) == expected_chunks + 1, "Map × N + Reduce × 1"


def test_the_map_prompt_has_no_title_or_tags(cfg: Config) -> None:
    """Map 段は `map_ja.txt`（`title` / `tags` を持たない。§12.5）。"""
    source = transcript(chars=60_000, hours=2)
    count = len(llm.split_chunks(source, cfg))
    recorder = Recorder(*[json.dumps(PARTIAL, ensure_ascii=False)] * count)
    with client_for(recorder) as client:
        analyze_session(source, cfg=cfg, client=client, env=ENV)
    assert '"title"' not in recorder.systems()[0]
    assert '"tags"' not in recorder.systems()[0]


def test_a_map_failure_stops_before_reduce(cfg: Config) -> None:
    """**Map が落ちたら Reduce へ進まない。**欠けた中間結果で要約すると内容が失われる。"""
    source = transcript(chars=60_000, hours=2)
    recorder = Recorder("壊れた出力", "壊れた出力")  # repair も失敗させる
    with client_for(recorder) as client:
        result = analyze_session(source, cfg=cfg, client=client, env=ENV)
    assert result.error_code is ErrorCode.LLM_INVALID_JSON
    assert result.result is None


# --- Reduce 段（§12.4） -------------------------------------------------


def test_reduce_input_is_a_json_array_of_partials(cfg: Config) -> None:
    """**入力は `PartialAnalysis` の JSON 配列である**（§12.4）。"""
    recorder = Recorder(json.dumps(FINAL, ensure_ascii=False))
    with client_for(recorder) as client:
        reduce_phase(partials(cfg, 3), cfg=cfg, client=client, env=ENV)
    body = json.loads(recorder.users()[0])
    assert isinstance(body, list)
    assert len(body) == 3
    assert set(body[0]) == {"summary", "key_points", "tasks", "decisions", "ideas"}


def test_reduce_never_resends_the_transcript(cfg: Config) -> None:
    """**原文 transcript を再送しない**（§12.4）。

    再送するとトークンが倍になり、**1 日分で `max_output_tokens` を超えて途中で切れる。**
    """
    marker = "これは原文であり Reduce に現れてはならない"
    source = transcript(chars=60_000, hours=2)
    source.segments[0] = AbsoluteSegment(
        at=source.segments[0].at, end_at=source.segments[0].end_at, text=marker
    )
    count = len(llm.split_chunks(source, cfg))
    recorder = Recorder(*[json.dumps(PARTIAL, ensure_ascii=False)] * count)
    with client_for(recorder) as client:
        analyze_session(source, cfg=cfg, client=client, env=ENV)
    assert marker not in recorder.users()[-1], "Reduce に原文が載っている"


def test_a_single_partial_skips_bundling(cfg: Config) -> None:
    """**1 束しか無い場合はそのまま最終 Reduce へ**（§12.4。無限ループの防止）。"""
    recorder = Recorder(json.dumps(FINAL, ensure_ascii=False))
    with client_for(recorder) as client:
        result = reduce_phase(partials(cfg, 1, size=30_000), cfg=cfg, client=client, env=ENV)
    assert result.result is not None
    assert len(recorder.requests) == 1


# --- 多段 Reduce（v5.4→v5.5 の変更 Q-1） --------------------------------


def test_an_oversized_reduce_input_folds_first(cfg: Config) -> None:
    """入力が `max_chars_per_request` を超えたら**先に束ねる**（§12.4）。"""
    many = partials(cfg, 40, size=2_000)  # JSON 化で 20,000 文字を超える
    recorder = Recorder()
    with client_for(recorder) as client:
        result = reduce_phase(many, cfg=cfg, client=client, env=ENV)
    assert result.result is not None
    assert result.reduce_depth >= 2, "多段になっていない"
    assert len(recorder.requests) > 1


def test_the_intermediate_stage_uses_the_partial_schema(cfg: Config) -> None:
    """**中間段の出力は `PartialAnalysis`**（Q-1）。

    `title` は「その日を表す簡潔な日本語」であり（§12.5）、**束の一部に対して付けた題は
    最終段では邪魔になる。**`tags` も中間段で削られると復元できない。
    """
    many = partials(cfg, 40, size=2_000)
    recorder = Recorder()
    with client_for(recorder) as client:
        reduce_phase(many, cfg=cfg, client=client, env=ENV)
    systems = recorder.systems()
    assert '"title"' not in systems[0], "中間段で AnalysisResult を使っている"
    assert '"title"' in systems[-1], "最終段で title を付けていない"


def test_bundles_keep_their_order(cfg: Config) -> None:
    """**時刻順のまま貪欲に詰める。並べ替えない**（§12.4）。

    Timeline の素材でもあるので順序が意味を持つ。
    """
    many = partials(cfg, 40, size=2_000)
    recorder = Recorder()
    with client_for(recorder) as client:
        reduce_phase(many, cfg=cfg, client=client, env=ENV)
    first_bundle = json.loads(recorder.users()[0])
    summaries = [item["summary"] for item in first_bundle]
    assert summaries == sorted(summaries, key=lambda s: int(s.split()[1]))


def test_every_bundle_fits_the_limit(cfg: Config) -> None:
    many = partials(cfg, 40, size=2_000)
    recorder = Recorder()
    with client_for(recorder) as client:
        reduce_phase(many, cfg=cfg, client=client, env=ENV)
    for body in recorder.users()[:-1]:
        assert len(body) <= cfg.llm.max_chars_per_request, len(body)


def test_the_depth_limit_stops_recursion(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """**上限に到達したら例外にせず打ち切る**（§1.3）。Raw ノートは既に保存されている。"""
    recorder = Recorder()

    def never_shrinks(*_args: object, **_kwargs: object) -> list[list[object]]:
        # 束ねても小さくならない状況（畳めないほど大きい中間結果）
        return [[object()], [object()]]

    with client_for(recorder) as client:
        result = reduce_phase(
            partials(cfg, 40, size=2_000),
            cfg=cfg,
            client=client,
            env=ENV,
            depth=REDUCE_MAX_DEPTH,
        )
    assert result.result is None
    assert result.error_code is ErrorCode.LLM_INVALID_JSON
    assert result.error_message is not None
    assert str(REDUCE_MAX_DEPTH) in result.error_message


def test_the_depth_limit_matches_the_spec() -> None:
    """§12.4 の表が定める段数と `REDUCE_MAX_DEPTH` が一致すること。

    **SPEC の値だけを変えても落ちるようにする。**定数を直書きで突き合わせていた頃は、
    §12.4 を 5 段に書き換えても実装が 3 段のまま通った（意図的に壊して発覚）。
    """
    from tests.spec_sync import spec_text

    text = spec_text()
    start = text.index("### 12.4")
    block = text[start : text.index("### 12.5", start)]
    matched = re.search(r"\| 段数の上限 \| \*\*(\d+) 段\*\*（`REDUCE_MAX_DEPTH`）", block)
    assert matched is not None, "§12.4 の「段数の上限」の行を読み取れません"
    assert int(matched.group(1)) == REDUCE_MAX_DEPTH


# --- 重複除去（§12.4） --------------------------------------------------


def test_dedupe_keeps_the_first_occurrence() -> None:
    assert dedupe(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (" VoiceDock ", "VoiceDock"),
        ("ＶｏｉｃｅＤｏｃｋ", "VoiceDock"),
        ("VOICEDOCK", "voicedock"),
        ("ﾃｽﾄ", "テスト"),
    ],
)
def test_dedupe_normalizes_before_comparing(left: str, right: str) -> None:
    """**前後空白除去・全角半角統一（NFKC）・小文字化の完全一致**（§12.4）。"""
    assert normalize_for_dedupe(left) == normalize_for_dedupe(right)
    assert dedupe([left, right]) == [left]


def test_dedupe_does_not_match_loosely() -> None:
    """**曖昧一致はしない**（§12.4。取りこぼしより重複のほうが安全）。

    緩くすると「削除条件を整理した」と「削除条件を整理する」が 1 件に潰れ、
    **決定事項とタスクの区別が消える。**
    """
    assert dedupe(["削除条件を整理した", "削除条件を整理する"]) == [
        "削除条件を整理した",
        "削除条件を整理する",
    ]
    assert dedupe(["VoiceDock", "VoiceDock の設計"]) == ["VoiceDock", "VoiceDock の設計"]


def test_the_reduce_result_is_deduped(cfg: Config) -> None:
    """**LLM に任せない**（§12.4 の「最後に重複除去する」）。

    プロンプトで「重複する項目はまとめてください」と指示しているが（§12.5）、
    守られたかどうかは確かめられる。
    """
    noisy = FINAL | {
        "key_points": ["同じ点", "同じ点", " 同じ点 "],
        "tags": ["VoiceDock", "ＶｏｉｃｅＤｏｃｋ"],
        "tasks": [
            {"text": "ND テストを書く", "due": None},
            {"text": "ND テストを書く", "due": "2026-09-20"},
        ],
    }
    recorder = Recorder(json.dumps(noisy, ensure_ascii=False))
    with client_for(recorder) as client:
        result = reduce_phase(partials(cfg, 2), cfg=cfg, client=client, env=ENV)
    assert result.result is not None
    assert result.result.key_points == ["同じ点"]  # type: ignore[attr-defined]
    assert result.result.tags == ["VoiceDock"]  # type: ignore[attr-defined]
    assert len(result.result.tasks) == 1  # type: ignore[attr-defined]


def test_tasks_are_deduped_by_text(cfg: Config) -> None:
    """`tasks` は `text` で比べる（`due` が違っても同じやることは 1 件）。

    **最初のものを残す。**Map は時刻順なので、**最初に現れた `due` が最も早い言及**である。
    """
    noisy = FINAL | {
        "tasks": [
            {"text": "買う", "due": "2026-09-20"},
            {"text": "買う", "due": None},
        ]
    }
    recorder = Recorder(json.dumps(noisy, ensure_ascii=False))
    with client_for(recorder) as client:
        result = reduce_phase(partials(cfg, 2), cfg=cfg, client=client, env=ENV)
    assert result.result is not None
    assert [task.due for task in result.result.tasks] == ["2026-09-20"]  # type: ignore[attr-defined]


# --- 失敗（§15.1） ------------------------------------------------------


def test_an_empty_transcript_is_a_merge_failure(cfg: Config) -> None:
    """チャンクが 0 個（統合結果が空）。**`session_empty` の判定は呼び手が行う**（#27）。"""
    empty = SessionTranscript(day_date=DAY, segments=[], blocks=[], excluded_partkeys=[])
    result = analyze_session(empty, cfg=cfg, env=ENV)
    assert result.error_code is ErrorCode.SESSION_MERGE_FAILED
    assert result.chunks == ()


def test_an_unreachable_endpoint_is_unavailable(cfg: Config) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        result = analyze_session(transcript(chars=200, hours=0.02), cfg=cfg, client=client, env=ENV)
    assert result.error_code is ErrorCode.LLM_UNAVAILABLE


def test_the_elapsed_time_accumulates(cfg: Config) -> None:
    """`llm_completed` の `elapsed_s` に使う（§16.2 / §21.2 Phase 3）。"""
    source = transcript(chars=60_000, hours=2)
    count = len(llm.split_chunks(source, cfg))
    recorder = Recorder(*[json.dumps(PARTIAL, ensure_ascii=False)] * count)
    with client_for(recorder) as client:
        result = analyze_session(source, cfg=cfg, client=client, env=ENV)
    assert result.elapsed_seconds >= 0.0
    assert result.ok


def test_the_endpoint_is_resolved_once(cfg: Config) -> None:
    """**env を毎回読まない。**呼び手が渡した `endpoint` を全段で使い回す。"""
    source = transcript(chars=60_000, hours=2)
    count = len(llm.split_chunks(source, cfg))
    recorder = Recorder(*[json.dumps(PARTIAL, ensure_ascii=False)] * count)
    with client_for(recorder) as client:
        result = analyze_session(
            source, cfg=cfg, client=client, endpoint=Endpoint(BASE_URL, "ai/pinned")
        )
    assert result.ok
    assert all(payload["model"] == "ai/pinned" for payload in recorder.payloads)
