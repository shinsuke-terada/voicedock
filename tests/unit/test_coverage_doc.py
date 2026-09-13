"""`docs/TEST_COVERAGE.md` が SPEC §20.1 と一致していることを固定する。

**件数のずれが最も危ない**（#55 の振り返り）。名前の変更は grep で見つかるが、
**§20.1 に 1 行足して実装を忘れても、いまは何も落ちない。**この表がその穴を塞ぐ。

**未カバーの項目は空欄にせず issue 番号を書く。**空欄を許すと「まだ書いていない」と
「書いたつもり」が区別できない。
"""

from __future__ import annotations

import re

import pytest

from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_unit_test_targets

COVERAGE_PATH = REPO_ROOT / "docs" / "TEST_COVERAGE.md"
TEST_PATTERN = re.compile(r"`(tests/[^`]+\.py)`")
ISSUE_PATTERN = re.compile(r"^#\d+$")

MAX_UNCOVERED = 2
"""未カバーの上限。**現時点は削除まわり（#36 / #37）の 2 件だけである。**

増やすときは「なぜテストを書かずに進めるか」を PR に書くこと。
"""


def coverage_rows() -> dict[str, str]:
    """`docs/TEST_COVERAGE.md` の §20.1 の表を `対象 → テスト` で返す。"""
    text = COVERAGE_PATH.read_text(encoding="utf-8")
    body = text.split("## §20.1 Unit Test", 1)[1].split("## §20.2", 1)[0]
    rows = re.findall(r"^\| (.+?) \| (.+?) \|$", body, re.M)
    return {
        target.strip(): tests.strip()
        for target, tests in rows
        if target.strip() not in {"§20.1 の対象", "---"}
    }


def test_the_table_matches_the_spec_row_for_row() -> None:
    """**§20.1 と 1 対 1、順序も同じ。**行を足したらここで落ちる。"""
    assert list(coverage_rows()) == spec_unit_test_targets()


@pytest.mark.parametrize("target", spec_unit_test_targets())
def test_every_entry_is_a_file_or_an_issue(target: str) -> None:
    """テスト列は**実在するファイル**か **`#NN`** のどちらかであること。

    散文（「あとで」「一部のみ」）を許すと、**読んだ人が確かめられない。**
    """
    value = coverage_rows()[target]
    if ISSUE_PATTERN.match(value):
        return
    found = TEST_PATTERN.findall(value)
    assert found, f"{target}: テストファイルも issue 番号も無い（{value!r}）"
    # **列の中身が「ファイルの列挙だけ」であること。**散文が混ざっていないか
    remainder = TEST_PATTERN.sub("", value).strip()
    assert remainder == "", f"{target}: 余分な記述がある（{remainder!r}）"
    for relative in found:
        assert (REPO_ROOT / relative).is_file(), f"{target}: {relative} が無い"


def test_the_uncovered_entries_stay_few() -> None:
    """**未カバーが増えたら気づく。**いまは削除まわりの 2 件だけである。"""
    uncovered = {
        target: value for target, value in coverage_rows().items() if ISSUE_PATTERN.match(value)
    }
    assert len(uncovered) <= MAX_UNCOVERED, uncovered


def test_the_integration_table_points_at_real_files() -> None:
    """§20.2 側の表も同じ規則で確かめる。"""
    text = COVERAGE_PATH.read_text(encoding="utf-8")
    body = text.split("## §20.2 Integration Test", 1)[1]
    found = TEST_PATTERN.findall(body)
    assert found, "§20.2 の表にテストファイルが 1 つも無い"
    for relative in found:
        assert (REPO_ROOT / relative).is_file(), relative
