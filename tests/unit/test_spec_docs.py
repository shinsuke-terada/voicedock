"""本書（`docs/SPEC.md`）自身の整合を固定する（SPEC §20.1 の「文書の整合」）。

**文書と実装の食い違いは、気づいた時に直すだけでは次に同じ場所で起きる。**
v5.3 の O-5（本文が指示するイベント名が §16.4 に在ること）と同じ処方で、
**機械が見る形にして初めて止まる。**

ここで見るのは「本書が実装を知っているか」だけである。実装の正しさは各モジュールの
テストが見る。
"""

from __future__ import annotations

import re

import pytest

from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_text, spec_tree_modules

SOURCE_ROOT = REPO_ROOT / "src" / "voicedock"

PLANNED: frozenset[str] = frozenset()
"""木に載っているが**まだ実装していない**モジュール。

**木から消さない** — 消すと「どこに何を置く予定か」が本書から失われる。
`pipeline.py` は #19、`daily.py` は #28、`cleaner.py` は #36 で実装したので外した。
**いまは全部実装済みである。**
"""


def implemented() -> set[str]:
    return {path.stem for path in SOURCE_ROOT.glob("*.py")} - {"__init__"}


def test_every_module_appears_in_the_tree() -> None:
    """**§6 の木が `src/voicedock/*.py` を全件列挙していること。**

    木は「どこに何があるか」の唯一の索引である。モジュールを足して木を直さずに通ると、
    **索引が静かに古くなり、読み手が存在しないファイルを探す。**v5.3 の時点で
    6 件（`heartbeat` / `discover` / `raw` / `wiki` / `status` / `health`）が欠けていた。
    """
    missing = sorted(implemented() - spec_tree_modules())
    assert missing == [], (
        f"§6 の木に載っていないモジュールがあります: {missing}。"
        "木を直すか、そのモジュールが本当に要るのかを考えること"
    )


def test_the_tree_only_promises_planned_modules() -> None:
    """木に在って未実装のものが `PLANNED` と一致すること。

    **逆向きの検査である。**実装したのに `PLANNED` から外し忘れると、
    「まだ無い」と書いてある行が残る。チケットを閉じるときにここが落ちる。
    """
    listed = spec_tree_modules() - {"__init__"}
    unbuilt = listed - implemented()
    assert unbuilt == PLANNED, (
        f"木に在って未実装のものが {sorted(unbuilt)} で、PLANNED は {sorted(PLANNED)} です。"
        "実装したら PLANNED から外すこと"
    )


def test_the_tree_lists_the_current_subcommands() -> None:
    """木の `cli.py` の説明が §17.1 のサブコマンドと一致すること。

    **v5.3 まで v5.0 で削除した 9 本**（`history` / `show` / `retry` / `pending` /
    `cleanup` / `scan`）が並んでいた。**存在しないコマンドを探す人が出る。**
    """
    text = spec_text()
    start = text.index("## 6. ")
    block = text[start : text.index("## 7. ", start)]
    # **`main.py` の行にも `cli.py` が出る**（ディスパッチの所在を書いてあるため）。
    # 木の項目としての行だけを取る
    line = next(raw for raw in block.splitlines() if "── cli.py" in raw)

    from tests.spec_sync import spec_removed_subcommands, spec_subcommands

    for name in spec_subcommands():
        assert name in line, f"§17.1 の {name} が木の cli.py の説明に無い"
    for name in spec_removed_subcommands():
        assert name not in line, f"v5.0 で削除した {name} が木に残っている"


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [("notes", "レンダリング・WikiLink"), ("notes", "Raw / Daily レンダリング")],
)
def test_the_notes_description_no_longer_claims_rendering(module: str, forbidden: str) -> None:
    """`notes.py` の説明がレンダリングを持つと書いていないこと。

    #24 で `raw.py`、#29 で `wiki.py` が分かれた。**1 つのモジュールが全部持つと
    書いてあると、次の実装者がそこへ足す。**
    """
    text = spec_text()
    start = text.index("## 6. ")
    block = text[start : text.index("## 7. ", start)]
    lines = [raw for raw in block.splitlines() if f"{module}.py" in raw]
    assert lines
    index = block.splitlines().index(lines[0])
    window = "\n".join(block.splitlines()[index : index + 3])
    assert forbidden not in window


def test_the_version_history_is_consistent() -> None:
    """文書版・前身文書・最新の付録が同じ版を指していること。

    **版を上げて付録を足し忘れる**（あるいは逆）ことを防ぐ。付録は新しい順に並べる
    規約なので、**付録 A が「1 つ前の版 → 現在の版」**でなければならない。
    """
    text = spec_text()
    title = re.search(r"^# VoiceDock 詳細仕様書 v(\d+\.\d+)$", text, re.M)
    assert title is not None
    current = title.group(1)

    meta = re.search(r"^\| 文書版 \| \*\*v(\d+\.\d+)", text, re.M)
    assert meta is not None
    assert meta.group(1) == current, "表題と「文書版」の行が食い違っている"

    previous = re.search(r"^\| 前身文書 \| v(\d+\.\d+) /", text, re.M)
    assert previous is not None

    appendix = re.search(r"^## 付録 A\. v(\d+\.\d+) から v(\d+\.\d+) への主な変更点$", text, re.M)
    assert appendix is not None, "付録 A が『vX から vY への主な変更点』の形でない"
    assert appendix.group(2) == current, "付録 A の行き先が現在の版と違う"
    assert appendix.group(1) == previous.group(1), "付録 A の出発点が前身文書の先頭と違う"


def test_the_appendix_letters_are_sequential() -> None:
    """付録のレターが A から連番であること（**新しい順**の規約）。

    版を上げるたびに繰り下げるので、**1 つ飛ばすと以降の参照がずれる。**
    """
    letters = re.findall(r"^## 付録 ([A-Z])\. ", spec_text(), re.M)
    assert letters == [chr(ord("A") + n) for n in range(len(letters))], letters


def test_no_module_is_orphaned_from_the_source_tree() -> None:
    """`src/voicedock/` に `.py` 以外の想定外のものが無いこと（木との突き合わせの前提）。"""
    unexpected = sorted(
        path.name
        for path in SOURCE_ROOT.iterdir()
        if path.name not in {"__pycache__", "migrations", "py.typed"} and path.suffix != ".py"
    )
    assert unexpected == []
