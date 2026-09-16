"""`voicedock.llm` の HTTP クライアントと repair 経路を固定する（SPEC §12.1 / §12.3）。

**テストはネットワークを一切使わない**（§20.2）。`httpx.MockTransport` で応答を組み立てる。

**この範囲で最も重いのは §14.4 N-7 である。**外部 LLM API へのフォールバックを
書いてしまうと、**音声の内容が外へ出る。**そのような経路が無いことをテストで固定する。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from voicedock import llm
from voicedock.config import Config
from voicedock.errors import ErrorCode
from voicedock.llm import (
    Endpoint,
    PromptKind,
    analyze,
    complete,
    endpoint_of,
    probe,
)

BASE_URL = "http://model-runner.docker.internal/engines/v1"
MODEL = "ai/gemma3:12B-Q4_0"
ENV = {"VOICEDOCK_LLM_URL": BASE_URL, "VOICEDOCK_LLM_MODEL": MODEL}
TRANSCRIPT = "おはようございます。今日は削除条件の整理をします。"

GOOD = {
    "title": "削除条件の整理",
    "summary": "削除条件を論理式へ落とした。",
    "key_points": ["論理式にした"],
    "tasks": [{"text": "ND テストを書く", "due": None}],
    "decisions": [],
    "ideas": [],
    "tags": ["VoiceDock"],
}


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config()


class Recorder:
    """`MockTransport` のハンドラ。**送ったリクエストを全部とっておく。**"""

    def __init__(self, *bodies: str | int) -> None:
        self.requests: list[httpx.Request] = []
        self._bodies = list(bodies)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self._bodies.pop(0) if self._bodies else ""
        if isinstance(body, int):
            return httpx.Response(body, text="boom")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": body}}],
                "usage": {"total_tokens": 42},
            },
        )

    @property
    def payloads(self) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.requests]

    def systems(self) -> list[str]:
        return [p["messages"][0]["content"] for p in self.payloads]  # type: ignore[index]

    def users(self) -> list[str]:
        return [p["messages"][1]["content"] for p in self.payloads]  # type: ignore[index]


def client_for(recorder: Recorder) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(recorder))


def run_analyze(cfg: Config, recorder: Recorder, **kwargs: object) -> llm.AnalyzeResult:
    with client_for(recorder) as client:
        return analyze(TRANSCRIPT, cfg=cfg, client=client, env=ENV, **kwargs)  # type: ignore[arg-type]


# --- §14.4 N-7: 外部フォールバックが無いこと ----------------------------


def test_no_external_llm_host_appears_in_the_source() -> None:
    """**外部 LLM API へのフォールバックを実装してはならない**（§14.4 N-7）。

    書いてしまうと**音声の内容が外へ出る。**`src/` に外部ホストが現れないことを見る。
    ここが落ちたら、まず「なぜそのホストが必要なのか」を疑うこと。
    """
    forbidden = (
        "api.openai.com",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
        "api.cohere",
        "openrouter.ai",
        "api.mistral.ai",
        "api.groq.com",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    )
    for module in sorted(Path(llm.__file__).parent.rglob("*.py")):
        source = module.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in source, f"{module.name} に {needle} がある（§14.4 N-7）"


def test_the_endpoint_comes_only_from_the_environment(cfg: Config) -> None:
    """**URL を埋め込まない。**通信先は `VOICEDOCK_LLM_URL` が指す先だけである（§12.1）。"""
    source = Path(llm.__file__).read_text(encoding="utf-8")
    assert "http://" not in source
    assert "https://" not in source


def test_a_missing_environment_variable_is_unavailable(cfg: Config) -> None:
    """**API キーの代わりに別の経路を探したりしない。**未設定なら `LLM_UNAVAILABLE`。"""
    assert endpoint_of(cfg, {}) is None
    assert endpoint_of(cfg, {"VOICEDOCK_LLM_URL": BASE_URL}) is None
    result = analyze(TRANSCRIPT, cfg=cfg, env={})
    assert result.error_code is ErrorCode.LLM_UNAVAILABLE
    assert result.analysis is None


def test_no_api_key_header_is_sent(cfg: Config) -> None:
    """**API キーは不要**（§12.1）。認証ヘッダを付けない。"""
    recorder = Recorder(json.dumps(GOOD))
    run_analyze(cfg, recorder)
    headers = recorder.requests[0].headers
    assert "authorization" not in headers
    assert "x-api-key" not in headers


# --- §12.1 リクエストの形 -----------------------------------------------


def test_the_request_matches_the_spec(cfg: Config) -> None:
    recorder = Recorder(json.dumps(GOOD))
    run_analyze(cfg, recorder)
    payload = recorder.payloads[0]

    assert payload["model"] == MODEL
    assert payload["temperature"] == cfg.llm.temperature
    assert payload["top_p"] == cfg.llm.top_p
    assert payload["max_tokens"] == cfg.llm.max_output_tokens
    assert payload["response_format"] == {"type": "json_object"}
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == TRANSCRIPT


def test_the_url_is_the_base_plus_chat_completions(cfg: Config) -> None:
    recorder = Recorder(json.dumps(GOOD))
    run_analyze(cfg, recorder)
    assert str(recorder.requests[0].url) == f"{BASE_URL}/chat/completions"


@pytest.mark.parametrize("base", [BASE_URL, f"{BASE_URL}/"])
def test_a_trailing_slash_does_not_drop_the_version(base: str) -> None:
    """**末尾スラッシュを正規化する。**

    `VOICEDOCK_LLM_URL` は外から注入される（§18.2）ので両方が来る。`urljoin` の規則に
    任せると `.../v1` の `v1` が消える。
    """
    assert Endpoint(base_url=base, model=MODEL).chat_url == f"{BASE_URL}/chat/completions"


def test_the_timeout_comes_from_the_configuration(cfg: Config) -> None:
    """1 リクエスト最大 1800 秒（§7.2）。**既定の 5 秒では必ず切れる。**"""
    seen: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout", {}))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        complete(Endpoint(BASE_URL, MODEL), system="s", user="u", cfg=cfg, client=client)
    assert seen[0]["read"] == cfg.llm.request_timeout_seconds


# --- 正常系 --------------------------------------------------------------


def test_a_valid_response_is_parsed(cfg: Config) -> None:
    result = run_analyze(cfg, Recorder(json.dumps(GOOD)))
    assert result.ok
    assert result.analysis is not None
    assert result.analysis.summary == GOOD["summary"]  # type: ignore[attr-defined]
    assert result.repairs == 0


def test_a_fenced_response_is_parsed(cfg: Config) -> None:
    """`response_format` 非対応のバックエンドでも動く（§12.1 / §12.3）。"""
    result = run_analyze(cfg, Recorder(f"```json\n{json.dumps(GOOD)}\n```"))
    assert result.ok


def test_the_map_kind_uses_the_partial_schema(cfg: Config) -> None:
    partial = {k: v for k, v in GOOD.items() if k not in {"title", "tags"}}
    result = run_analyze(cfg, Recorder(json.dumps(partial)), kind=PromptKind.MAP)
    assert result.ok
    assert not hasattr(result.analysis, "title")


def test_the_map_kind_rejects_a_title(cfg: Config) -> None:
    """Map 中間結果に `title` は無い（§12.2）。`extra="forbid"` が弾く。"""
    recorder = Recorder(json.dumps(GOOD), json.dumps(GOOD))
    result = run_analyze(cfg, recorder, kind=PromptKind.MAP)
    assert result.error_code is ErrorCode.LLM_INVALID_JSON


# --- repair（§12.3 の 3〜4） --------------------------------------------


def test_an_invalid_response_is_repaired(cfg: Config) -> None:
    recorder = Recorder("すみません、JSON ではありません", json.dumps(GOOD))
    result = run_analyze(cfg, recorder)
    assert result.ok
    assert result.repairs == 1
    assert len(recorder.requests) == 2


def test_the_repair_request_never_contains_the_transcript(cfg: Config) -> None:
    """**transcript 本文を再送しない**（§12.3）。

    トークン節約と情報漏洩面の縮小の両方が理由である。**repair は「不正だった出力」と
    「検証エラー」だけを渡す。**
    """
    recorder = Recorder("壊れた出力", json.dumps(GOOD))
    run_analyze(cfg, recorder)

    second = recorder.payloads[1]
    assert TRANSCRIPT not in json.dumps(second, ensure_ascii=False)
    assert recorder.users()[1] == ""
    assert "壊れた出力" in recorder.systems()[1]


def test_the_repair_errors_do_not_leak_the_input(cfg: Config) -> None:
    """**検証エラーの文面に本文を載せない。**

    `ValidationError` の既定の文字列には入力値が入る（`input=...`）。transcript の一部が
    そこへ現れると、**repair のリクエストで本文を再送することになる**（§12.3 が禁じている）。
    """
    leaked = "これは本文の一部でありここに現れてはならない"
    broken = json.dumps({"title": "t", "summary": leaked, "mood": leaked})
    recorder = Recorder(broken, json.dumps(GOOD))
    run_analyze(cfg, recorder)
    assert leaked not in recorder.systems()[1].split("前回の出力:")[0]


def test_a_second_failure_is_invalid_json(cfg: Config) -> None:
    recorder = Recorder("壊れた 1", "壊れた 2")
    result = run_analyze(cfg, recorder)
    assert result.error_code is ErrorCode.LLM_INVALID_JSON
    assert result.analysis is None
    assert result.repairs == cfg.llm.repair_attempts
    assert len(recorder.requests) == cfg.llm.repair_attempts + 1


def test_repair_attempts_zero_does_not_retry(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"llm": {"repair_attempts": 0}})
    recorder = Recorder("壊れた")
    result = run_analyze(cfg, recorder)
    assert result.error_code is ErrorCode.LLM_INVALID_JSON
    assert len(recorder.requests) == 1


def test_repair_attempts_two_retries_twice(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"llm": {"repair_attempts": 2}})
    recorder = Recorder("壊れた 1", "壊れた 2", json.dumps(GOOD))
    result = run_analyze(cfg, recorder)
    assert result.ok
    assert result.repairs == 2


def test_the_validation_errors_are_passed_to_repair(cfg: Config) -> None:
    recorder = Recorder(json.dumps({"title": "t"}), json.dumps(GOOD))
    run_analyze(cfg, recorder)
    assert "summary" in recorder.systems()[1]


# --- 失敗（§15.1） ------------------------------------------------------


def test_a_connection_error_is_unavailable(cfg: Config) -> None:
    """到達不可は `LLM_UNAVAILABLE`（一時的失敗としてリトライ対象。§15.1）。"""

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        result = analyze(TRANSCRIPT, cfg=cfg, client=client, env=ENV)
    assert result.error_code is ErrorCode.LLM_UNAVAILABLE
    assert result.analysis is None


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_an_http_error_is_unavailable(cfg: Config, status: int) -> None:
    result = run_analyze(cfg, Recorder(status))
    assert result.error_code is ErrorCode.LLM_UNAVAILABLE


def test_a_repair_that_cannot_connect_stops(cfg: Config) -> None:
    """repair の途中で落ちたら `LLM_UNAVAILABLE`（`LLM_INVALID_JSON` にしない）。

    **リトライできる失敗とできない失敗を混ぜない**（§15.1 / §15.2）。
    """
    recorder = Recorder("壊れた", 503)
    result = run_analyze(cfg, recorder)
    assert result.error_code is ErrorCode.LLM_UNAVAILABLE


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": "nope"},
        {},
        [],
    ],
)
def test_a_malformed_envelope_does_not_raise(cfg: Config, body: object) -> None:
    """**例外を投げない**（§1.3）。応答の形が違っても `LLM_INVALID_JSON` で済ませる。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = analyze(TRANSCRIPT, cfg=cfg, client=client, env=ENV)
    assert result.error_code is ErrorCode.LLM_INVALID_JSON


