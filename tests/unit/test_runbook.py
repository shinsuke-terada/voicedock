"""実機手順書が実在するコマンドだけで書かれていることを固定する（#93）。

**手順が腐ったのは、正本が GitHub issue の本文にあったからである。**issue は git 管理外
なので、v5.0 で `scan` / `retry` / `pending` / `cleanup` を削除しても
**issue の動作確認手順は何も落ちずに残った。**実機を繋いだ当日、最初の 1 行で止まる。

正本を `docs/E2E.md` に移し、ここで検査する。

**`docs/SPEC.md` は対象外である。**§17.1 の「v5.0 で削除した 6 本」の表と付録の変更履歴が
**旧名を意図的に載せている。**SPEC と CLI の一致は `tests/unit/test_cli.py` が
`spec_subcommands()` で別に固定している。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_e2e_ids, spec_removed_subcommands, spec_subcommands
from voicedock.cli import SUBCOMMANDS

E2E_PATH = REPO_ROOT / "docs" / "E2E.md"
POC_PATH = REPO_ROOT / "docs" / "POC.md"

# `voicedock <word>` の形。
#
# **先読みで捕まえる（`(?=...)`）。**`docker compose exec voicedock voicedock status` は
# `voicedock` が 2 つ並ぶので、語を消費して照合すると 1 つめの `voicedock voicedock` だけが
# 当たり、**内側の `voicedock status` は走査位置の後ろに置き去りになる。**
# `docs/E2E.md` の手順はほぼ全部この形なので、**消費する正規表現だと検査が丸ごと空振りする。**
INVOCATION = re.compile(r"\bvoicedock[ \t]+(?=([a-z][a-z-]*)\b)")

# サービス名として現れる語。`docker compose exec voicedock <cmd>` / `logs voicedock` など
NOT_A_SUBCOMMAND = frozenset({"voicedock", "python", "sqlite3", "ls", "env", "sh", "bash"})


def runbook_files() -> list[Path]:
    """検査の対象。**実機で人が打つものすべて。**"""
    found = [E2E_PATH, POC_PATH, REPO_ROOT / "README.md"]
    found += sorted((REPO_ROOT / "scripts").glob("*.sh"))
    found += [p for p in sorted((REPO_ROOT / "helper").iterdir()) if p.is_file()]
    return [p for p in found if p.is_file()]


def invocations_in(text: str) -> set[str]:
    """`voicedock <サブコマンド>` として現れる語を返す。"""
    return {name for name in INVOCATION.findall(text) if name not in NOT_A_SUBCOMMAND}


# --- 陽性対照（**検査が実際に効くこと**） --------------------------------


def test_the_extraction_finds_a_dead_command() -> None:
    """**先に検査自体を確かめる。**拾えない正規表現では何も守れない。"""
    assert invocations_in("docker compose exec voicedock voicedock scan") == {"scan"}
    assert invocations_in("$ voicedock retry 1 --session") == {"retry"}


def test_the_extraction_ignores_the_service_name() -> None:
    """陰性対照。`docker compose logs voicedock` の後ろは引数であってサブコマンドではない。"""
    assert invocations_in("docker compose logs -f voicedock") == set()
    assert invocations_in("docker compose exec voicedock voicedock status") == {"status"}


# --- 本体 ---------------------------------------------------------------


@pytest.mark.parametrize("path", runbook_files(), ids=lambda p: p.name)
def test_every_invocation_is_a_real_subcommand(path: Path) -> None:
    """**手順に出てくる `voicedock <sub>` が実在すること。**

    v5.0 で削除した 6 本のどれかが残っていれば、**実機当日その行で止まる。**
    """
    found = invocations_in(path.read_text(encoding="utf-8"))
    unknown = sorted(found - set(SUBCOMMANDS))
    assert unknown == [], (
        f"{path.relative_to(REPO_ROOT)}: 実在しないサブコマンド {unknown}。"
        f"実在するのは {sorted(SUBCOMMANDS)} だけである（§17.1）"
    )


@pytest.mark.parametrize("name", spec_removed_subcommands())
def test_no_runbook_names_a_removed_subcommand(name: str) -> None:
    """**v5.0 で削除した 6 本を `voicedock <name>` の形で書かないこと**（§17.1）。

    説明として名前に触れるのは構わない（`docs/E2E.md` の対応表）。
    **打てる形で書いてはならない。**
    """
    for path in runbook_files():
        found = invocations_in(path.read_text(encoding="utf-8"))
        assert name not in found, (
            f"{path.relative_to(REPO_ROOT)} が `voicedock {name}` を書いている"
        )


def test_the_cli_and_the_spec_still_agree() -> None:
    """上の検査の土台。**`SUBCOMMANDS` が §17.1 とずれていたら意味が無い。**"""
    assert list(SUBCOMMANDS) == spec_subcommands()


# --- `docs/E2E.md` が §20.3 と 1 対 1 ------------------------------------


def e2e_rows() -> list[str]:
    body = E2E_PATH.read_text(encoding="utf-8").split("## 2. 判定表", 1)[1].split("## 3.", 1)[0]
    return re.findall(r"^\| (E2E-\d+) \|", body, re.M)


def test_the_runbook_covers_every_scenario() -> None:
    """**§20.3 と 1 対 1、順序も同じ。**SPEC に 1 行足したらここで落ちる。

    **件数のずれが最も危ない**（#55 の振り返り）。シナリオを足して手順を書き忘れても、
    **実機当日まで誰も気づかない。**
    """
    assert e2e_rows() == spec_e2e_ids()


@pytest.mark.parametrize("scenario", spec_e2e_ids())
def test_every_scenario_has_a_verdict(scenario: str) -> None:
    """**判定欄を空にしない。**空欄は「まだ」と「書いたつもり」を区別できない
    （`test_coverage_doc.py` と同じ理由）。
    """
    body = E2E_PATH.read_text(encoding="utf-8")
    row = re.search(rf"^\| {re.escape(scenario)} \| .+? \| (.+?) \|", body, re.M)
    assert row is not None, f"{scenario} の行が判定表に無い"
    verdict = row.group(1).strip()
    assert verdict.startswith(("✅", "✗", "⬜", "—")), f"{scenario} の判定が空か散文（{verdict!r}）"


@pytest.mark.parametrize("scenario", spec_e2e_ids())
def test_every_scenario_has_a_procedure(scenario: str) -> None:
    """**シナリオごとに手順の節があること。**表だけでは当日に手が動かない。"""
    body = E2E_PATH.read_text(encoding="utf-8")
    assert re.search(rf"^### 3\.\d+ {re.escape(scenario)} — ", body, re.M), (
        f"{scenario} の手順の節が無い"
    )


# --- 手順が呼ぶプログラムがイメージに在ること ----------------------------

# `docker compose exec voicedock <プログラム>` の <プログラム>。
# **`docker` の引数（`-T` など）は拾わない**ので `voicedock` の直後だけを見る
EXEC_PROGRAM = re.compile(r"docker compose exec (?:-\S+ )*voicedock ([a-z][\w.-]*)")


def test_the_extraction_finds_the_program() -> None:
    """陽性対照。**先に検査自体を確かめる。**"""
    assert EXEC_PROGRAM.findall("docker compose exec voicedock python -m sqlite3 /data/x.db") == [
        "python"
    ]
    assert EXEC_PROGRAM.findall("docker compose exec voicedock voicedock status") == ["voicedock"]


@pytest.mark.parametrize(
    "path",
    [E2E_PATH, POC_PATH, REPO_ROOT / "README.md", REPO_ROOT / "docs" / "SPEC.md"],
    ids=lambda p: p.name,
)
def test_every_documented_program_exists_in_the_image(path: Path) -> None:
    """**手順が呼ぶプログラムがイメージに在ること。**

    §17.1 は `history` / `show` を削った代替として `events` を SQL で読む手順を載せている。
    **v5.13 まで `sqlite3 -box` と書いてあり、そのとおり打つと
    `executable file not found` になった**（2026-09-14 に実機で踏んだ）。
    runtime イメージに `sqlite3` CLI は入っていない。

    **`dev` は `FROM runtime` なので、ここで見つからないものは本番にも無い。**
    """
    if not path.is_file():
        pytest.skip(f"{path.name} がマウントされていない")
    programs = sorted(set(EXEC_PROGRAM.findall(path.read_text(encoding="utf-8"))))
    missing = [name for name in programs if shutil.which(name) is None]
    assert missing == [], (
        f"{path.relative_to(REPO_ROOT)}: イメージに無いプログラムを手順が呼んでいる {missing}"
    )


def test_the_check_would_catch_a_missing_program() -> None:
    """陽性対照。**`sqlite3` CLI が無いことを確かめる** — 在るなら検査が意味を失う。"""
    assert shutil.which("sqlite3") is None, (
        "sqlite3 CLI がイメージに入った。手順を戻してよいか §18.3 と照らして判断すること"
    )
    assert shutil.which("python") is not None


# --- 手順が参照するスクリプトが実在すること ------------------------------


@pytest.mark.parametrize("path", [E2E_PATH, POC_PATH], ids=lambda p: p.name)
def test_referenced_scripts_exist(path: Path) -> None:
    """`./scripts/...` / `./helper/...` を書いたのに置き忘れていないこと。"""
    text = path.read_text(encoding="utf-8")
    for relative in sorted(set(re.findall(r"\./((?:scripts|helper)/[\w.-]+)", text))):
        assert (REPO_ROOT / relative).is_file(), f"{path.name} が参照する {relative} が無い"
