"""`voicedock.doctor` の出力が SPEC §19.2 の書式と 12 検査に従うことを固定する。

**「PR をマージしたら doctor の行が 1 本増える」が §19.2 の動作確認手段である。**
だから検査の一覧（`CHECKS`）と §19.2 の表の突き合わせを置いてある。

**番号は詰めない**（§19.2 の方針）。D-4〜D-6 / D-14〜D-16 / D-19 は v5.0 の削減で欠番である。
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from tests.helpers import complete_tree, example_document, merge, write_heartbeat
from voicedock import db, doctor, llm, paths, status
from voicedock.config import load_config
from voicedock.db import Recording
from voicedock.doctor import DETAIL_INDENT, LABEL_WIDTH, SEPARATOR, Status
from voicedock.errors import EXIT_DOCTOR_FATAL, EXIT_OK
from voicedock.paths import DevicePath, partkey_for
from voicedock.states import PartStatus


def write_config(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


NOW = datetime(2026, 9, 13, 9, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

GOOD_PROBE = llm.Probe(
    ok=True, detail="ai/test-model", model="ai/test-model", elapsed_seconds=4.1, total_tokens=12
)


def fake_tool(path: Path, version: str = "7.1") -> Path:
    """`-version` を返す偽の ffmpeg / ffprobe（D-10）。

    **実物を要求しない。**CI に ffmpeg は無く（§18.5）、D-10 が見たいのは
    「設定されたパスが実行でき、版を取れるか」である。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho 'ffmpeg version {version} Copyright (c)'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **patch: Any) -> Path:
    """**D-1〜D-20 がすべて通る**環境を作り、設定ファイルのパスを返す。

    `/data` と `/obsidian` と `/inbox` 相当を `tmp_path` 配下へ向け、スキーマを適用した DB と
    偽 ffmpeg / ffprobe と `heartbeat.json` を置く。LLM は `llm.probe` を差し替える。

    **`inbox_root` を必ず `tmp_path` 配下にする。**既定のままだと `/inbox` を見るので、
    **開発イメージでは通り、CI の runner では SKIP になる**（2026-09-15 に踏んだ）。
    **環境に依存する検査結果を fixture の外に残さない。**
    """
    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    vault = tmp_path / "obsidian"
    (vault / ".obsidian").mkdir(parents=True, exist_ok=True)  # §13.6 の目印
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "DATA_ROOT", data_root)
    monkeypatch.setattr(paths, "VAULT_ROOT", vault)
    with db.connect(data_root / "voicedock.db"):
        pass

    tools = tmp_path / "bin"
    document = merge(
        complete_tree(tmp_path),
        {
            "database": {"path": str(data_root / "voicedock.db")},
            "obsidian": {"root": str(vault)},
            "import": {"inbox_root": str(inbox)},
            "audio": {
                "ffmpeg": str(fake_tool(tools / "ffmpeg")),
                "ffprobe": str(fake_tool(tools / "ffprobe")),
            },
        },
    )
    # **Compose 5.5.1 が実際に注入する形**（2026-09-14 実測。docs/POC.md §11.1.1）。
    # `/engines/v1` ではなく `/v1/` で、**末尾にスラッシュが付く**
    monkeypatch.setenv("VOICEDOCK_LLM_URL", "http://model-runner.docker.internal/v1/")
    monkeypatch.setenv("VOICEDOCK_LLM_MODEL", "ai/test-model")
    monkeypatch.setattr(llm, "probe", lambda *_args, **_kwargs: GOOD_PROBE)
    write_heartbeat(
        tmp_path / "state",
        updated_at=NOW.isoformat(),
        helper_version="5.5.0",
        mount_readonly=True,
        mount_mode="ro",
        # **三重ロックの 3 つを明示する。**省くと D-17 が `unknown` を出し、
        # §19.2 の `DISABLED` の例（`false` / `NOT installed`）と一致しない
        delete_source_audio=False,
        reaper_installed=False,
    )
    return write_config(tmp_path, merge(document, patch))