def test_a_non_json_envelope_does_not_raise(cfg: Config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = analyze(TRANSCRIPT, cfg=cfg, client=client, env=ENV)
    assert result.error_code is ErrorCode.LLM_INVALID_JSON


# --- probe（§19.2 D-12） ------------------------------------------------


def test_probe_reports_the_model_and_elapsed_time(cfg: Config) -> None:
    """**状態の照会ではなく実際の疎通を見る**（§19.2 の DH-2 の注記）。"""
    recorder = Recorder('{"ok": true}')
    with client_for(recorder) as client:
        found = probe(cfg, client=client, env=ENV)
    assert found.ok
    assert found.model == MODEL
    assert found.total_tokens == 42
    assert len(recorder.requests) == 1


def test_probe_reports_a_failure(cfg: Config) -> None:
    with client_for(Recorder(503)) as client:
        found = probe(cfg, client=client, env=ENV)
    assert not found.ok
    assert "503" in found.detail


def test_probe_without_environment_variables(cfg: Config) -> None:
    found = probe(cfg, env={})
    assert not found.ok
    assert cfg.llm.endpoint_env in found.detail


def test_probe_sends_a_short_request(cfg: Config) -> None:
    """**短いリクエスト**であること（D-12）。診断で 1800 秒待たされない。"""
    recorder = Recorder('{"ok": true}')
    with client_for(recorder) as client:
        probe(cfg, client=client, env=ENV)
    assert len(recorder.users()[0]) < 100


# --- §12.2 上限は切り詰める（#112） ------------------------------------
# 2026-09-14 の実機で、**タグ 5 個の超過で 3 時間 47 分ぶんの解析が失われた。**
# §12.3 の修復は長さの超過に効かなかった。件数や文字数の上限は**表示の都合**であり、
# 超過を理由に 1 日分を捨てるのは記録の破壊である（§1.3）。


def over_limit(**overrides: object) -> str:
    document: dict[str, object] = dict(GOOD)
    document.update(overrides)
    return json.dumps(document, ensure_ascii=False)


def test_too_many_tags_are_trimmed_instead_of_failing(cfg: Config) -> None:
    """**これが実機で起きた形である。**タグ 20 個（上限 15）。"""
    recorder = Recorder(over_limit(tags=[f"タグ{n}" for n in range(20)]))
    result = run_analyze(cfg, recorder)

    assert result.ok, f"切り詰めずに失敗している: {result.error_message}"
    assert result.analysis is not None
    assert len(result.analysis.tags) == 15  # type: ignore[attr-defined]
    assert result.trimmed == ("tags: 20 -> 15",)
    assert len(recorder.requests) == 1, "修復を 1 往復させている（大きなプロンプトの再送）"


def test_every_list_section_is_trimmed(cfg: Config) -> None:
    recorder = Recorder(
        over_limit(
            key_points=[f"点{n}" for n in range(25)],
            decisions=[f"決{n}" for n in range(40)],
            ideas=[f"案{n}" for n in range(40)],
            tags=[f"タグ{n}" for n in range(20)],
        )
    )
    result = run_analyze(cfg, recorder)

    assert result.ok
    assert set(result.trimmed) == {
        "key_points: 25 -> 20",
        "decisions: 40 -> 30",
        "ideas: 40 -> 30",
        "tags: 20 -> 15",
    }


def test_a_long_title_is_trimmed(cfg: Config) -> None:
    """**リストだけ直すと、次は `title` で 1 日分を失う。**"""
    recorder = Recorder(over_limit(title="あ" * 121))
    result = run_analyze(cfg, recorder)

    assert result.ok, f"切り詰めずに失敗している: {result.error_message}"
    assert result.analysis is not None
    assert len(result.analysis.title) == 120  # type: ignore[attr-defined]
    assert result.trimmed == ("title: 121 -> 120",)


def test_a_long_summary_is_trimmed(cfg: Config) -> None:
    recorder = Recorder(over_limit(summary="あ" * 4001))
    result = run_analyze(cfg, recorder)

    assert result.ok
    assert result.trimmed == ("summary: 4001 -> 4000",)


def test_exactly_at_the_limit_is_not_trimmed(cfg: Config) -> None:
    """**上限ちょうどは切らない。**境界で 1 件削ると内容が減る。"""
    recorder = Recorder(over_limit(tags=[f"タグ{n}" for n in range(15)]))
    result = run_analyze(cfg, recorder)

    assert result.ok
    assert result.trimmed == ()
    assert len(result.analysis.tags) == 15  # type: ignore[union-attr]


def test_an_empty_summary_is_not_repaired_by_trimming(cfg: Config) -> None:
    """**`min_length` は切り詰めない。**足りないものは作れない。

    空の `summary` は本当に情報が無い場合であり、修復へ回してよい。
    """
    recorder = Recorder(over_limit(summary=""), over_limit(summary=""))
    result = run_analyze(cfg, recorder)

    assert not result.ok
    assert result.error_code is ErrorCode.LLM_INVALID_JSON
    assert len(recorder.requests) == 2, "修復へ回っていない"


def test_a_type_error_still_goes_through_repair(cfg: Config) -> None:
    """**変えるのは「長さの超過だけでは失敗にしない」ことだけ**（§12.3 は残る）。"""
    recorder = Recorder(over_limit(tags="文字列"), json.dumps(GOOD, ensure_ascii=False))
    result = run_analyze(cfg, recorder)

    assert result.ok
    assert len(recorder.requests) == 2, "型違いが修復へ回っていない"
