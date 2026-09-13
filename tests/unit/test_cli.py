"""受け入れ条件（Issue #4）をそのままテストにする。"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from tests.helpers import complete_tree, merge
from tests.spec_sync import spec_removed_subcommands, spec_subcommands
from voicedock import __version__, db, paths, worker
from voicedock.cli import IMPLEMENTED, SUBCOMMANDS
from voicedock.errors import EXIT_CONFIG, EXIT_ERROR, EXIT_OK
from voicedock.main import main

# SPEC §17.1 の表そのものを期待値にする。**直書きしない。**
# v5.0 まではここに 11 本を写していたため、SPEC の表に 1 行足しても何も落ちなかった。
SPEC_SUBCOMMANDS: tuple[str, ...] = tuple(spec_subcommands())

# §17.1 の「v5.0 で削除した 6 本」の表（v4.6→v5.0 の変更 L-4）。**戻っていないことを固定する。**
REMOVED_SUBCOMMANDS: tuple[str, ...] = tuple(spec_removed_subcommands())


def test_subcommand_set_matches_spec() -> None:
    assert tuple(SUBCOMMANDS) == SPEC_SUBCOMMANDS


def test_spec_subcommand_tables_do_not_overlap() -> None:
    """§17.1 の 2 つの表に同じ名前が載っていないこと。

    「残す」表と「削除した」表の両方に現れたら、どちらかの編集が中途半端である。
    """
    assert set(SPEC_SUBCOMMANDS).isdisjoint(REMOVED_SUBCOMMANDS), (
        SPEC_SUBCOMMANDS,
        REMOVED_SUBCOMMANDS,
    )


@pytest.mark.parametrize("name", REMOVED_SUBCOMMANDS)
def test_removed_subcommands_are_gone(name: str) -> None:
    """v5.0 で削った 6 本が argparse の使い方エラーになること（v4.6→v5.0 の変更 L-4）。

    **未実装（EXIT_ERROR）と区別する。**未実装は「いずれ実装する」を意味するが、
    この 6 本は代替へ移した結果として存在しない（§17.1 の対応表）。
    """
    assert name not in SUBCOMMANDS
    with pytest.raises(SystemExit) as excinfo:
        main([name])
    assert excinfo.value.code == EXIT_CONFIG


@pytest.mark.parametrize("name", SPEC_SUBCOMMANDS)
@pytest.mark.parametrize("extra", [["1"], ["--limit", "5"], ["--force"]])
def test_no_subcommand_takes_arguments(name: str, extra: list[str]) -> None:
    """引数を取るサブコマンドが無いこと（§17.1）。

    `--limit` / `session_id` / `--force` は v5.0 で全滅した。足したくなったら
    §17.1 の表を先に直すこと。余分な引数は argparse の使い方エラー（終了コード 2）になる。
    """
    with pytest.raises(SystemExit) as excinfo:
        main([name, *extra])
    assert excinfo.value.code == EXIT_CONFIG


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


def test_service_runs_the_worker_loop(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`service` が `worker.Worker.run()` を呼び、`service_started` を出すこと（§10.0）。

    **ループの中身は `test_worker_loop.py` が見る。**ここで確かめるのは配線だけである
    （設定を読み、DB を作り、worker を回す）。即座に終了すると compose の
    `restart: unless-stopped` でクラッシュループになるので、**走ったこと**を見る。
    """
    started: list[object] = []

    def fake_run(self: worker.Worker) -> None:
        started.append(self)
        self.log.info(
            "service_started", version="test", schema_version=self.database.schema_version()
        )

    document = merge(
        complete_tree(tmp_path),
        {"database": {"path": str(tmp_path / "data" / "voicedock.db")}},
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(config))
    monkeypatch.setattr(worker.Worker, "run", fake_run)

    assert main(["service"]) == EXIT_OK
    assert started, "worker が回っていない"
    out = capsys.readouterr().out
    assert "service_started" in out, "設定を読めたら service_started を出す（§16.2）"
    assert "schema_version=1" in out, "起動時にスキーマを自動適用する（§8.5）"
    assert (tmp_path / "data" / "voicedock.db").is_file(), "起動時に DB を作ること"


def test_service_closes_the_database(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**DB を閉じて終わる。**`with` を外すと `-wal` が残り、次回の起動で
    「チェックポイントされていない分」を抱えたまま開くことになる。
    """
    closed: list[bool] = []

    def fake_run(self: worker.Worker) -> None:
        original = self.database.close

        def spy() -> None:
            closed.append(True)
            original()

        self.database.close = spy  # type: ignore[method-assign]

    document = merge(
        complete_tree(tmp_path),
        {"database": {"path": str(tmp_path / "data" / "voicedock.db")}},
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(config))
    monkeypatch.setattr(worker.Worker, "run", fake_run)

    assert main(["service"]) == EXIT_OK
    assert closed == [True]


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
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setattr(paths, "DATA_ROOT", data_root)
    with db.connect(data_root / "voicedock.db"):
        pass
    document = merge(
        complete_tree(tmp_path), {"database": {"path": str(data_root / "voicedock.db")}}
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
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
    """サブコマンドの argv。**v5.0 以降、引数を取るサブコマンドは無い**（§17.1）。"""
    return [name]
