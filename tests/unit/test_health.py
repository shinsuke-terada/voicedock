"""`voicedock.health` が SPEC §19.1（H-1〜H-9）と一致することを固定する。

**`health` は 120 秒ごとに走る**（`compose.yaml` の healthcheck。§18.2）。
したがって次の 2 つを**テストで縛る**。

1. **副作用を持たない** — Vault に一時ファイルを撒き続けてはならない
2. **LLM へ実リクエストしない** — 120 秒ごとにモデルを起こしてはならない

どちらも「うっかり `doctor` の実装を流用する」と破れるので、静的にも検査する。
"""

from __future__ import annotations

import ast
import io
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import REPO_ROOT, example_document, merge, write_heartbeat
from tests.spec_sync import spec_text
from voicedock import db, health, paths
from voicedock.config import Config, parse_config
from voicedock.errors import EXIT_OK, EXIT_UNHEALTHY
from voicedock.health import Context, Result, evaluate

JST = ZoneInfo("Asia/Tokyo")

HEALTH_PY = REPO_ROOT / "src" / "voicedock" / "health.py"


# --- SPEC 整合 -----------------------------------------------------------


def spec_health_checks() -> list[str]:
    """§19.1 の表から `H-n` を出現順に返す。"""
    section = re.search(
        r"^#+ 19\.1[^\n]*\n(.*?)(?=^#{1,6} (?:\d|付録)|\Z)", spec_text(), re.S | re.M
    )
    assert section is not None
    found = re.findall(r"^\| \*{0,2}(H-\d+)\*{0,2} \|", section.group(1), re.M)
    assert found, "SPEC §19.1 の表を読み取れませんでした"
    return found


