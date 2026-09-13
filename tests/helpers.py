"""テスト間で共有する素の関数。

**fixture ではなく関数として置く。**parametrize の引数など collection 時に必要なものは
fixture では書けない。pytest fixture は `tests/conftest.py` にあり、ここを呼ぶ。

**テスト同士で import しないための置き場でもある。**以前は `test_doctor.py` と `test_cli.py` が
`test_config.py` からヘルパを import していた。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from tests.fixtures.fake_whisper import write_fake_whisper
from voicedock.config import Config, parse_config

REPO_ROOT = Path(__file__).parents[1]
EXAMPLE_PATH = REPO_ROOT / "config" / "config.example.yaml"


def example_document() -> dict[str, Any]:
    """`config/config.example.yaml` をパースして返す（§7.2 の完全な写し）。"""
    document = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """`patch` を `base` へ再帰的に深くマージする（辞書は再帰、それ以外は置換）。"""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = merge(current, value)
        else:
            merged[key] = value
    return merged


def complete_tree(tmp_path: Path) -> dict[str, Any]:
    """ファイル実在検査（V-20 / V-23 / V-24 / V-25）を通る設定を作る。

    プロンプトは #25、モデルは #13 の成果物であり実環境にはまだ無い。
    **検査そのものは今のうちに固定しておく。**
    """
    for name in ("analyze", "map", "reduce", "repair"):
        (tmp_path / f"{name}.txt").write_text("x", encoding="utf-8")
    (tmp_path / "whisper.bin").write_bytes(b"\0" * 16)
    (tmp_path / "vad.bin").write_bytes(b"\0" * 16)
    # **`--help` に VAD フラグを含める。**doctor D-7 は `--help` の出力で VAD 対応を
    # 判定する（§10.6 の注記）。`exit 0` だけのスクリプトにすると、D-1 を見るだけの
    # テストが「健全な環境」を表せなくなる（D-7 が notice になる）
    write_fake_whisper(tmp_path / "whisper-cli")
    executable = tmp_path / "whisper-cli"
    return merge(
        example_document(),
        {
            "llm": {
                "prompts": {
                    "analyze": str(tmp_path / "analyze.txt"),
                    "map": str(tmp_path / "map.txt"),
                    "reduce": str(tmp_path / "reduce.txt"),
                    "repair": str(tmp_path / "repair.txt"),
                }
            },
            "transcription": {
                "executable": str(executable),
                "model": str(tmp_path / "whisper.bin"),
                "vad": {"model": str(tmp_path / "vad.bin")},
            },
        },
    )


def parsed(document: Mapping[str, Any]) -> Config:
    """違反が無いことを確かめてから `Config` を返す。"""
    cfg, violations = parse_config(document)
    assert violations == [], violations
    assert cfg is not None
    return cfg


def write_heartbeat(state_root: Path, **fields: Any) -> Path:
    """`state/heartbeat.json` を**指定したフィールドだけ**書く（§7.5）。

    既定値で埋めた完全なものが要るときは `tests.fixtures.fake_tree.write_state()` を使う。
    こちらは「フィールドが欠けている」状態を作るために使う。
    """
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / "heartbeat.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path
