"""`voicedock.config` が SPEC §7 と一致することを固定する。

§7.2 の全キーと §7.3 の規則表は `docs/SPEC.md` から parse して突き合わせる。SPEC に
キーや規則を足して実装を忘れる事故、実装だけ増やして SPEC に書き忘れる事故が即座に落ちる。
"""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import ValidationError

from tests.spec_sync import spec_config_example, spec_validation_rules
from voicedock import config as config_module
from voicedock.config import (
    IMPLEMENTED_RULES,
    NO_RULE,
    RULES,
    Config,
    ConfigError,
    ConfigUnreadable,
    Violation,
    check_files,
    check_locks,
    config_path,
    key_count,
    load_config,
    logger_for,
    parse_config,
    startup_notices,
)
from voicedock.errors import ErrorCode

REPO_ROOT = Path(__file__).parents[2]
EXAMPLE_PATH = REPO_ROOT / "config" / "config.example.yaml"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "config"

# 専用のテスト関数で扱う規則（fixture を置かないもの）
FILE_RULES = frozenset({"V-20", "V-23", "V-24", "V-25"})
LOCK_RULES = frozenset({"V-26", "V-30"})

NOW = datetime(2026, 8, 30, 7, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))


# --- ヘルパ --------------------------------------------------------------


def example_document() -> dict[str, Any]:
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


def fixtures() -> list[dict[str, Any]]:
    loaded = []
    for path in sorted(FIXTURE_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path
        data["__name__"] = path.stem
        loaded.append(data)
    assert loaded, f"fixture が見つかりません: {FIXTURE_DIR}"
    return loaded


def complete_tree(tmp_path: Path) -> dict[str, Any]:
    """ファイル実在検査（V-20 / V-23 / V-24 / V-25）を通る設定を作る。

    プロンプトは #25、モデルは #13 の成果物であり実環境にはまだ無い。
    **検査そのものは今のうちに固定しておく。**
    """
    for name in ("analyze", "map", "reduce", "repair"):
        (tmp_path / f"{name}.txt").write_text("x", encoding="utf-8")
    (tmp_path / "whisper.bin").write_bytes(b"\0" * 16)
    (tmp_path / "vad.bin").write_bytes(b"\0" * 16)
    executable = tmp_path / "whisper-cli"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
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
    cfg, violations = parse_config(document)
    assert violations == [], violations
    assert cfg is not None
    return cfg


def write_heartbeat(state_root: Path, **fields: Any) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "heartbeat.json").write_text(json.dumps(fields), encoding="utf-8")


# --- SPEC 整合 -----------------------------------------------------------


def test_example_matches_spec() -> None:
    """`config.example.yaml` が §7.2 の YAML ブロックと完全一致すること。"""
    assert example_document() == spec_config_example()


def test_example_has_no_violations() -> None:
    """例そのものが §7.3 を通ること（ファイル実在検査は除く）。"""
    cfg, violations = parse_config(example_document())
    assert violations == []
    assert cfg is not None


def test_implemented_rules_match_spec() -> None:
    """§7.3 の非廃止規則と `RULES` が 1 対 1 であること。"""
    assert frozenset(spec_validation_rules()) == IMPLEMENTED_RULES


def test_rules_follow_spec_order() -> None:
    assert [r.id for r in RULES] == list(spec_validation_rules())


def test_fixtures_cover_every_rule() -> None:
    """全規則にテストがあること。SPEC に規則を足すと落ちる。"""
    covered = {f["rule"] for f in fixtures()} | FILE_RULES | LOCK_RULES
    assert covered == IMPLEMENTED_RULES


# --- 個別の規則 ----------------------------------------------------------


@pytest.mark.parametrize("fixture", fixtures(), ids=lambda f: str(f["__name__"]))
def test_fixture_yields_expected_violation(fixture: dict[str, Any]) -> None:
    document = merge(example_document(), fixture["patch"])
    cfg, violations = parse_config(document)
    assert cfg is None
    assert len(violations) == 1, violations
    found = violations[0]
    assert found.rule == fixture["rule"]
    assert found.code == ErrorCode(fixture["code"])
    assert found.key == fixture["key"]
    assert found.message


def test_missing_key_is_reported() -> None:
    """§7.3 に規則が無いキーの欠落は `NO_RULE` として報告し、起動は中止する。

    §7.2 の形をしていない設定では処理を続けられない。全フィールドを必須にしているのは、
    既定値をコードへ置くと §7.2 と二重管理になるからである（§7.4 / 付録 B の F-3）。
    """
    document = example_document()
    del document["database"]["path"]
    _cfg, violations = parse_config(document)
    assert len(violations) == 1
    assert violations[0].rule == NO_RULE
    assert violations[0].code is ErrorCode.CONFIG_INVALID_VALUE
    assert violations[0].key == "database.path"