def run(path: Path, state_root: Path) -> tuple[str, int]:
    buf = io.StringIO()
    code = doctor.run(buf, path=path, state_root=state_root, now=NOW)
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
    assert lines[3] == f"[✓] {'Config validation':<21}105 keys, 0 errors"
    assert lines[4].startswith(f"[✓] {'Database':<21}")
    assert lines[5].startswith(f"[✓] {'Data volume':<21}")
    assert lines[6].startswith(f"[✓] {'Whisper executable':<21}")
    assert "(VAD: supported)" in lines[6], "D-7 は VAD 対応の有無を表示する（§19.2）"
    assert lines[7].startswith(f"[✓] {'Whisper model':<21}")
    assert lines[8].startswith(f"[✓] {'VAD model':<21}")
    assert lines[9] == f"[✓] {'ffmpeg / ffprobe':<21}7.1"
    assert lines[10].startswith(f"[✓] {'LLM endpoint':<21}http://")
    assert lines[11] == f"[✓] {'LLM response':<21}ai/test-model  (4.1s, 12 tokens)"
    assert lines[12].startswith(f"[✓] {'Obsidian vault':<21}")
    assert "(readable, writable, folders exist)" in lines[12]
    expected_helper = f"[✓] {'Helper':<21}running (last seen 0s ago, v5.5.0, mount=readOnly)"
    assert lines[13].startswith(expected_helper)
    # D-18 の続き行（実効値の表示）
    assert lines[14].strip().startswith("INCLUDE_VOLUMES:")
    assert lines[15].strip().startswith("EXCLUDE_VOLUMES:")
    # D-20: inbox に取り残しが無い（#120）
    assert lines[16] == f"[✓] {'Inbox orphans':<21}none"
    # **D-17 は必ず出る**（§19.2）。notice なので `[!]`
    assert lines[17] == f"[!] {'Source deletion':<21}DISABLED"
    assert "lock 1  :" in lines[18]
    assert "lock 2-A:" in lines[19]
    assert "lock 2-B:" in lines[20]
    assert lines[21] == SEPARATOR
    assert lines[22] == "13 checks passed, 0 failed, 1 notices"
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
    assert lines[3] == f"[✗] {'Config validation':<21}105 keys, 2 errors"
    assert lines[4].startswith(" " * DETAIL_INDENT)
    assert "V-3  CONFIG_INVALID_VALUE  audio.target_sample_rate" in lines[4]
    assert "V-7  CONFIG_INVALID_VALUE  session.group_by" in lines[5]
    assert lines[-1] == "1 checks passed, 1 failed, 0 notices"
    assert "[-] Database" in out, "D-1 が致命的に失敗したら以降は skip される"
    assert code == EXIT_DOCTOR_FATAL


def test_file_rules_are_reported_by_d1(tmp_path: Path) -> None:
    """V-20 / V-23 / V-25 は D-1 が報告する。

    **プロンプトの行き先を意図的に存在しないパスへ向ける。**`config.example.yaml` の
    `/app/prompts/*.txt` は #25 で実在するようになり、**コンテナでは在る / CI では
    `/app` が無い**という環境依存のテストになっていた。V-23 / V-25（モデル）は
    どちらの環境にも無いのでそのまま使える。
    """
    document = merge(
        example_document(), {"llm": {"prompts": {"analyze": str(tmp_path / "absent.txt")}}}
    )
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


