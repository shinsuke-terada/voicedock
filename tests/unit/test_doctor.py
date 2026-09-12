"""`voicedock.doctor` の出力が SPEC §19.2 の書式に従うことを固定する。

本チケットは D-1 だけを実装する。**書式（記号・桁・サマリ）はここで固定し、
以降のチケットは検査を足すだけにする。**
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.helpers import complete_tree, example_document, merge
from voicedock import db, doctor, paths
from voicedock.doctor import DETAIL_INDENT, LABEL_WIDTH, SEPARATOR, Status
from voicedock.errors import EXIT_DOCTOR_FATAL, EXIT_OK


def write_config(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **patch: Any) -> Path:
    """D-1〜D-3 がすべて通る環境を作り、設定ファイルのパスを返す。

    `/data` 相当を `tmp_path` 配下へ向け、スキーマを適用した DB を置く。
    """
    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "DATA_ROOT", data_root)
    with db.connect(data_root / "voicedock.db"):
        pass
    document = merge(
        complete_tree(tmp_path), {"database": {"path": str(data_root / "voicedock.db")}}
    )
    return write_config(tmp_path, merge(document, patch))


def run(path: Path, state_root: Path) -> tuple[str, int]:
    buf = io.StringIO()
    code = doctor.run(buf, path=path, state_root=state_root)
    return buf.getvalue(), code


def test_separator_and_label_widths() -> None:
    """§19.2: 区切り線は 56 桁、ラベル列は 21 桁、続き行は 27 桁。"""
    assert len(SEPARATOR) == 56
    assert LABEL_WIDTH == 21
    assert DETAIL_INDENT == 27


def test_all_ok_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = healthy(tmp_path, monkeypatch)
    lines = run(path, tmp_path / "state")[0].splitlines()
    assert lines[0] == "VoiceDock doctor"
    assert lines[1] == SEPARATOR
    assert lines[2] == f"[✓] {'Config file':<21}{path}"
    assert lines[3] == f"[✓] {'Config validation':<21}110 keys, 0 errors"
    assert lines[4].startswith(f"[✓] {'Database':<21}")
    assert lines[5].startswith(f"[✓] {'Data volume':<21}")
    assert lines[6] == SEPARATOR
    assert lines[7] == "4 checks passed, 0 failed, 0 notices"
    assert run(path, tmp_path / "state")[1] == EXIT_OK


def test_violations_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = healthy(
        tmp_path,
        monkeypatch,
        **{"audio": {"target_sample_rate": 44100}, "session": {"group_by": "week"}},
    )
    out, code = run(path, tmp_path / "state")
    lines = out.splitlines()

    assert lines[2] == f"[✓] {'Config file':<21}{path}"
    assert lines[3] == f"[✗] {'Config validation':<21}110 keys, 2 errors"
    assert lines[4].startswith(" " * DETAIL_INDENT)
    assert "V-3  CONFIG_INVALID_VALUE  audio.target_sample_rate" in lines[4]
    assert "V-7  CONFIG_INVALID_VALUE  session.group_by" in lines[5]
    assert lines[-1] == "1 checks passed, 1 failed, 0 notices"
    assert "[-] Database" in out, "D-1 が致命的に失敗したら以降は skip される"
    assert code == EXIT_DOCTOR_FATAL


def test_file_rules_are_reported_by_d1(tmp_path: Path) -> None:
    """V-20 / V-23 / V-25 は D-1 が報告する（#13 / #25 が未了の実環境で出る形）。"""
    document = example_document()
    path = write_config(tmp_path, document)
    out, code = run(path, tmp_path / "state")
    assert "V-20  CONFIG_INVALID_VALUE  llm.prompts.analyze" in out
    assert "V-23  WHISPER_MODEL_MISSING  transcription.model" in out
    assert "V-25  WHISPER_MODEL_MISSING  transcription.vad.model" in out
    assert code == EXIT_DOCTOR_FATAL


def test_missing_config_file(tmp_path: Path) -> None:
    path = tmp_path / "nope.yaml"
    out, code = run(path, tmp_path / "state")
    lines = out.splitlines()
    assert lines[2] == f"[✗] {'Config file':<21}{path}  （設定ファイルがありません）"
    assert lines[3] == f"[-] {'Config validation':<21}skipped（設定ファイルを読めません）"
    assert lines[-1] == "0 checks passed, 1 failed, 0 notices"
    assert code == EXIT_DOCTOR_FATAL


