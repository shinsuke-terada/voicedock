"""pytest の共有 fixture とマーカーの自動 skip（SPEC §20.2）。

**すべての fixture は `tmp_path` 配下に作る。**git 管理下の fixture を直接与えると、
削除テストが実 fixture を消す（§20.2）。

素の関数は `tests/helpers.py` にある（collection 時に必要なものは fixture では書けない）。
"""

from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.fake_tree import build_fake_inbox, build_fake_volumes
from tests.helpers import complete_tree, example_document, merge, parsed
from voicedock import db
from voicedock.config import Config
from voicedock.db import Database

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


# --- ネットワーク遮断（§14.5 / §14.4 N-7） -------------------------------


class NetworkBlocked(RuntimeError):
    """テストが外部へ繋ごうとした（§14.5）。"""


_BLOCKED_FAMILIES = (socket.AF_INET, socket.AF_INET6)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """**すべてのテストでネットワークを遮断する**（§14.5 / §14.4 N-7）。

    §14.5 は「**コンテナは外部ネットワークへ出ない**」と規定している。外部 LLM API への
    フォールバックを書くと**音声の内容が外へ出る。**「書いていない」ではなく
    **「書いたら落ちる」**状態にするために、統合テストだけでなく**全テストに掛ける。**
    正当にネットワークが要るテストは 1 つも無い（LLM は `httpx.MockTransport`。§20.2）。

    **遮断するのは `AF_INET` / `AF_INET6` だけである。**`AF_UNIX` を塞ぐと pytest や
    coverage の内部まで巻き添えにする（**遮断しすぎてテスト基盤ごと落とすほうが害が大きい**）。

    **`socket()` の生成そのものは許す。**生成を禁じると `httpx` の初期化が落ち、
    「繋ごうとした」ではなく理由の分からないエラーになる。

    **子プロセスには掛からない**（monkeypatch は自プロセスのみ）。Helper / whisper /
    ffmpeg がネットワークを使わないことは `test_helper_portability.py` と §20.4 が別に見る。
    """

    def blocked_connect(self: socket.socket, address: Any) -> None:
        if self.family in _BLOCKED_FAMILIES:
            raise NetworkBlocked(f"テストが外部へ繋ごうとした: {address!r}（§14.5）")
        _real_connect(self, address)

    def blocked_connect_ex(self: socket.socket, address: Any) -> int:
        if self.family in _BLOCKED_FAMILIES:
            raise NetworkBlocked(f"テストが外部へ繋ごうとした: {address!r}（§14.5）")
        return int(_real_connect_ex(self, address))

    def blocked_create_connection(address: Any, *_args: object, **_kwargs: object) -> None:
        raise NetworkBlocked(f"テストが外部へ繋ごうとした: {address!r}（§14.5）")

    _real_connect = socket.socket.connect
    _real_connect_ex = socket.socket.connect_ex
    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect_ex)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)


# --- 時刻 ----------------------------------------------------------------


@pytest.fixture
def frozen_now() -> datetime:
    """固定時刻。安定性判定・idle close・自動再試行のテストで基準にする。"""
    return datetime(2026, 9, 13, 9, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))


# --- コンテナのマウント点に相当するディレクトリ --------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """`/obsidian` 相当。**本物の Vault を表す**ので目印も置く（§13.6）。

    目印が無いディレクトリは「Docker が作った空の幻」であり、**Vault ではない**。
    それを試したいテストは自分で目印の無い木を作る（`test_vault_available.py`）。
    """
    path = tmp_path / "obsidian"
    (path / ".obsidian").mkdir(parents=True)
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
    """SQLite の置き場。**ファイルは作らない**（「DB が無い」状態を作れるようにする）。"""
    return tmp_path / "voicedock.db"


@pytest.fixture
def database(db_path: Path, frozen_now: datetime) -> Iterator[Database]:
    """スキーマ v1 を適用した空の DB（§8）。"""
    with db.connect(db_path, tz=frozen_now.tzinfo or UTC, now=frozen_now) as opened:
        yield opened


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
