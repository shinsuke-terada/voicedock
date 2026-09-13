"""ローカル LLM（Docker Model Runner）のクライアントと出力スキーマの生成（SPEC §12）。

**スキーマとプロンプトは `llm.analysis.sections` から自動生成する**（§12.2）。設定と
プロンプトが食い違う事故を構造的に防ぐため、真実の出所を config に一本化する。
`sections.<key>.enabled: false` は**プロンプトにも載らず・スキーマにも入らず・ノートにも
出ない**の 3 つが同時に成り立たなければならない。

**外部 LLM API へのフォールバックを実装してはならない**（§14.4 N-7）。通信先は
`VOICEDOCK_LLM_URL` が指すローカルの Docker Model Runner のみである。音声の内容が
外へ出る経路を作らない。

**LLM の呼び出しは `analyze()` / `probe()` の内側だけに閉じる**（§11.1(2)）。
**状態遷移は行わない**（#27）。失敗は戻り値で返す（例外を投げない）。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from voicedock.config import AnalysisConfig, Config, SectionConfig
from voicedock.errors import ErrorCode

CHAT_PATH: Final = "chat/completions"
THINK_RE: Final = re.compile(r"<think>.*?</think>", re.S)
FENCE_RE: Final = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.S)

TITLE_MAX: Final = 120
SUMMARY_MAX: Final = 4000
TASK_TEXT_MAX: Final = 500

NOT_A_SECTION: Final[frozenset[str]] = frozenset({"timeline"})
"""**LLM の出力項目ではない `sections`**（§12.2）。

`timeline` は Map 中間結果からレンダラが組み立てる（§13.4）。スキーマにもプロンプトにも
載せない。**有効 / 無効は `order` での表示可否にしか効かない。**
"""

LIST_SECTIONS: Final[tuple[str, ...]] = ("key_points", "tasks", "decisions", "ideas", "tags")
"""配列になる `sections`。`summary` だけが文字列である（§12.2）。"""

PARTIAL_EXCLUDED: Final[frozenset[str]] = frozenset({"title", "tags"})
"""Map 中間結果が持たない項目（§12.2 の `PartialAnalysis`）。"""


class PromptKind(StrEnum):
    """どのプロンプトを使うか（§12.5）。"""

    ANALYZE = "analyze"
    MAP = "map"
    REDUCE = "reduce"
    REPAIR = "repair"


class Task(BaseModel):
    """§12.2 の `Task`。`due` は ISO8601 日付または `null`。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=TASK_TEXT_MAX)
    due: str | None = None


# --- スキーマ生成（§12.2） ----------------------------------------------


def enabled_sections(analysis: AnalysisConfig) -> dict[str, SectionConfig]:
    """スキーマとプロンプトに載せる `sections`（§12.2）。

    **`timeline` を外す。**LLM の出力項目ではない。
    """
    return {
        name: section
        for name, section in analysis.sections.items()
        if section.enabled and name not in NOT_A_SECTION
    }