def test_checks_match_spec(
    tmp_path: Path, make_config: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**実装する検査が §19.1 の H-* と 1 対 1 であること。**

    SPEC に H-10 を足して実装を忘れる事故、逆に実装だけ増える事故が落ちる。
    v5.0 の時点で SPEC は H-9 まであるのに、issue #12 のタイトルは
    「H-1〜H-7」で止まっていた（**古いのは本文だけでなく件数もだった**）。
    """
    ctx = _context(tmp_path, monkeypatch)
    assert [result.check for result in evaluate(ctx)] == spec_health_checks()


def test_h1_is_not_skipped() -> None:
    """H-1 を結果に出すこと。

    「Python プロセスが生存している」はこのプログラムが動いていることで満たされるが、
    **番号を飛ばすと読む人が仕様を疑う。**
    """
    assert health.CHECKS[0] is health.check_process


# --- 副作用を持たないこと（§19.1 の要件） --------------------------------


def test_creates_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**実行前後でファイルが 1 つも増えないこと。**

    120 秒ごとに走るので、一時ファイルを作ると **Vault に撒き続ける。**
    実書き込みの検証は `doctor` D-13 / D-3 が担う（人が呼ぶときだけ）。
    """
    ctx = _context(tmp_path, monkeypatch)
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    evaluate(ctx)
    after = sorted(str(p) for p in tmp_path.rglob("*"))
    assert after == before, f"health が副作用を持っている: {set(after) - set(before)}"


def test_does_not_import_http_clients() -> None:
    """**LLM へ実リクエストしないこと**を静的に見る（§19.1）。

    `health` が HTTP を投げると **120 秒ごとにモデルを起こす。**疎通確認は
    `doctor` D-12 の仕事である。
    """
    tree = ast.parse(HEALTH_PY.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"http", "urllib", "httpx", "requests", "socket"}
    assert imported & forbidden == set(), f"health が HTTP を持ち込んでいる: {imported & forbidden}"


def test_does_not_write_or_delete() -> None:
    """書き込み・削除の呼び出しが無いこと（副作用ゼロの静的な裏付け）。"""
    body = HEALTH_PY.read_text(encoding="utf-8")
    tree = ast.parse(body)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        assert name not in {
            "write_text",
            "write_bytes",
            "mkdir",
            "touch",
            "unlink",
            "rmtree",
            "replace",
        }, f"health が副作用を持つ呼び出しをしている: {name}:{node.lineno}"


def test_migrate_is_false() -> None:
    """H-2 が `migrate=False` で開くこと。

    healthcheck がスキーマを作ってしまうと**「DB が無い」ことを報告できなくなる**
    （`doctor` D-2 と同じ理由）。
    """
    body = HEALTH_PY.read_text(encoding="utf-8")
    assert "migrate=False" in body


# --- 各検査の合否 -------------------------------------------------------


def _context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stale: bool = False, **overrides: object
) -> Context:
    """全検査が通る状態を作る。個別のテストが 1 つだけ壊す。"""
    for name in ("data", "obsidian", "inbox", "queue", "state"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "obsidian" / ".obsidian").mkdir(exist_ok=True)  # §13.6 の目印
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(paths, "VAULT_ROOT", tmp_path / "obsidian")
    monkeypatch.setattr(paths, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(paths, "QUEUE_ROOT", tmp_path / "queue")

    whisper = tmp_path / "whisper-cli"
    whisper.write_text("#!/bin/sh\n", encoding="utf-8")
    whisper.chmod(0o755)
    model = tmp_path / "model.bin"
    model.write_bytes(b"x")

    now = datetime(2026, 9, 13, 9, 0, 0, tzinfo=JST)
    write_heartbeat(
        tmp_path / "state",
        updated_at=(now - timedelta(seconds=3600 if stale else 10)).isoformat(),
        **{k: v for k, v in overrides.items() if k in _HEARTBEAT_KEYS},
    )
    # **Compose 5.5.1 が実際に注入する形**（docs/POC.md §11.1.1）
    monkeypatch.setenv("VOICEDOCK_LLM_URL", "http://model-runner.docker.internal/v1/")
    monkeypatch.setenv("VOICEDOCK_LLM_MODEL", "qwen3-30b-a3b")

    cfg = _config(tmp_path, whisper, model)
    return Context(cfg=cfg, state_root=tmp_path / "state", now=now)


_HEARTBEAT_KEYS = frozenset({"config_error", "helper_version", "mount_readonly"})


def _config(tmp_path: Path, whisper: Path, model: Path) -> Config:
    """`make_config` fixture を使わず、必要な枝だけを持つ最小の設定を組む。

    `health` が読むのは `database` / `transcription` / `llm` / `import_` / `tz` だけである。
    """
    document = merge(
        example_document(),
        {
            "database": {"path": str(tmp_path / "data" / "voicedock.db")},
            "transcription": {"executable": str(whisper), "model": str(model)},
        },
    )
    # プロンプトと VAD モデルの実在検査（V-20 / V-25）は health の対象外だが、
    # parse_config は構造だけを見るので実ファイルは要らない
    cfg, violations = parse_config(document)
    assert cfg is not None, violations
    return cfg


def results_by_check(ctx: Context) -> dict[str, Result]:
    return {result.check: result for result in evaluate(ctx)}


def test_all_checks_pass_in_a_healthy_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path, monkeypatch)
    db.connect(ctx.cfg.database.path, tz=ctx.cfg.tz).close()
    results = evaluate(ctx)
    failed = [r.render() for r in results if not r.ok]
    assert failed == [], failed


def test_h2_fails_when_the_database_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB が無ければ unhealthy（H-2）。**接続を試みる前に落とす。**

    `sqlite3.connect()` は `migrate=False` でも**空のファイルを作ってしまう。**
    120 秒ごとに走る検査が `/data` にファイルを置くのは副作用であり、
    かつ「DB が無い」ことを報告できなくなる。
    """
    ctx = _context(tmp_path, monkeypatch)
    assert not ctx.cfg.database.path.exists()
    result = results_by_check(ctx)["H-2"]
    assert not result.ok
    assert "missing" in result.detail
    assert not ctx.cfg.database.path.exists(), "H-2 が空の DB を作ってしまった"


def test_h2_fails_when_the_migration_is_not_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ファイルはあるが `schema_version` が無い場合も unhealthy（H-2）。"""
    ctx = _context(tmp_path, monkeypatch)
    ctx.cfg.database.path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(ctx.cfg.database.path).close()
    result = results_by_check(ctx)["H-2"]
    assert not result.ok
    assert "schema" in result.detail or "unreadable" in result.detail


