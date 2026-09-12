"""受け入れ条件（Issue #4）をそのままテストにする。"""

from __future__ import annotations

import signal

import pytest

from voicedock import __version__
from voicedock.cli import (
    EXIT_CONFIG,
    EXIT_DOCTOR_FATAL,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_UNHEALTHY,
    IMPLEMENTED,
    SUBCOMMANDS,
)
from voicedock.main import main

# SPEC §17.1 の 11 サブコマンド。ここを唯一の期待値とし、漏れたらテストが落ちる。
SPEC_SUBCOMMANDS: tuple[str, ...] = (
    "service",
    "scan",
    "status",
    "history",
    "show",
    "retry",
    "pending",
    "cleanup",
    "doctor",
    "health",
    "version",
)


def test_subcommand_set_matches_spec() -> None:
    assert tuple(SUBCOMMANDS) == SPEC_SUBCOMMANDS


def test_version_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == __version__


def test_help_lists_all_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == EXIT_OK

    out = capsys.readouterr().out
    missing = [name for name in SPEC_SUBCOMMANDS if name not in out]
    assert not missing, f"--help に現れないサブコマンド: {missing}"


@pytest.mark.parametrize("name", [n for n in SPEC_SUBCOMMANDS if n not in IMPLEMENTED])
def test_unimplemented_returns_error(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = _minimal_argv(name)
    assert main(argv) == EXIT_ERROR
    assert "未実装" in capsys.readouterr().err


def test_service_waits_and_announces_stub(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """service はスタブである間、未実装を明示してから待機する。

    TODO(#19): worker_loop() が実装されたら、このテストは書き換わる。
    即座に終了すると compose の restart: unless-stopped でクラッシュループになるため、
    「待機すること」自体が現時点の仕様である。
    """
    paused = False

    def _fake_pause() -> None:
        nonlocal paused
        paused = True

    monkeypatch.setattr(signal, "pause", _fake_pause)
    assert main(["service"]) == EXIT_OK
    assert paused, "service は待機しなければならない（即座に終了するとクラッシュループになる）"
    assert "未実装" in capsys.readouterr().out


def test_no_subcommand_prints_help_and_returns_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == EXIT_ERROR
    assert "<subcommand>" in capsys.readouterr().out


def test_unknown_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["no-such-command"])
    assert excinfo.value.code == EXIT_CONFIG  # argparse の使い方エラーは 2


def test_exit_codes_match_spec() -> None:
    # SPEC §17.3
    assert (EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_UNHEALTHY, EXIT_DOCTOR_FATAL) == (0, 1, 2, 3, 4)


def _minimal_argv(name: str) -> list[str]:
    """必須の位置引数を持つサブコマンドに、最小限の引数を足す（SPEC §17.1）。"""
    if name == "show":
        return [name, "1"]
    if name == "retry":
        return [name, "1"]
    return [name]