def test_registry_matches_the_implemented_checks() -> None:
    """**番号は詰めない**（§19.2）。D-4〜D-6 / D-14〜D-16 / D-19 は v5.0 の削減で欠番。

    **D-19 は欠番のままにする。**v5.0 で消した「inbox / queue の滞留」と
    同じ題目の検査を v5.28 で戻したが、**番号は再利用せず D-20 にした**（#120）。

    実行順は「前提が積み上がる順」である。**D-17（削除モードの表示）を最後に置く**のは、
    それが検査ではなく**必ず見せる表示**であり、手前が落ちても出したいからではなく、
    逆に**手前の情報（heartbeat）が揃ってから判断する**ためである。
    """
    assert [c.id for c in doctor.CHECKS] == [
        "D-1",
        "D-2",
        "D-3",
        "D-7",
        "D-8",
        "D-9",
        "D-10",
        "D-11",
        "D-12",
        "D-13",
        "D-18",
        "D-20",
        "D-17",
    ]


def test_the_container_checks_match_the_spec_count() -> None:
    """§19.2 の D 表が 13 件で、実装と集合として一致すること。

    **件数のずれが最も危ない**（#55 の振り返り）。SPEC に検査を足して実装を忘れると、
    **検査が足りないまま「総仕上げ完了」になる。**
    """
    listed = set(re.findall(r"^\| \*?\*?(D-\d+)\*?\*? \| ", _section_19_2(), re.M))
    assert len(listed) == 13, sorted(listed)
    assert {c.id for c in doctor.CHECKS} == listed


def _section_19_2() -> str:
    from tests.spec_sync import spec_text

    text = spec_text()
    start = text.index("### 19.2")
    return text[start : text.index("## 20. ", start)]


# --- D-2 / D-3 -----------------------------------------------------------


def test_database_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert "[✓] Database" in out
    assert f"(schema v{db.SCHEMA_VERSION}, 0 recordings, 0 sessions, 0 events," in out
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
        # **行ごと入れ替える。**`UPDATE` だと適用済みの版が複数行あるとき
        # 全部が 99 になり、PRIMARY KEY に当たる
        opened.conn.execute("DELETE FROM schema_version")
        opened.conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (99, ?)",
            ("2026-09-13T09:00:00+00:00",),
        )
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
    # `now` を渡す。**渡さないと D-18 が実時刻で heartbeat の鮮度を見る**ので、
    # fixture の時刻との差で stale になり、このテストが日によって落ちる
    assert doctor.run(state_root=tmp_path / "state", now=NOW) == EXIT_OK
    assert "105 keys, 0 errors" in capsys.readouterr().out


# --- D-10 ffmpeg / ffprobe -----------------------------------------------


def test_d10_reports_the_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert f"[✓] {'ffmpeg / ffprobe':<21}7.1" in out
    assert code == EXIT_OK


def test_d10_shows_both_versions_when_they_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**版が食い違うのは異常である**（別の場所から入っている）。黙って片方だけ出さない。"""
    path = healthy(tmp_path, monkeypatch)
    fake_tool(tmp_path / "bin" / "ffprobe", version="6.0")
    out, _code = run(path, tmp_path / "state")
    assert "7.1 / 6.0" in out


def test_d10_fails_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = healthy(tmp_path, monkeypatch)
    (tmp_path / "bin" / "ffmpeg").unlink()
    out, code = run(path, tmp_path / "state")
    assert f"[✗] {'ffmpeg / ffprobe':<21}" in out
    assert "ffmpeg:" in out
    assert code == EXIT_DOCTOR_FATAL


def test_d10_fails_when_not_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 0 終了も失敗にする。**「在るが動かない」を「在る」と報告しない。**"""
    path = healthy(tmp_path, monkeypatch)
    (tmp_path / "bin" / "ffprobe").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    out, code = run(path, tmp_path / "state")
    assert "[✗] ffmpeg / ffprobe" in out
    assert code == EXIT_DOCTOR_FATAL


# --- D-11 / D-12 LLM ----------------------------------------------------


# --- D-9: VAD（§10.6 / §22 R-15） ---------------------------------------


