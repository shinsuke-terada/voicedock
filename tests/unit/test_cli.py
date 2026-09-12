"""受け入れ条件（Issue #4）をそのままテストにする。"""

from __future__ import annotations

import pathlib
import signal

import pytest
import yaml

from tests.helpers import complete_tree
from voicedock import __version__
from voicedock.cli import IMPLEMENTED, SUBCOMMANDS
from voicedock.errors import EXIT_CONFIG, EXIT_ERROR, EXIT_OK
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
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """service は設定を検証したあと、未実装を明示してから待機する。

    TODO(#19): worker_loop() が実装されたら、このテストは書き換わる。
    即座に終了すると compose の restart: unless-stopped でクラッシュループになるため、
    「待機すること」自体が現時点の仕様である。
    """
    paused = False

    def _fake_pause() -> None:
        nonlocal paused
        paused = True

    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(complete_tree(tmp_path)), encoding="utf-8")
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(config))
    monkeypatch.setattr(signal, "pause", _fake_pause)

    assert main(["service"]) == EXIT_OK
    assert paused, "service は待機しなければならない（即座に終了するとクラッシュループになる）"
    out = capsys.readouterr().out
    assert "service_started" in out, "設定を読めたら service_started を出す（§16.2）"
    assert "未実装" in out


def test_service_returns_config_exit_code_on_violation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """設定違反では終了コード 2 を返し、stderr へ規則 ID とキー名を出す（§17.3）。"""
    document = complete_tree(tmp_path)
    document["audio"]["target_sample_rate"] = 44100
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(config))

    assert main(["service"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "V-3  CONFIG_INVALID_VALUE  audio.target_sample_rate" in err


def test_service_returns_config_exit_code_when_the_file_is_missing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(tmp_path / "nope.yaml"))
    assert main(["service"]) == EXIT_CONFIG
    assert "設定ファイルがありません" in capsys.readouterr().err


def test_doctor_runs(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(complete_tree(tmp_path)), encoding="utf-8")
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(config))
    # state_root は既定（/state）のまま。delete_source_audio が false のあいだ
    # V-30 は heartbeat を読まないので、結果に影響しない（§7.3）
    assert main(["doctor"]) == EXIT_OK
    assert "VoiceDock doctor" in capsys.readouterr().out


def test_no_subcommand_prints_help_and_returns_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == EXIT_ERROR
    assert "<subcommand>" in capsys.readouterr().out


def test_unknown_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["no-such-command"])
    assert excinfo.value.code == EXIT_CONFIG  # argparse の使い方エラーは 2


def _minimal_argv(name: str) -> list[str]:
    """必須の位置引数を持つサブコマンドに、最小限の引数を足す（SPEC §17.1）。"""
    if name == "show":
        return [name, "1"]
    if name == "retry":
        return [name, "1"]
    return [name]