@pytest.mark.parametrize(
    ("check", "attribute", "name"),
    [("H-3", "DATA_ROOT", "data"), ("H-4", "VAULT_ROOT", "obsidian")],
)
def test_unwritable_root_is_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: str, attribute: str, name: str
) -> None:
    ctx = _context(tmp_path, monkeypatch)
    target = tmp_path / name
    target.chmod(0o500)
    try:
        result = results_by_check(ctx)[check]
        assert not result.ok, f"{check} が書き込み不可を検出していない"
        assert "not writable" in result.detail
    finally:
        target.chmod(0o755)


def test_a_writable_directory_without_the_marker_is_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**H-4 は「書ける」だけでは真にしない**（§13.6 / #134）。

    Docker は bind mount の source が無ければ空ディレクトリとして作るので、
    Vault が消えても**書ける場所には見え続ける。**`docker ps` の STATUS 列で
    気づけるようにするのがこの検査の狙いである（H-8 と同じ考え方）。
    """
    ctx = _context(tmp_path, monkeypatch)
    assert results_by_check(ctx)["H-4"].ok, "目印が在るのに unhealthy になっている"

    (tmp_path / "obsidian" / ".obsidian").rmdir()

    result = results_by_check(ctx)["H-4"]
    assert not result.ok, "目印が無いのに healthy のまま（幻の Vault へ書き続ける）"
    assert "not a vault" in result.detail, result.detail
    assert ".obsidian" in result.detail, "何が足りないのかが読めない"


@pytest.mark.parametrize(
    ("check", "attribute"), [("H-3", "DATA_ROOT"), ("H-4", "VAULT_ROOT"), ("H-9", "INBOX_ROOT")]
)
def test_missing_root_is_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: str, attribute: str
) -> None:
    ctx = _context(tmp_path, monkeypatch)
    monkeypatch.setattr(paths, attribute, tmp_path / "gone")
    result = results_by_check(ctx)[check]
    assert not result.ok
    assert "not a directory" in result.detail


def test_h5_fails_when_whisper_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path, monkeypatch)
    ctx.cfg.transcription.executable.unlink()
    result = results_by_check(ctx)["H-5"]
    assert not result.ok
    assert "missing" in result.detail


def test_h5_fails_when_whisper_is_not_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path, monkeypatch)
    ctx.cfg.transcription.executable.chmod(0o644)
    result = results_by_check(ctx)["H-5"]
    assert not result.ok
    assert "not executable" in result.detail


def test_h6_fails_when_the_model_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**モデル未取得は unhealthy**（§18.6 の `fetch-models.sh` 未実行）。

    文字起こしが 1 本も通らないのに healthy に見えてはならない。
    """
    ctx = _context(tmp_path, monkeypatch)
    ctx.cfg.transcription.model.unlink()
    result = results_by_check(ctx)["H-6"]
    assert not result.ok
    assert "fetch-models" in result.detail


@pytest.mark.parametrize("name", ["VOICEDOCK_LLM_URL", "VOICEDOCK_LLM_MODEL"])
def test_h7_fails_when_the_env_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    ctx = _context(tmp_path, monkeypatch)
    monkeypatch.delenv(name)
    result = results_by_check(ctx)["H-7"]
    assert not result.ok
    assert name in result.detail


def test_h7_fails_on_an_empty_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空文字も未設定と同じに扱うこと（`export X=` は設定したつもりになりやすい）。"""
    ctx = _context(tmp_path, monkeypatch)
    monkeypatch.setenv("VOICEDOCK_LLM_URL", "")
    assert not results_by_check(ctx)["H-7"].ok


def test_h8_fails_when_the_helper_is_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Helper の停止は unhealthy**（§19.1 / §22 R-23）。

    Helper が止まると録音は 1 本も取り込まれないのに、コンテナは正常に見え続ける。
    **症状が「何も起きない」なので、検出しなければ誰も気づかない。**
    """
    ctx = _context(tmp_path, monkeypatch, stale=True)
    result = results_by_check(ctx)["H-8"]
    assert not result.ok
    assert "stale" in result.detail


def test_h8_fails_without_a_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path, monkeypatch)
    (tmp_path / "state" / "heartbeat.json").unlink()
    result = results_by_check(ctx)["H-8"]
    assert not result.ok
    assert "missing" in result.detail