def test_d9_warns_when_vad_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**VAD を切ったら黙って通さない**（v5.16→v5.17 の変更）。

    2026-09-14 の実測（同じ 26.0 分の音声。`docs/POC.md` §12.4）:

    | | `elapsed` | `rtf` | 文字数 | 区間 / ユニーク |
    |---|---|---|---|---|
    | VAD on | 209.6 秒 | 0.134 | 667 | 40 / 39 |
    | **VAD off** | **2819.3 秒** | **1.806** | **2274** | **178 / 11** |

    **13.4 倍遅く、文字数は 3.4 倍。**増えた分は無音から生成された幻覚で、
    178 区間のうち 74 区間が「はい、ご視聴ありがとうございました。」だった。
    **`rtf 1.806` では §21.2 Phase 2 の 8 時間要件を満たせない**（16 時間 → 29 時間）。

    v5.16 までは `[-] VAD model` と**黙って skip** していた。
    """
    path = healthy(tmp_path, monkeypatch, transcription={"vad": {"enabled": False}})
    out, code = run(path, tmp_path / "state")
    assert "[!] VAD model" in out, out
    assert "幻覚" in out, "何が起きるかを書く"
    assert "13 倍" in out, "どれだけ遅くなるかを書く"
    assert code == 0, "設定としては許す（失敗ではない）"


def test_d9_checks_the_model_when_vad_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """陰性対照。**有効なときはモデルの実在を見る。**何でも警告では意味が無い。"""
    path = healthy(tmp_path, monkeypatch)
    out, code = run(path, tmp_path / "state")
    assert "[✓] VAD model" in out, out
    assert code == 0


def test_d11_shows_the_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, _code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert f"[✓] {'LLM endpoint':<21}http://model-runner.docker.internal/v1/" in out


def test_d11_fails_without_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§18.2 の `models` 長構文が注入する。**未設定なら何も始まらない。**"""
    path = healthy(tmp_path, monkeypatch)
    monkeypatch.delenv("VOICEDOCK_LLM_URL")
    out, code = run(path, tmp_path / "state")
    assert "[✗] LLM endpoint" in out
    assert "VOICEDOCK_LLM_URL" in out
    assert code == EXIT_DOCTOR_FATAL


def test_d12_is_skipped_when_d11_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**D-11 が落ちたら D-12 を実行しない。**到達できない先へ投げて待つ意味がない。"""
    path = healthy(tmp_path, monkeypatch)
    monkeypatch.delenv("VOICEDOCK_LLM_MODEL")
    out, _code = run(path, tmp_path / "state")
    assert "[-] LLM response" in out


def test_d12_shows_elapsed_and_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§19.2 の `LLM response  <model>  (4.1s, 12 tokens)`。

    **所要時間を表示する**のが D-12 の要点である（§21.2 Phase 3 の判断材料）。
    """
    out, _code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert f"[✓] {'LLM response':<21}ai/test-model  (4.1s, 12 tokens)" in out


def test_d12_fails_when_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**状態の照会ではなく疎通を見る**（§19.2 の DH-2 の注記）。"""
    path = healthy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        llm, "probe", lambda *_a, **_k: llm.Probe(ok=False, detail="ConnectError: refused")
    )
    out, code = run(path, tmp_path / "state")
    assert "[✗] LLM response" in out
    assert "refused" in out
    assert code == EXIT_DOCTOR_FATAL


def test_d12_tolerates_a_missing_token_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`usage` を返さないバックエンドでも合格にする（§12.1 は必須としていない）。"""
    path = healthy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        llm,
        "probe",
        lambda *_a, **_k: llm.Probe(ok=True, detail="m", model="m", elapsed_seconds=1.0),
    )
    out, code = run(path, tmp_path / "state")
    assert "? tokens" in out
    assert code == EXIT_OK