def test_missing_key_with_a_rule_is_reported_as_that_rule() -> None:
    """V 規則が付いているキーの欠落は、その規則違反として報告する。

    `audio.target_codec` が無いことは V-3（`pcm_s16le` であること）の違反そのものである。
    """
    document = example_document()
    del document["audio"]["target_codec"]
    _cfg, violations = parse_config(document)
    assert [(v.rule, v.key) for v in violations] == [("V-3", "audio.target_codec")]


def test_wrong_subtree_type_is_reported() -> None:
    document = merge(example_document(), {"audio": 5})
    _cfg, violations = parse_config(document)
    assert len(violations) == 1
    assert violations[0].rule == NO_RULE
    assert violations[0].key == "audio"


def test_import_key_is_reported_by_alias() -> None:
    """`import` は予約語なので属性名は `import_` だが、**報告は `import.` で行う**。"""
    document = merge(example_document(), {"import": {"inbox_retain": "both"}})
    _cfg, violations = parse_config(document)
    assert [v.key for v in violations] == ["import.inbox_retain"]


def test_unknown_top_level_key() -> None:
    document = merge(example_document(), {"unknown_key": 1})
    _cfg, violations = parse_config(document)
    assert len(violations) == 1
    assert violations[0].rule == "V-1"
    assert violations[0].code is ErrorCode.CONFIG_UNKNOWN_KEY
    assert violations[0].key == "unknown_key"


# --- ファイル実在検査 ----------------------------------------------------


def test_complete_tree_passes_file_checks(tmp_path: Path) -> None:
    assert check_files(parsed(complete_tree(tmp_path))) == []


@pytest.mark.parametrize(
    ("rule", "key", "filename", "code"),
    [
        ("V-20", "llm.prompts.analyze", "analyze.txt", ErrorCode.CONFIG_INVALID_VALUE),
        ("V-20", "llm.prompts.repair", "repair.txt", ErrorCode.CONFIG_INVALID_VALUE),
        ("V-23", "transcription.model", "whisper.bin", ErrorCode.WHISPER_MODEL_MISSING),
        ("V-24", "transcription.executable", "whisper-cli", ErrorCode.WHISPER_EXEC_MISSING),
        ("V-25", "transcription.vad.model", "vad.bin", ErrorCode.WHISPER_MODEL_MISSING),
    ],
)
def test_file_rules(tmp_path: Path, rule: str, key: str, filename: str, code: ErrorCode) -> None:
    document = complete_tree(tmp_path)
    (tmp_path / filename).unlink()
    violations = check_files(parsed(document))
    assert [(v.rule, v.key, v.code) for v in violations] == [(rule, key, code)]


