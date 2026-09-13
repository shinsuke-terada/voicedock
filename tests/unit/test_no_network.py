"""テストが外部ネットワークへ出られないことを固定する（SPEC §14.5 / §14.4 N-7）。

**「書いていない」ではなく「書いたら落ちる」状態にする。**§14.5 は「コンテナは外部
ネットワークへ出ない」と規定しており、外部 LLM API へのフォールバックを書くと
**音声の内容が外へ出る。**遮断は `tests/conftest.py` の `no_network` が autouse で掛ける。

**この遮断は自プロセスにしか効かない。**子プロセス（Helper / whisper / ffmpeg）が
ネットワークを使わないことは `test_helper_portability.py` と §20.4 が別に見る。
"""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest

from tests.conftest import NetworkBlocked


def test_an_outbound_tcp_connection_is_blocked() -> None:
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("example.com", 80))


def test_a_raw_socket_connect_is_blocked() -> None:
    """**`socket()` は作れる。**繋ごうとしたところで落ちる。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(NetworkBlocked):
        sock.connect(("93.184.216.34", 80))


def test_connect_ex_is_blocked_too() -> None:
    """**`connect_ex` は例外ではなく errno を返す。**塞ぎ忘れると静かに繋がる。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(NetworkBlocked):
        sock.connect_ex(("93.184.216.34", 80))


def test_ipv6_is_blocked() -> None:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock, pytest.raises(NetworkBlocked):
        sock.connect(("::1", 80))


def test_httpx_cannot_reach_the_outside() -> None:
    """**`llm.py` が外部 API を叩いたらここで落ちる**（§14.4 N-7）。"""
    with httpx.Client(timeout=1.0) as client, pytest.raises(Exception) as excinfo:
        client.get("http://model-runner.docker.internal/engines/v1/models")
    assert _has_network_blocked(excinfo.value), excinfo.value


def test_a_unix_socket_still_works(tmp_path: Path) -> None:
    """**`AF_UNIX` は塞がない。**塞ぐと pytest や coverage の内部を巻き添えにする。"""
    path = tmp_path / "sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))


def test_the_mock_transport_is_not_blocked() -> None:
    """**遮断が偽陽性を出していないこと。**§20.2 の LLM mock はここを通る。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = client.get("http://model-runner.docker.internal/engines/v1/models")
    assert response.json() == {"ok": True}


def _has_network_blocked(error: BaseException) -> bool:
    """`httpx` は下位の例外を `ConnectError` で包むので、原因の連鎖をたどる。"""
    seen: BaseException | None = error
    while seen is not None:
        if isinstance(seen, NetworkBlocked):
            return True
        seen = seen.__cause__ or seen.__context__
    return False