def test_d11_and_d12_share_one_endpoint_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**env を 2 回読まない。**D-11 が解決した値を D-12 が使い回す。

    2 回読むと、その間に環境が変わった場合に**「設定は在るのに疎通先が違う」**という
    再現しにくい食い違いが生まれる。
    """
    seen: list[object] = []

    def spy(_cfg: object, *, endpoint: object = None, **_kwargs: object) -> llm.Probe:
        seen.append(endpoint)
        return GOOD_PROBE

    path = healthy(tmp_path, monkeypatch)
    monkeypatch.setattr(llm, "probe", spy)
    run(path, tmp_path / "state")
    assert len(seen) == 1
    assert seen[0] is not None, "D-11 の結果が渡っていない"


# --- D-13 Obsidian vault -------------------------------------------------


def test_d13_passes_on_a_writable_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert f"[✓] {'Obsidian vault':<21}{tmp_path / 'obsidian'}" in out
    assert "(readable, writable, folders exist)" in out
    assert code == EXIT_OK


def test_d13_does_not_create_dated_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**フォルダを作らない。**

    テンプレートは `{yyyymmdd}` を含むので、doctor を走らせるたびに**空の日付フォルダが
    Vault へ増える。**存在する最も深い祖先へ一時ファイルを書いて「作成できる」ことを
    確かめる（§19.2 の「存在するか作成できる」）。
    """
    path = healthy(tmp_path, monkeypatch)
    vault = tmp_path / "obsidian"
    run(path, tmp_path / "state")
    assert sorted(p.name for p in vault.iterdir()) == [".obsidian"], (
        "doctor が Vault にフォルダを作った"
    )


def test_d13_leaves_no_probe_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = healthy(tmp_path, monkeypatch)
    run(path, tmp_path / "state")
    assert not list((tmp_path / "obsidian").rglob("*.tmp"))


def test_d13_fails_when_the_vault_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = healthy(tmp_path, monkeypatch, **{"obsidian": {"root": str(tmp_path / "absent")}})
    out, code = run(path, tmp_path / "state")
    assert "[✗] Obsidian vault" in out
    assert "（ありません）" in out
    assert code == EXIT_DOCTOR_FATAL


def test_d13_fails_when_the_vault_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root では書き込み拒否が成立しない")
    path = healthy(tmp_path, monkeypatch)
    vault = tmp_path / "obsidian"
    vault.chmod(0o500)
    try:
        out, code = run(path, tmp_path / "state")
    finally:
        vault.chmod(0o755)
    assert "[✗] Obsidian vault" in out
    assert "書き込めません" in out
    assert code == EXIT_DOCTOR_FATAL


def test_d13_fails_when_the_vault_has_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**「書けない」と「Vault でない」を別の文言にする**（§19.2 / #134）。

    何が違うのかが分からない診断は無いのと同じである。実機では
    `mv` で退避した Vault の代わりに Docker が空ディレクトリを作り、
    doctor は OK を出し続けていた。
    """
    path = healthy(tmp_path, monkeypatch)
    (tmp_path / "obsidian" / ".obsidian").rmdir()

    out, code = run(path, tmp_path / "state")

    assert "[✗] Obsidian vault" in out
    assert ".obsidian/ がありません" in out
    assert "Obsidian で 1 度開いてください" in out, "次に何をすればよいかが書かれていない"
    assert "書き込めません" not in out, "権限エラーと同じ文言になっている"
    assert code == EXIT_DOCTOR_FATAL


def test_d13_can_be_disabled_by_an_empty_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**逃げ道**（§13.6）。設定フォルダ名を変えている Vault のために残す。"""
    path = healthy(tmp_path, monkeypatch, **{"obsidian": {"vault_marker": ""}})
    (tmp_path / "obsidian" / ".obsidian").rmdir()

    out, _ = run(path, tmp_path / "state")

    assert "[✓] Obsidian vault" in out, out


