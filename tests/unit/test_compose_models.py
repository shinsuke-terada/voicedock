"""`compose.yaml` の `models` 長構文が §18.2 と設定に揃っていることを固定する（#13）。

**URL とモデル名は Compose が環境変数で注入する**（§18.2）。注入する側（`compose.yaml`）と
読む側（`config.yaml` の `endpoint_env` / `model_env`）で名前が食い違うと、
**D-11 が「未設定」と言い続ける。**どちらも正しく見えるので、突き合わせないと気づけない。

**Compose 5.5.1 が実際に注入した値**（2026-09-14 に実機で確認。`docs/POC.md` §11.1.1）:

```text
VOICEDOCK_LLM_URL=http://model-runner.docker.internal/v1/
VOICEDOCK_LLM_MODEL=ai/qwen3:30b-a3b-instruct-2507-q4_K_M
```

**手で与えていた値とは両方とも違った** — `/engines/v1` ではなく `/v1/`（末尾スラッシュつき）、
モデル id に `docker.io/` 接頭辞は付かない。**だから実挙動を測るまで決め打ちしない。**
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_section_code

COMPOSE = REPO_ROOT / "compose.yaml"
CONFIG_EXAMPLE = REPO_ROOT / "config" / "config.example.yaml"

SERVICE = "voicedock"
MODEL_KEY = "summarizer"


def compose_document() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def spec_compose() -> dict[str, Any]:
    """§18.2 の `yaml` ブロック（規範）。"""
    return yaml.safe_load(spec_section_code("18.2", "yaml"))


def config_example() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_EXAMPLE.read_text(encoding="utf-8"))


# --- 注入する側と読む側の名前が一致すること -----------------------------


@pytest.mark.parametrize(
    ("compose_key", "config_key"),
    [("endpoint_var", "endpoint_env"), ("model_var", "model_env")],
)
def test_the_injected_variable_names_match_the_config(compose_key: str, config_key: str) -> None:
    """**片方だけ変えると D-11 が「未設定」と言い続ける。**

    `compose.yaml` が注入する変数名と、`config.yaml` が読む変数名は同じでなければならない。
    どちらのファイルも単体では正しく見えるので、**突き合わせないと気づけない。**
    """
    injected = compose_document()["services"][SERVICE]["models"][MODEL_KEY][compose_key]
    read = config_example()["llm"][config_key]
    assert injected == read, f"{compose_key}={injected} だが config は {config_key}={read}"


def test_the_service_declares_both_variables() -> None:
    """**両方要る。**片方だけだと `endpoint_of()` が `None` を返して LLM 段が丸ごと落ちる。"""
    models = compose_document()["services"][SERVICE]["models"][MODEL_KEY]
    assert set(models) == {"endpoint_var", "model_var"}, models


# --- SPEC §18.2 との一致 -------------------------------------------------


def test_the_model_tag_matches_the_spec() -> None:
    """**モデルのタグは §18.2 が持つ。**約 18.6 GB の取得先を 2 か所で管理しない。"""
    declared = compose_document()["models"][MODEL_KEY]
    expected = spec_compose()["models"][MODEL_KEY]
    assert declared == expected, f"compose={declared} / SPEC §18.2={expected}"


def test_the_service_model_block_matches_the_spec() -> None:
    """サービス側の宣言も §18.2 と同じであること。"""
    declared = compose_document()["services"][SERVICE]["models"][MODEL_KEY]
    expected = spec_compose()["services"][SERVICE]["models"][MODEL_KEY]
    assert declared == expected, f"compose={declared} / SPEC §18.2={expected}"