def test_broken_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("a: [1,\n", encoding="utf-8")
    out, code = run(path, tmp_path / "state")
    assert "[✗] Config file" in out
    assert "YAML として不正" in out
    assert "skipped" in out
    assert code == EXIT_DOCTOR_FATAL


def test_non_mapping_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- 1\n", encoding="utf-8")
    out, code = run(path, tmp_path / "state")
    assert "トップレベルがマッピングでありません" in out
    assert code == EXIT_DOCTOR_FATAL


def test_summary_ignores_skipped_rows() -> None:
    """§19.2: `[-]` は数えない（既に数えた失敗の結果だから）。"""
    rows = [
        doctor.Row(Status.OK, "a", ""),
        doctor.Row(Status.FAIL, "b", ""),
        doctor.Row(Status.SKIP, "c", ""),
        doctor.Row(Status.NOTICE, "d", ""),
    ]
    assert doctor._summary(rows) == "1 checks passed, 1 failed, 1 notices"


def test_row_render_indents_extra_lines() -> None:
    row = doctor.Row(Status.NOTICE, "Source deletion", "ENABLED", ("lock 1  : true", "lock 2-A"))
    assert row.render() == [
        f"[!] {'Source deletion':<21}ENABLED",
        f"{' ' * DETAIL_INDENT}lock 1  : true",
        f"{' ' * DETAIL_INDENT}lock 2-A",
    ]


def test_registry_has_d1_to_d3() -> None:
    """D-4 以降は各担当チケットが足す（#34 で総仕上げ）。"""
    assert [c.id for c in doctor.CHECKS] == ["D-1", "D-2", "D-3"]


# --- D-2 / D-3 -----------------------------------------------------------


def test_database_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert "[✓] Database" in out
    assert "(schema v1, 0 recordings, 0 sessions, 0 events," in out
    assert code == EXIT_OK


def test_database_row_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DB が無ければ失敗し、**doctor が DB を作らない**こと（診断は副作用を持たない）。"""
    path = healthy(tmp_path, monkeypatch)
    db_file = tmp_path / "data" / "voicedock.db"
    for suffix in ("", "-wal", "-shm"):
        target = db_file.with_name(db_file.name + suffix)
        if target.exists():
            target.unlink()

    out, code = run(path, tmp_path / "state")
    assert "[✗] Database" in out
    assert "（ありません）" in out
    assert code == EXIT_DOCTOR_FATAL
    assert not db_file.exists(), "doctor が DB を作ってしまった"


def test_database_row_on_version_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = healthy(tmp_path, monkeypatch)
    with db.connect(tmp_path / "data" / "voicedock.db", migrate=False) as opened:
        opened.conn.execute("UPDATE schema_version SET version = 99")
        opened.conn.commit()
    out, code = run(path, tmp_path / "state")
    assert "[✗] Database" in out
    assert "schema v99 は未対応" in out
    assert code == EXIT_DOCTOR_FATAL


def test_data_volume_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert f"[✓] {'Data volume':<21}{tmp_path / 'data'} writable, " in out
    assert "GiB free" in out
    assert code == EXIT_OK


def test_data_volume_row_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = healthy(tmp_path, monkeypatch)
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path / "nope")
    out, code = run(path, tmp_path / "state")
    assert "[✗] Data volume" in out
    assert code == EXIT_DOCTOR_FATAL


def test_data_volume_row_when_low_on_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空きが `free_space_margin_bytes` を下回れば失敗（§19.2 D-3）。

    `staging_max_bytes` も一緒に上げる（V-22 が「margin より大きいこと」を要求するため）。
    """
    huge = 1 << 50  # 1 PiB。実在のディスクでは必ず下回る
    path = healthy(
        tmp_path,
        monkeypatch,
        **{"import": {"free_space_margin_bytes": huge, "staging_max_bytes": huge + 1}},
    )
    out, code = run(path, tmp_path / "state")
    assert "[✗] Data volume" in out
    assert "を下回る" in out
    assert code == EXIT_DOCTOR_FATAL


def test_probe_file_is_cleaned_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """書き込み検査に使った一時ファイルを残さないこと。"""
    path = healthy(tmp_path, monkeypatch)
    run(path, tmp_path / "state")
    assert list((tmp_path / "data").glob(".voicedock-doctor-probe*")) == []


def test_run_defaults_to_the_env_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = healthy(tmp_path, monkeypatch)
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(path))
    assert doctor.run(state_root=tmp_path / "state") == EXIT_OK
    assert "110 keys, 0 errors" in capsys.readouterr().out