def test_d13_checks_an_existing_output_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既に在る出力フォルダが書き込み不可なら落ちること。

    **祖先をたどる実装が「在るフォルダ」を飛ばしていないか**を見る。
    """
    if os.geteuid() == 0:
        pytest.skip("root では書き込み拒否が成立しない")
    path = healthy(tmp_path, monkeypatch)
    folder = tmp_path / "obsidian" / "Daily" / "Voice" / "Raw" / "20260913"
    folder.mkdir(parents=True)
    folder.chmod(0o500)
    try:
        out, code = run(path, tmp_path / "state")
    finally:
        folder.chmod(0o755)
    assert "[✗] Obsidian vault" in out
    assert "raw の出力フォルダ" in out
    assert code == EXIT_DOCTOR_FATAL


# --- D-18 Helper --------------------------------------------------------


def test_d18_reports_a_running_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert f"[✓] {'Helper':<21}running (last seen 0s ago, v5.5.0, mount=readOnly)" in out
    assert "INCLUDE_VOLUMES:" in out
    assert "EXCLUDE_VOLUMES:" in out
    assert code == EXIT_OK


def test_d18_fails_without_a_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Helper が動かなければ録音は 1 本も取り込まれない。**致命的である。"""
    path = healthy(tmp_path, monkeypatch)
    (tmp_path / "state" / "heartbeat.json").unlink()
    out, code = run(path, tmp_path / "state")
    assert "[✗] Helper" in out
    assert "heartbeat.json がありません" in out
    assert "install.sh" in out, "直し方を示す"
    assert code == EXIT_DOCTOR_FATAL


def test_d18_fails_on_a_stale_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Helper の死を見逃さない**（§22 R-23）。

    気づけないと「新しい録音が無い」と解釈して静かに待つことになる。
    """
    path = healthy(tmp_path, monkeypatch)
    old = NOW - timedelta(seconds=99999)
    write_heartbeat(tmp_path / "state", updated_at=old.isoformat(), helper_version="5.5.0")
    out, code = run(path, tmp_path / "state")
    assert "[✗] Helper" in out
    assert "stale" in out
    assert code == EXIT_DOCTOR_FATAL


def test_d18_fails_on_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**`config_error` は致命的。**`helper.conf` が検証を通らない間、Helper は
    1 本も取り込まない（§7.4）。
    """
    path = healthy(tmp_path, monkeypatch)
    write_heartbeat(
        tmp_path / "state",
        updated_at=NOW.isoformat(),
        config_error="VOICEDOCK_HOME が /Users 配下でない",
    )
    out, code = run(path, tmp_path / "state")
    assert "[✗] Helper" in out
    assert "VOICEDOCK_HOME が /Users 配下でない" in out
    assert code == EXIT_DOCTOR_FATAL


def test_d18_shows_the_effective_volume_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = healthy(tmp_path, monkeypatch)
    write_heartbeat(
        tmp_path / "state",
        updated_at=NOW.isoformat(),
        helper_version="5.5.0",
        mount_readonly=True,
        include_volumes=["DJIMIC3"],
        exclude_volumes=["Macintosh HD"],
    )
    out, _code = run(path, tmp_path / "state")
    assert "INCLUDE_VOLUMES: ['DJIMIC3']" in out
    assert "EXCLUDE_VOLUMES: ['Macintosh HD']" in out


def test_d18_says_all_volumes_when_include_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`INCLUDE_VOLUMES` が空は「すべて」である（§7.4）。**空配列と書くと誤解される。**"""
    out, _code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert "INCLUDE_VOLUMES: (すべて)" in out


# --- D-17 削除モード（§19.2 / §14.2） -----------------------------------


def test_d17_is_always_shown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**削除モードの状態を必ず表示する**（§19.2）。無効でも notice で出す。"""
    out, code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert f"[!] {'Source deletion':<21}DISABLED" in out
    assert code == EXIT_OK, "無効なのは異常ではない（fatal=False）"