def test_h8_fails_on_a_helper_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Helper の設定エラーも unhealthy にする。**

    Helper は動いているが取り込みを行わない状態であり（§7.4 の起動時検証に違反）、
    H-8 の目的（沈黙の検出）はこちらも捕まえるべきである。
    """
    ctx = _context(tmp_path, monkeypatch, config_error="5: MOUNT_MODE must be ro or rw: nope")
    result = results_by_check(ctx)["H-8"]
    assert not result.ok
    assert "config_error" in result.detail


def test_h9_fails_when_the_queue_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path, monkeypatch)
    (tmp_path / "queue").chmod(0o500)
    try:
        result = results_by_check(ctx)["H-9"]
        assert not result.ok
    finally:
        (tmp_path / "queue").chmod(0o755)


# --- デバイス未接続は healthy（§19.1） -----------------------------------


def test_a_disconnected_device_stays_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**DJI Mic 未接続を unhealthy の条件にしてはならない**（§19.1）。

    通常はデバイスが接続されていない状態が正常である。**inbox が空でも healthy。**
    """
    ctx = _context(tmp_path, monkeypatch)
    db.connect(ctx.cfg.database.path, tz=ctx.cfg.tz).close()
    assert list((tmp_path / "inbox").iterdir()) == []
    results = evaluate(ctx)
    assert all(result.ok for result in results), [r.render() for r in results if not r.ok]


def test_no_check_looks_at_the_inventory() -> None:
    """`inventory.json`（デバイスの接続状況）を読まないこと。

    読むと「デバイス未接続」を unhealthy の材料にする経路ができる。
    **Helper の生存（heartbeat）とデバイスの接続（inventory）は別の話である。**
    """
    body = HEALTH_PY.read_text(encoding="utf-8")
    assert "inventory" not in body
    assert "read_inventory" not in body


# --- 全体の実行と終了コード（§17.3） ------------------------------------


def test_evaluate_runs_every_check_even_after_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 つ失敗しても残りを実行すること。

    最初の失敗で打ち切ると `docker logs` に出る情報が 1 行だけになる。
    """
    ctx = _context(tmp_path, monkeypatch)
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path / "gone")
    results = evaluate(ctx)
    assert len(results) == len(spec_health_checks())
    assert not results[2].ok


def test_write_reports_the_failing_checks() -> None:
    out = io.StringIO()
    health._write(
        out, [Result("H-1", True, "alive"), Result("H-8", False, "helper heartbeat is stale")]
    )
    text = out.getvalue()
    assert "H-1 ok alive" in text
    assert "H-8 FAIL" in text
    assert "unhealthy: H-8" in text


def test_write_reports_healthy() -> None:
    out = io.StringIO()
    health._write(out, [Result("H-1", True, "alive")])
    assert "healthy: 1 checks passed" in out.getvalue()


def test_unreadable_config_is_unhealthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """設定が読めない場合も**終了コード 3**（§17.3）。

    §17.3 の 2（設定エラー）は起動時の中止に使う番号である。healthcheck の文脈で
    Docker が見るのは「0 か否か」だけなので、**3 に統一して「不健康」として扱う。**
    """
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(tmp_path / "nope.yaml"))
    out = io.StringIO()
    assert health.run(state_root=tmp_path / "state", out=out) == EXIT_UNHEALTHY
    assert "config unreadable" in out.getvalue()


def test_exit_code_is_three_when_unhealthy() -> None:
    """§17.3 の 3（unhealthy）を使うこと。"""
    assert EXIT_UNHEALTHY == 3
    assert EXIT_OK == 0


def test_environ_is_the_only_env_read() -> None:
    """`os.environ` 以外から env を読まないこと（§7 の系統境界）。

    `health` が `.env` や `helper.conf` を直読みすると、設定の出所が二重になる。
    """
    body = HEALTH_PY.read_text(encoding="utf-8")
    # 文字列リテラルとして `.env` / `helper.conf` を持たないこと
    # （`os.environ` が `.env` を部分一致で含むので、素朴な `in` では誤検出する）
    literals = {
        node.value
        for node in ast.walk(ast.parse(body))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for forbidden in (".env", "helper.conf"):
        assert not any(forbidden in text for text in literals), (
            f"health が {forbidden} を直読みしている（§7 の系統境界）"
        )