def build_schema(cfg: Config, *, partial: bool = False) -> type[BaseModel]:
    """`sections` から `AnalysisResult`（または `PartialAnalysis`）を動的生成する。

    `partial=True` は Map 段階の中間結果で、**`title` / `tags` を持たない**（§12.2）。

    **`max_items` が無い項目に `max_length` を付けない。**`summary` は `max_items` を
    持たない（§7.2）。既定値を勝手に入れると、**設定に書いていない上限が生まれる。**

    `title` は `summary` が有効なら常に生成する（§12.2）。`sections` に `title` という
    項目は無い — V-18 が `summary.enabled: true` を強制しているので実質「常に在る」。
    """
    sections = enabled_sections(cfg.llm.analysis)
    fields: dict[str, Any] = {}

    if "summary" in sections and not partial:
        fields["title"] = (str, Field(min_length=1, max_length=TITLE_MAX))
    if "summary" in sections:
        fields["summary"] = (str, Field(min_length=1, max_length=SUMMARY_MAX))

    for name in LIST_SECTIONS:
        section = sections.get(name)
        if section is None:
            continue
        if partial and name in PARTIAL_EXCLUDED:
            continue
        item: type = Task if name == "tasks" else str
        fields[name] = (list[item], _list_field(section))  # type: ignore[valid-type]

    return create_model(
        "PartialAnalysis" if partial else "AnalysisResult",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def _list_field(section: SectionConfig) -> Any:
    if section.max_items is None:
        return Field(default_factory=list)
    return Field(default_factory=list, max_length=section.max_items)


def render_schema_block(cfg: Config, *, partial: bool = False) -> str:
    """プロンプトへ差し込む JSON スキーマ表現（§12.5 の `{schema_block}`）。

    **生成したモデルから作る。**手書きの表を別に持つと、`sections` を切ったときに
    プロンプトとスキーマが食い違う（§12.2 がまさに防ごうとしている事故）。

    `model_json_schema()` をそのまま貼らないのは、`$defs` と `anyOf` を含む JSON Schema が
    ローカルモデルには読みにくいためである。**期待する出力の形をそのまま見せる。**
    """
    model = build_schema(cfg, partial=partial)
    lines = ["{"]
    rendered = [
        f'  "{name}": {_example(name, field)}' for name, field in model.model_fields.items()
    ]
    lines.append(",\n".join(rendered))
    lines.append("}")
    return "\n".join(lines)


def _example(name: str, field: Any) -> str:
    """1 項目の説明。**上限を明示する**（モデルが守りやすくなる）。"""
    limit = getattr(field, "metadata", None)
    maximum = _max_length(limit)
    if name == "title":
        return f'"内容を表す簡潔な日本語（{maximum or TITLE_MAX} 文字以内）"'
    if name == "summary":
        return f'"全体の要約（{maximum or SUMMARY_MAX} 文字以内）"'
    if name == "tasks":
        suffix = f"（最大 {maximum} 件）" if maximum else ""
        return f'[{{"text": "やること", "due": "2026-08-30 または null"}}]{suffix}'
    suffix = f"（最大 {maximum} 件）" if maximum else ""
    return f'["..."]{suffix}'


def _max_length(metadata: object) -> int | None:
    for entry in metadata if isinstance(metadata, Sequence) else ():
        value = getattr(entry, "max_length", None)
        if isinstance(value, int):
            return value
    return None


# --- プロンプト（§12.5） ------------------------------------------------


def load_prompt(path: Path, **fields: str) -> str:
    """テンプレートを読み、`{...}` を差し込む（§12.5）。

    **`str.format()` を使わない。**プロンプト本文には `{"text": ...}` のような JSON の例が
    入るので、`format()` は `KeyError` で落ちる。差し込むキーだけを置換する。
    """
    text = path.read_text(encoding="utf-8")
    for key, value in fields.items():
        text = text.replace("{" + key + "}", value)
    return text


def system_prompt(cfg: Config, kind: PromptKind) -> str:
    """`analyze` / `map` / `reduce` の system プロンプト（§12.5）。"""
    prompts = cfg.llm.prompts
    path = {
        PromptKind.ANALYZE: prompts.analyze,
        PromptKind.MAP: prompts.map,
        PromptKind.REDUCE: prompts.reduce,
        PromptKind.REPAIR: prompts.repair,
    }[kind]
    return load_prompt(
        Path(path),
        schema_block=render_schema_block(cfg, partial=kind is PromptKind.MAP),
        custom_instructions=cfg.llm.analysis.custom_instructions,
    )


def repair_prompt(cfg: Config, *, errors: str, previous_output: str) -> str:
    """repair の system プロンプト（§12.3 / §12.5）。

    **transcript 本文を渡さない。**渡すのは「不正だった出力」と「検証エラー」だけである。
    トークン節約と情報漏洩面の縮小の両方が理由である（§12.3）。
    """
    return load_prompt(Path(cfg.llm.prompts.repair), errors=errors, previous_output=previous_output)


# --- JSON 抽出（§12.3） -------------------------------------------------


def strip_think(text: str) -> str:
    """`<think>...</think>` を除去する（§12.3。thinking 系モデル対策）。

    **最初に行う。**除去前に括弧の釣り合いで抽出すると、**思考中に書かれた JSON を
    採ってしまう。**閉じていない `<think>` は以降すべてを思考とみなす（モデルが
    `max_tokens` で切られた場合にこうなる）。
    """
    without_pairs = THINK_RE.sub("", text)
    head, marker, _rest = without_pairs.partition("<think>")
    return head if marker else without_pairs


def extract_json(text: str) -> dict[str, object] | None:
    """応答本文から JSON オブジェクトを取り出す（§12.3 の 3 段抽出）。

    `response_format` に非対応のバックエンドでも動かすために独自に行う（§12.1）。
    **例外を投げない。**取れなければ `None`。
    """
    cleaned = strip_think(text).strip()
    for candidate in (cleaned, *_fenced(cleaned), *_balanced(cleaned)):
        parsed = _object(candidate)
        if parsed is not None:
            return parsed
    return None


def _object(candidate: str) -> dict[str, object] | None:
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _fenced(text: str) -> list[str]:
    return FENCE_RE.findall(text)


def _balanced(text: str) -> list[str]:
    """最初の `{` から対応する `}` まで（§12.3 の 3 段目）。

    **文字列リテラルを跨がない。**`{"text": "}"}` の `}` を終端と誤認すると、
    壊れた JSON を抽出して「抽出は成功したが検証で落ちる」になる。
    """
    start = text.find("{")
    if start < 0:
        return []
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return [text[start : index + 1]]
    return []


# --- 接続（§12.1） ------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    """`VOICEDOCK_LLM_URL` / `VOICEDOCK_LLM_MODEL`（§12.1 / §18.2）。"""

    base_url: str
    model: str

    @property
    def chat_url(self) -> str:
        """**末尾スラッシュを正規化する。**`.../v1` と `.../v1/` の両方が注入されうる。

        `httpx` の URL 結合に任せると `.../v1` の `v1` が消える（`urljoin` の規則）。
        """
        return f"{self.base_url.rstrip('/')}/{CHAT_PATH}"


def endpoint_of(cfg: Config, env: Mapping[str, str] | None = None) -> Endpoint | None:
    """環境変数からエンドポイントを読む。どちらか欠けていれば `None`（D-11）。"""
    source = env if env is not None else os.environ
    base_url = source.get(cfg.llm.endpoint_env, "").strip()
    model = source.get(cfg.llm.model_env, "").strip()
    if not base_url or not model:
        return None
    return Endpoint(base_url=base_url, model=model)


@dataclass(frozen=True)
class Completion:
    """1 回の `chat/completions` の結果。"""

    content: str | None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    elapsed_seconds: float = 0.0
    total_tokens: int | None = None


def complete(
    endpoint: Endpoint,
    *,
    system: str,
    user: str,
    cfg: Config,
    client: httpx.Client | None = None,
) -> Completion:
    """`POST {base}/chat/completions`（§12.1）。**例外を投げない。**

    **クライアントを使い回さない**（`client` はテストのための差し替え口）。常駐プロセスで
    保持すると、Model Runner の再起動後に死んだ接続を掴み続ける。1 リクエストの所要は
    最大 1800 秒（§7.2）なので、接続確立の費用は無視できる。
    """
    payload = {
        "model": endpoint.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg.llm.temperature,
        "top_p": cfg.llm.top_p,
        "max_tokens": cfg.llm.max_output_tokens,
        "response_format": {"type": "json_object"},
    }
    started = time.monotonic()
    try:
        opened = client if client is not None else httpx.Client()
        try:
            response = opened.post(
                endpoint.chat_url, json=payload, timeout=cfg.llm.request_timeout_seconds
            )
        finally:
            if client is None:
                opened.close()
    except httpx.HTTPError as exc:
        return Completion(
            content=None,
            error_code=ErrorCode.LLM_UNAVAILABLE,
            error_message=f"{type(exc).__name__}: {exc}",
            elapsed_seconds=time.monotonic() - started,
        )

    elapsed = time.monotonic() - started
    if response.status_code >= httpx.codes.BAD_REQUEST:
        return Completion(
            content=None,
            error_code=ErrorCode.LLM_UNAVAILABLE,
            error_message=f"HTTP {response.status_code}: {response.text[:200]}",
            elapsed_seconds=elapsed,
        )
    return Completion(
        content=_content_of(response),
        error_code=None,
        elapsed_seconds=elapsed,
        total_tokens=_tokens_of(response),
    )


def _content_of(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _tokens_of(response: httpx.Response) -> int | None:
    try:
        body = response.json()
    except ValueError:
        return None
    usage = body.get("usage") if isinstance(body, dict) else None
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    return total if isinstance(total, int) else None


# --- 解析（§12.3） -----------------------------------------------------


@dataclass(frozen=True)
class AnalyzeResult:
    """`analyze()` の結果。**状態遷移は呼び手が決める**（#27）。"""

    analysis: BaseModel | None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    elapsed_seconds: float = 0.0
    repairs: int = 0
    """repair を試した回数。0 なら 1 回目で通った。"""

    @property
    def ok(self) -> bool:
        return self.analysis is not None and self.error_code is None


def analyze(
    body: str,
    *,
    cfg: Config,
    kind: PromptKind = PromptKind.ANALYZE,
    endpoint: Endpoint | None = None,
    client: httpx.Client | None = None,
    env: Mapping[str, str] | None = None,
) -> AnalyzeResult:
    """LLM へ投げ、JSON を抽出して検証する（§12.3 の 1〜4）。

    **例外を投げない。**失敗は `error_code` で返す（`LLM_UNAVAILABLE` / `LLM_INVALID_JSON`）。
    状態遷移は #27 の責務である。

    `body` は analyze / map では transcript、reduce では中間結果の JSON 配列である。
    """
    target = endpoint if endpoint is not None else endpoint_of(cfg, env)
    if target is None:
        return AnalyzeResult(
            analysis=None,
            error_code=ErrorCode.LLM_UNAVAILABLE,
            error_message=(
                f"{cfg.llm.endpoint_env} / {cfg.llm.model_env} が設定されていません（§12.1）"
            ),
        )

    model = build_schema(cfg, partial=kind is PromptKind.MAP)
    system = system_prompt(cfg, kind)
    elapsed = 0.0

    completion = complete(target, system=system, user=body, cfg=cfg, client=client)
    elapsed += completion.elapsed_seconds
    if completion.error_code is not None:
        return AnalyzeResult(
            analysis=None,
            error_code=completion.error_code,
            error_message=completion.error_message,
            elapsed_seconds=elapsed,
        )

    raw = completion.content or ""
    for attempt in range(cfg.llm.repair_attempts + 1):
        validated, failure = _validate(raw, model)
        if validated is not None:
            return AnalyzeResult(analysis=validated, elapsed_seconds=elapsed, repairs=attempt)
        if attempt == cfg.llm.repair_attempts:
            return AnalyzeResult(
                analysis=None,
                error_code=ErrorCode.LLM_INVALID_JSON,
                error_message=failure,
                elapsed_seconds=elapsed,
                repairs=attempt,
            )
        # **transcript 本文を再送しない**（§12.3）
        retry = complete(
            target,
            system=repair_prompt(cfg, errors=failure, previous_output=raw),
            user="",
            cfg=cfg,
            client=client,
        )
        elapsed += retry.elapsed_seconds
        if retry.error_code is not None:
            return AnalyzeResult(
                analysis=None,
                error_code=retry.error_code,
                error_message=retry.error_message,
                elapsed_seconds=elapsed,
                repairs=attempt + 1,
            )
        raw = retry.content or ""

    # repair_attempts が負にならないことは V-* が保証するので到達しない
    return AnalyzeResult(  # pragma: no cover
        analysis=None,
        error_code=ErrorCode.LLM_INVALID_JSON,
        error_message="repair_attempts の扱いが不正です",
        elapsed_seconds=elapsed,
    )


def _validate(raw: str, model: type[BaseModel]) -> tuple[BaseModel | None, str]:
    """抽出 → Pydantic 検証。失敗したら `(None, エラー説明)`。"""
    document = extract_json(raw)
    if document is None:
        return None, "応答から JSON を抽出できませんでした"
    try:
        return model.model_validate(document), ""
    except ValidationError as exc:
        return None, _errors_of(exc)


def _errors_of(exc: ValidationError) -> str:
    """repair プロンプトへ渡すエラー説明（§12.5 の `{errors}`）。

    **本文を載せない。**`ValidationError` の既定の文字列には入力値が含まれる（`input=...`）。
    transcript の一部がそこへ現れると、repair のリクエストで本文を再送することになる
    （§12.3 がそれを禁じている）。
    """
    return "\n".join(
        f"- {'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )


# --- 疎通（§19.2 D-11 / D-12） ------------------------------------------


@dataclass(frozen=True)
class Probe:
    """`probe()` の結果。doctor D-12 が所要時間とトークン数を表示する。"""

    ok: bool
    detail: str
    model: str | None = None
    elapsed_seconds: float = 0.0
    total_tokens: int | None = None


def probe(
    cfg: Config,
    *,
    endpoint: Endpoint | None = None,
    client: httpx.Client | None = None,
    env: Mapping[str, str] | None = None,
) -> Probe:
    """LLM へ短いリクエストを投げて応答を得る（D-12）。**例外を投げない。**

    **状態の照会ではなく実際の疎通を見る。**`docker model status` が running でも
    モデルがロードされていないことがある（§19.2 の DH-2 の注記）。
    """
    target = endpoint if endpoint is not None else endpoint_of(cfg, env)
    if target is None:
        return Probe(ok=False, detail=f"{cfg.llm.endpoint_env} / {cfg.llm.model_env} が未設定")
    completion = complete(
        target, system='{"ok": true} と返してください。', user="ping", cfg=cfg, client=client
    )
    if completion.error_code is not None:
        return Probe(
            ok=False,
            detail=completion.error_message or "不明な失敗",
            model=target.model,
            elapsed_seconds=completion.elapsed_seconds,
        )
    return Probe(
        ok=True,
        detail=target.model,
        model=target.model,
        elapsed_seconds=completion.elapsed_seconds,
        total_tokens=completion.total_tokens,
    )