def test_d17_shows_all_three_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**三重ロックの 3 つすべてを個別に表示する**（§19.2）。

    どれが効いているかが見えないと Phase 7 の移行判断ができない。
    """
    out, _code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    assert "lock 1  : config.yaml=false, helper.conf=false" in out
    assert "lock 2-A: voicedock-reaper is NOT installed  <- deletion is impossible" in out
    assert "lock 2-B: MOUNT_MODE=ro (device mounted read-only)" in out


def test_d17_matches_the_spec_disabled_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§19.2 の `DISABLED` の例と**逐語一致**すること。"""
    out, _code = run(healthy(tmp_path, monkeypatch), tmp_path / "state")
    example = _spec_deletion_example("DISABLED")
    for line in example.splitlines():
        assert line.strip() in out, f"§19.2 の行が出ていない: {line.strip()!r}"


def test_d17_matches_the_spec_enabled_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§19.2 の `ENABLED` の例と逐語一致すること（警告ブロックを含む）。"""
    path = healthy(tmp_path, monkeypatch, **{"cleanup": {"delete_source_audio": True}})
    write_heartbeat(
        tmp_path / "state",
        updated_at=NOW.isoformat(),
        helper_version="5.5.0",
        delete_source_audio=True,
        reaper_installed=True,
        mount_readonly=False,
        mount_mode="rw",
    )
    out, code = run(path, tmp_path / "state")
    assert f"[!] {'Source deletion':<21}ENABLED" in out
    for line in _spec_deletion_example("ENABLED").splitlines():
        assert line.strip() in out, f"§19.2 の行が出ていない: {line.strip()!r}"
    assert code == EXIT_OK, "有効なのは設定であって失敗ではない"


def _write_inventory(state_root: Path, *, devices: dict[str, list[str]]) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "inventory.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "generated_at": NOW.isoformat(),
                "mount_readonly": False,
                "device_free_bytes": {},
                "devices": devices,
            }
        ),
        encoding="utf-8",
    )


def test_d17_does_not_claim_read_write_without_a_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**ロック 2-B が外れているように読ませない**（#148）。

    `mount_readonly: false` は**デバイスが 0 台のときの初期値**であり観測ではない。
    ロック 2-B の状態は Phase 7 の移行判断に使うので、**本物の異常と見分けが
    つかなくなる。**
    """
    path = healthy(tmp_path, monkeypatch)
    write_heartbeat(
        tmp_path / "state",
        updated_at=NOW.isoformat(),
        helper_version="5.5.0",
        mount_readonly=False,
        mount_mode="ro",
        delete_source_audio=False,
        reaper_installed=False,
    )
    _write_inventory(tmp_path / "state", devices={})

    out, _ = run(path, tmp_path / "state")

    assert "no device connected" in out
    assert "read-write" not in out, "未接続なのに書き込み可能と表示した"
    assert "MOUNT_MODE=ro" in out, "意図は出し続ける"
    assert f"mount={status.NO_DEVICE}" in out, "D-18 の行も直っていること"


def test_d17_still_reports_a_connected_writable_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**塞ぎすぎない。**繋がっていて書き込み可能なら、そう言う（本物の異常）。"""
    path = healthy(tmp_path, monkeypatch)
    write_heartbeat(
        tmp_path / "state",
        updated_at=NOW.isoformat(),
        helper_version="5.5.0",
        mount_readonly=False,
        mount_mode="ro",
        delete_source_audio=False,
        reaper_installed=False,
    )
    _write_inventory(tmp_path / "state", devices={"DJIMIC3": ["a/b_orig.wav"]})

    out, _ = run(path, tmp_path / "state")

    assert "device mounted read-write" in out, "本物の食い違いを隠した"


def test_d17_unknown_locks_fall_to_the_safe_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**不明は安全側（削除しない）へ倒す**（§7.5）。"""
    path = healthy(tmp_path, monkeypatch, **{"cleanup": {"delete_source_audio": True}})
    (tmp_path / "state" / "heartbeat.json").unlink()
    out, _code = run(path, tmp_path / "state")
    assert f"[!] {'Source deletion':<21}DISABLED" in out
    assert "helper.conf=unknown" in out
    assert "voicedock-reaper: unknown" in out


