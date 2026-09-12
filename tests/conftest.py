"""pytest の共有 fixture とマーカーの自動 skip（SPEC §20.2）。

**すべての fixture は `tmp_path` 配下に作る。**git 管理下の fixture を直接与えると、
削除テストが実 fixture を消す（§20.2）。

素の関数は `tests/helpers.py` にある（collection 時に必要なものは fixture では書けない）。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.fake_tree import build_fake_inbox, build_fake_volumes
from tests.helpers import complete_tree, example_document, merge, parsed
from voicedock.config import Config

# --- マーカー ------------------------------------------------------------

_MARKER_REASON = "（このテストは make test（コンテナ内）で走る）"


def _has_tools(*names: str) -> bool:
    return all(shutil.which(name) is not None for name in names)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """外部の道具が要るテストを、**利用可能性を見て**自動 skip する。

    CI は uv イメージで走り `ffmpeg` も `whisper-cli` も無い（§18.5 のとおり CI は
    プロジェクトイメージを構築しない）。理由文に「`make test` で走る」と書くのは、
    CI に `s` が出たときに壊れていると誤解されないようにするためである。
    """
    available = {
        "needs_ffmpeg": _has_tools("ffmpeg", "ffprobe"),
        "needs_whisper": _has_tools("whisper-cli"),
        "needs_llm": "VOICEDOCK_LLM_URL" in os.environ,
    }
    for item in items:
        for marker, ok in available.items():
            if marker in item.keywords and not ok:
                item.add_marker(pytest.mark.skip(reason=f"{marker} が無い{_MARKER_REASON}"))


# --- 時刻 ----------------------------------------------------------------


@pytest.fixture
def frozen_now() -> datetime:
    """固定時刻。安定性判定・idle close・自動再試行のテストで基準にする。"""
    return datetime(2026, 9, 13, 9, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))


# --- コンテナのマウント点に相当するディレクトリ --------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """`/obsidian` 相当。"""
    path = tmp_path / "obsidian"
    path.mkdir()
    return path


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    """`/state` 相当。Helper の報告（§7.5）を置く。"""
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def queue_root(tmp_path: Path) -> Path:
    """`/queue` 相当。削除要求（§14.3）を置く。"""
    path = tmp_path / "queue" / "delete"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """SQLite の置き場。**ファイルは作らない。**

    TODO(#10): スキーマの初期化をここへ足す。いま DDL を書くと `db.py` と二重管理になる。
    """
    return tmp_path / "voicedock.db"


# --- 偽 inbox / 偽ボリューム ---------------------------------------------


@pytest.fixture
def fake_inbox(tmp_path: Path) -> Path:
    """Helper が置いた状態の inbox（`.meta.json` 付き。§10.2）。"""
    return build_fake_inbox(tmp_path / "inbox")


@pytest.fixture
def fake_volumes(tmp_path: Path) -> Path:
    """reaper のテスト用の偽ボリューム（§20.4）。`.meta.json` は無い。"""
    return build_fake_volumes(tmp_path / "volumes")


# --- 設定 ----------------------------------------------------------------


@pytest.fixture
def config_document() -> dict[str, Any]:
    """`config.example.yaml` の内容。**毎回新しい辞書を返す**（テストが書き換えてよい）。"""
    return example_document()


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., Config]:
    """`patch` を当てた `Config` を返す factory。

    ファイル実在検査を通る木（`complete_tree`）の上に patch を重ねる。
    """

    def factory(patch: Mapping[str, Any] | None = None) -> Config:
        return parsed(merge(complete_tree(tmp_path), patch or {}))

    return factory