def test_v24_requires_the_executable_bit(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root では os.access(X_OK) が常に真になるため、この検査は成立しない")
    document = complete_tree(tmp_path)
    (tmp_path / "whisper-cli").chmod(0o644)
    violations = check_files(parsed(document))
    assert [(v.rule, v.code) for v in violations] == [("V-24", ErrorCode.WHISPER_EXEC_MISSING)]
    assert "実行できません" in violations[0].message


def test_v25_is_skipped_when_vad_is_disabled(tmp_path: Path) -> None:
    document = complete_tree(tmp_path)
    (tmp_path / "vad.bin").unlink()
    document = merge(document, {"transcription": {"vad": {"enabled": False}}})
    assert check_files(parsed(document)) == []


# --- 削除ロック（V-26 / V-30） -------------------------------------------


def enabled(tmp_path: Path) -> Config:
    return parsed(merge(complete_tree(tmp_path), {"cleanup": {"delete_source_audio": True}}))


def test_no_notice_when_deletion_is_disabled(tmp_path: Path) -> None:
    cfg = parsed(complete_tree(tmp_path))
    state = tmp_path / "state"
    assert startup_notices(cfg, state_root=state, now=NOW) == []
    assert check_locks(cfg, state_root=state, now=NOW) == []


def test_v26_notice_when_enabled(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_heartbeat(state, updated_at=NOW.isoformat(), delete_source_audio=True)
    notices = startup_notices(enabled(tmp_path), state_root=state, now=NOW)
    assert [n.rule for n in notices] == ["V-26"]


def test_v30_confirmed_mismatch_is_a_violation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_heartbeat(state, updated_at=NOW.isoformat(), delete_source_audio=False)
    violations = check_locks(enabled(tmp_path), state_root=state, now=NOW)
    assert [(v.rule, v.code, v.key) for v in violations] == [
        ("V-30", ErrorCode.CONFIG_LOCK_MISMATCH, "cleanup.delete_source_audio")
    ]


def test_v30_agrees(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_heartbeat(state, updated_at=NOW.isoformat(), delete_source_audio=True)
    assert check_locks(enabled(tmp_path), state_root=state, now=NOW) == []


@pytest.mark.parametrize("case", ["missing", "stale", "field_absent", "broken"])
def test_v30_unknown_warns_but_does_not_block(tmp_path: Path, case: str) -> None:
    """不明は起動を止めない。**Helper 未実装の間にクラッシュループさせない**（§7.3）。"""
    state = tmp_path / "state"
    if case == "stale":
        old = NOW - timedelta(seconds=3600)
        write_heartbeat(state, updated_at=old.isoformat(), delete_source_audio=True)
    elif case == "field_absent":
        write_heartbeat(state, updated_at=NOW.isoformat())
    elif case == "broken":
        state.mkdir(parents=True)
        (state / "heartbeat.json").write_text("{ not json", encoding="utf-8")

    cfg = enabled(tmp_path)
    assert check_locks(cfg, state_root=state, now=NOW) == []
    assert [n.rule for n in startup_notices(cfg, state_root=state, now=NOW)] == ["V-26", "V-30"]


# --- 読み込み API --------------------------------------------------------


def test_load_config_succeeds_on_complete_tree(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(complete_tree(tmp_path)), encoding="utf-8")
    cfg = load_config(path, state_root=tmp_path / "state", now=NOW)
    assert cfg.timezone == "Asia/Tokyo"


def test_load_config_raises_with_all_violations(tmp_path: Path) -> None:
    document = merge(example_document(), {"audio": {"target_sample_rate": 44100}})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, state_root=tmp_path / "state", now=NOW)
    assert excinfo.value.path == path
    assert [v.rule for v in excinfo.value.violations] == ["V-3"]


def test_load_config_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigUnreadable) as excinfo:
        load_config(tmp_path / "nope.yaml")
    assert "設定ファイルがありません" in excinfo.value.violations[0].message


def test_load_config_reports_broken_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("a: [1,\n", encoding="utf-8")
    with pytest.raises(ConfigUnreadable) as excinfo:
        load_config(path)
    assert "YAML" in excinfo.value.violations[0].message


def test_load_config_reports_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(ConfigUnreadable):
        load_config(path)


def test_check_filesystem_can_be_skipped(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(path, state_root=tmp_path / "state", check_filesystem=False)
    assert cfg.transcription.language == "ja"


# --- その他 --------------------------------------------------------------


def test_key_count_counts_leaves() -> None:
    document = example_document()

    def leaves(node: Any) -> int:
        if isinstance(node, dict):
            return sum(leaves(v) for v in node.values())
        return 1

    assert key_count(document) == leaves(document)
    assert key_count(document) == 110


def test_config_is_frozen() -> None:
    cfg = parsed(example_document())
    with pytest.raises(ValidationError):
        cfg.timezone = "UTC"


def test_tz_property() -> None:
    assert parsed(example_document()).tz == ZoneInfo("Asia/Tokyo")


def test_config_path_uses_the_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(elsewhere))
    assert config_path() == elsewhere
    monkeypatch.delenv("VOICEDOCK_CONFIG")
    assert config_path() == config_module.DEFAULT_CONFIG_PATH


def test_no_env_override_mechanism() -> None:
    """§7 の「環境変数による個別キーの上書き機構は提供しない」をコードで固定する。

    `config.py` が触る環境変数は `VOICEDOCK_CONFIG` の 1 つだけでなければならない。
    キーごとの env 上書きが入ると、設定の出所が二重になって追えなくなる。
    """
    source = (REPO_ROOT / "src" / "voicedock" / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv")
    ]
    assert len(reads) == 1, f"os.environ / os.getenv の参照は 1 箇所だけにする: {len(reads)}"
    assert config_module.CONFIG_PATH_ENV == "VOICEDOCK_CONFIG"


def test_logger_for_uses_the_config(capsys: pytest.CaptureFixture[str]) -> None:
    """format / level / timezone が設定どおりにロガーへ渡ること。"""
    document = merge(
        example_document(),
        {"timezone": "UTC", "logging": {"format": "json", "level": "DEBUG"}},
    )
    logger_for(parsed(document)).debug("service_started", version="0.1.0")
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["level"] == "DEBUG"
    assert payload["event"] == "service_started"
    assert payload["ts"].endswith("+00:00")


def test_violation_render() -> None:
    violation = Violation("V-3", ErrorCode.CONFIG_INVALID_VALUE, "audio.x", "だめ")
    assert violation.render() == "V-3  CONFIG_INVALID_VALUE  audio.x: だめ"