def test_d17_does_not_reimplement_the_lock_logic() -> None:
    """**判定は `status.Locks` を使う**（§14.2）。

    「削除が起こりうるか」を決める場所が 2 つになると、片方だけ直したときに
    **doctor が DISABLED と言いながら実際には削除される。**
    """
    source = Path(doctor.__file__).read_text(encoding="utf-8")
    assert "status.Locks(" in source
    body = source[source.index("def check_source_deletion") : source.index("def _flag(")]
    assert "and" not in body.replace("and the reaper", ""), "論理式を書き直している"


def _spec_deletion_example(label: str) -> str:
    """§19.2 の `Source deletion` の例（`ENABLED` / `DISABLED`）を SPEC から読む。"""
    blocks: list[str] = re.findall(r"```text\n(.*?)\n```", _section_19_2(), re.S)
    found = [b for b in blocks if b.lstrip().startswith("[!] Source deletion") and label in b]
    assert found, f"§19.2 に {label} の例がありません"
    example: str = found[-1]
    return example


# --- D-20 inbox の取り残し（#120） --------------------------------------


def inbox_pair_for(path: Path, *, device: str = "DJIMIC3") -> None:
    """Helper が置いた形（`.wav` + `.meta.json`）を作る（§10.2）。"""
    stem = "TX00_MIC001_20260912_120950"
    folder = "TX_MIC001_20260912_120950"
    root = path / device / folder
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{stem}_orig.wav").write_bytes(b"x" * 2048)
    (root / f"{stem}_orig.wav.meta.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "device_id": device,
                "relpath": f"{folder}/{stem}_orig.wav",
                "size": 2048,
                "mtime": 1789000000.0,
                "sha256": "0" * 64,
                "copied_at": "2026-09-12T12:09:50+09:00",
                "helper_version": "5.5.0",
            }
        ),
        encoding="utf-8",
    )


def test_d20_notices_an_orphan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**取り残しは `NOTICE`。`FAIL` にしない**（記録は失われていない。#120）。"""
    path = healthy(tmp_path, monkeypatch)
    cfg = load_config(path)
    inbox_pair_for(Path(cfg.import_.inbox_root))
    with db.connect(cfg.database.path, busy_timeout_ms=1000, tz=cfg.tz) as opened:
        opened.insert_recording(_orphan_row())

    out, code = run(path, tmp_path / "state")
    assert "[!] Inbox orphans" in out
    assert "1 files" in out
    assert code == EXIT_OK, "**FAIL にしてはならない**"


def test_d20_is_ok_when_the_part_is_not_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """処理待ちの原本は取り残しではない。"""
    path = healthy(tmp_path, monkeypatch)
    cfg = load_config(path)
    inbox_pair_for(Path(cfg.import_.inbox_root))
    with db.connect(cfg.database.path, busy_timeout_ms=1000, tz=cfg.tz) as opened:
        opened.insert_recording(_orphan_row(state=PartStatus.DISCOVERED))

    out, _ = run(path, tmp_path / "state")
    assert "[✓] Inbox orphans" in out


def _orphan_row(state: str = PartStatus.COMPLETED) -> Recording:
    folder = "TX_MIC001_20260912_120950"
    relpath = f"{folder}/TX00_MIC001_20260912_120950_orig.wav"
    return Recording(
        partkey=partkey_for("DJIMIC3", DevicePath(PurePosixPath(relpath))),
        device_id="DJIMIC3",
        source_folder=folder,
        transmitter_id="TX00",
        mic_index=1,
        started_at="2026-09-12T12:09:50+09:00",
        status=state,
        updated_at="2026-09-12T12:09:50+09:00",
        duration_seconds=60.0,
        source_path=relpath,
    )
