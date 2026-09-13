"""`voicedock.paths` が SPEC §11.2 / §14.1.1 / §14.4 N-10・N-14 に従うことを固定する。

**symlink の実験は `tmp_path` の中だけで行う。**v4.0 でコンテナは `/Volumes` を
マウントしないため、`/Volumes/Macintosh HD`（ルートへの symlink）相当を自作して再現する。
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest
import yaml

from tests.spec_sync import spec_section_code
from voicedock import paths
from voicedock.paths import (
    DATA_ROOT,
    INBOX_ROOT,
    QUEUE_ROOT,
    VAULT_ROOT,
    DevicePath,
    InboxPath,
    StagingPath,
    is_safe_relpath,
    is_under,
    require_under,
    safe_unlink_inbox,
    safe_unlink_staging,
    safe_unlink_tmp,
)

REPO_ROOT = Path(__file__).parents[2]


# --- マウント点 ----------------------------------------------------------


def test_mount_roots_match_compose() -> None:
    """定数が `compose.yaml` のコンテナ側マウント先と一致すること（§18.2）。

    ここがずれると封じ込めが**常に偽**になり、削除が一切できなくなる（または逆に
    意図しない場所を許してしまう）。
    """
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    volumes = compose["services"]["voicedock"]["volumes"]
    # `${VOICEDOCK_HOME:?... is required}/inbox:/inbox:rw` のように、左辺の変数展開が
    # コロンを含む。素朴に split(":") すると壊れるので、先に ${...} を潰す
    targets = {re.sub(r"\$\{[^}]*\}", "VAR", str(entry)).split(":")[1] for entry in volumes}
    for root in (INBOX_ROOT, DATA_ROOT, VAULT_ROOT, QUEUE_ROOT):
        assert str(root) in targets, f"{root} が compose.yaml のマウント先に無い: {sorted(targets)}"


# --- is_under ------------------------------------------------------------


def test_is_under_accepts_a_child(tmp_path: Path) -> None:
    root = tmp_path / "device"
    (root / "a").mkdir(parents=True)
    assert is_under(root, root / "a" / "b.wav")


def test_is_under_rejects_the_root_itself(tmp_path: Path) -> None:
    """root そのものは配下ではない（root を消させない）。"""
    root = tmp_path / "device"
    root.mkdir()
    assert not is_under(root, root)


def test_is_under_rejects_a_sibling(tmp_path: Path) -> None:
    (tmp_path / "device").mkdir()
    (tmp_path / "other").mkdir()
    assert not is_under(tmp_path / "device", tmp_path / "other" / "x.wav")


def test_is_under_rejects_a_prefix_sibling(tmp_path: Path) -> None:
    """名前の前方一致で通してはならない（`/data` と `/data-old`）。"""
    (tmp_path / "data").mkdir()
    (tmp_path / "data-old").mkdir()
    assert not is_under(tmp_path / "data", tmp_path / "data-old" / "x.wav")


@pytest.mark.parametrize("case", ["relative_root", "relative_target"])
def test_is_under_rejects_relative_paths(tmp_path: Path, case: str) -> None:
    root = tmp_path / "device"
    root.mkdir()
    if case == "relative_root":
        assert not is_under(Path("device"), root / "x.wav")
    else:
        assert not is_under(root, Path("device/x.wav"))


def test_is_under_rejects_dotdot(tmp_path: Path) -> None:
    root = tmp_path / "device"
    root.mkdir()
    (tmp_path / "other").mkdir()
    assert not is_under(root, root / ".." / "other" / "x.wav")


def test_is_under_resolves_symlink_escape(tmp_path: Path) -> None:
    """**ルートへの symlink を経由したパスが封じ込めを擦り抜けないこと。**

    `/Volumes/Macintosh HD -> /` の再現（`docs/POC.md` §5.1、§22 R-19）。
    字句判定なら「配下」と判定されてしまうことを併記する。
    """
    root = tmp_path / "device"
    root.mkdir()
    (root / "escape").symlink_to("/")
    target = root / "escape" / "etc" / "passwd"

    assert root in target.parents, "字句判定では配下と見えてしまう（これが R-19）"
    assert not is_under(root, target), "realpath 解決後は配下ではない"


def test_is_under_resolves_symlink_to_a_sibling(tmp_path: Path) -> None:
    root = tmp_path / "device"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (root / "link").symlink_to(other)
    assert not is_under(root, root / "link" / "x.wav")


def test_is_under_allows_an_internal_symlink(tmp_path: Path) -> None:
    """root の内側へ向く symlink は通す。

    §14.1.1 の `resolved == target` は課さない（§11.2）。Vault 内に symlink を張って
    いる利用者の一時ファイルを片付けられなくなるため。
    """
    root = tmp_path / "vault"
    (root / "sub").mkdir(parents=True)
    (root / "link").symlink_to(root / "sub")
    assert is_under(root, root / "link" / ".x.md.tmp")


def test_is_under_works_for_a_missing_target(tmp_path: Path) -> None:
    """まだ存在しないパスでも判定できること（削除の「前」に呼ばれる）。"""
    root = tmp_path / "device"
    root.mkdir()
    assert is_under(root, root / "not-yet" / "x.wav")


def test_require_under(tmp_path: Path) -> None:
    root = tmp_path / "device"
    (root / "a").mkdir(parents=True)
    require_under(root, root / "a" / "b.wav")  # 例外が出なければ合格
    with pytest.raises(ValueError, match="配下ではありません"):
        require_under(root, tmp_path / "x.wav")


# --- is_safe_relpath -----------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("TX_MIC001_20260829_071201/TX01_MIC002_20260829_071204.wav", True),
        ("a.wav", True),
        ("/TX01/a.wav", False),  # 絶対パス（N-17）
        ("", False),  # 空
        ("./a.wav", True),  # PurePosixPath が '.' を落として正規化する（下のテスト参照）
        ("../a.wav", False),  # '..'（N-17）
        ("TX01/../a.wav", False),
        (".Trashes/a.wav", False),  # '.' 始まり（R-22）
        ("TX01/.fseventsd", False),
        ("TX01/a\nb.wav", False),  # 制御文字
        ("TX01/a\x7fb.wav", False),
    ],
)
def test_is_safe_relpath(rel: str, expected: bool) -> None:
    assert is_safe_relpath(PurePosixPath(rel)) is expected


def test_pathlib_normalizes_a_single_dot_but_not_dotdot() -> None:
    """`'.'` の検査が防御的なだけである理由を記録する。

    `PurePosixPath` は単独の `'.'` を正規化して落とすため、`is_safe_relpath` へ届かない。
    一方 **`'..'` は残る**（これが N-17 で実際に効く検査である）。
    """
    assert PurePosixPath("./a.wav").parts == ("a.wav",)
    assert PurePosixPath("../a.wav").parts == ("..", "a.wav")


# --- safe_unlink: 成功経路 -----------------------------------------------


def _patch_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """マウント点の定数を `tmp_path` 配下へ差し替える。

    定数は関数本体で参照しているので monkeypatch が効く。**本番のコードには上書きの
    入口を作らない**方針（設定で緩められる安全柵は柵ではない）のため、差し替えは
    テストのここだけで行う。
    """
    roots = {name: tmp_path / name for name in ("inbox", "data", "obsidian")}
    for path in roots.values():
        path.mkdir()
    monkeypatch.setattr(paths, "INBOX_ROOT", roots["inbox"])
    monkeypatch.setattr(paths, "DATA_ROOT", roots["data"])
    monkeypatch.setattr(paths, "VAULT_ROOT", roots["obsidian"])
    return roots


def test_safe_unlink_inbox_removes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _patch_roots(monkeypatch, tmp_path)
    target = roots["inbox"] / "a.wav"
    target.write_bytes(b"x")
    safe_unlink_inbox(InboxPath(target))
    assert not target.exists()


def test_safe_unlink_staging_removes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _patch_roots(monkeypatch, tmp_path)
    target = roots["data"] / "staging" / "a.wav"
    target.parent.mkdir()
    target.write_bytes(b"x")
    safe_unlink_staging(StagingPath(target))
    assert not target.exists()


@pytest.mark.parametrize("root_name", ["obsidian", "data"])
def test_safe_unlink_tmp_accepts_both_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_name: str
) -> None:
    roots = _patch_roots(monkeypatch, tmp_path)
    target = roots[root_name] / ".2026-08-29 Voice.md.tmp"
    target.write_text("x", encoding="utf-8")
    safe_unlink_tmp(target)
    assert not target.exists()


# --- safe_unlink: 拒否 ---------------------------------------------------


@pytest.mark.parametrize("which", ["inbox", "staging", "tmp"])
def test_safe_unlink_rejects_outside_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    _patch_roots(monkeypatch, tmp_path)
    outside = tmp_path / ".stray.md.tmp"
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="配下ではありません"):
        if which == "inbox":
            safe_unlink_inbox(InboxPath(outside))
        elif which == "staging":
            safe_unlink_staging(StagingPath(outside))
        else:
            safe_unlink_tmp(outside)
    assert outside.exists(), "拒否したときはファイルが残っていること"


@pytest.mark.parametrize("which", ["inbox", "staging", "tmp"])
def test_safe_unlink_rejects_a_device_path(which: str) -> None:
    """`DevicePath` を渡すと実行時にも弾かれること（§14.4 N-10）。

    mypy でも弾かれる（PR 本文に出力を貼る）。`NewType` は実行時に消えるため、
    型検査を通していない経路からの呼び出しに対して二重に守る。
    """
    device = DevicePath(PurePosixPath("TX_MIC001_20260829_071201/a.wav"))
    with pytest.raises(TypeError, match="DevicePath は PurePosixPath"):
        if which == "inbox":
            safe_unlink_inbox(device)  # type: ignore[arg-type]
        elif which == "staging":
            safe_unlink_staging(device)  # type: ignore[arg-type]
        else:
            safe_unlink_tmp(device)  # type: ignore[arg-type]


def test_safe_unlink_rejects_a_symlink_pointing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """root 外を指す symlink は**封じ込めの段階で**落ちる（これが本命の防壁）。"""
    roots = _patch_roots(monkeypatch, tmp_path)
    real = tmp_path / "real.wav"
    real.write_bytes(b"x")
    link = roots["inbox"] / "link.wav"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="配下ではありません"):
        safe_unlink_inbox(InboxPath(link))
    assert link.is_symlink(), "リンクが残っていること"
    assert real.exists(), "実体が残っていること"


def test_safe_unlink_rejects_a_symlink_pointing_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """root 内を指す symlink も削除しない。

    `unlink` はリンクだけを外すので無害だが、「消そうとしているものが想定と違う」
    合図なので安全側へ倒す（§1.3）。
    """
    roots = _patch_roots(monkeypatch, tmp_path)
    real = roots["inbox"] / "real.wav"
    real.write_bytes(b"x")
    link = roots["inbox"] / "link.wav"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="symlink は削除しません"):
        safe_unlink_inbox(InboxPath(link))
    assert link.is_symlink(), "リンクが残っていること"
    assert real.exists(), "実体が残っていること"


def test_safe_unlink_rejects_a_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = _patch_roots(monkeypatch, tmp_path)
    folder = roots["inbox"] / "TX_MIC001_20260829_071201"
    folder.mkdir()
    (folder / "a.wav").write_bytes(b"x")

    with pytest.raises(ValueError, match="通常ファイルではありません"):
        safe_unlink_inbox(InboxPath(folder))
    assert (folder / "a.wav").exists(), "中身が残っていること"


def test_safe_unlink_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = _patch_roots(monkeypatch, tmp_path)
    with pytest.raises(FileNotFoundError):
        safe_unlink_inbox(InboxPath(roots["inbox"] / "nope.wav"))
    with pytest.raises(FileNotFoundError):
        safe_unlink_staging(StagingPath(roots["data"] / "nope.wav"))
    # 一時ファイルは失敗経路の後片付けで呼ばれるため、既定で黙って返る
    safe_unlink_tmp(roots["obsidian"] / ".nope.md.tmp")


def test_safe_unlink_missing_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = _patch_roots(monkeypatch, tmp_path)
    safe_unlink_inbox(InboxPath(roots["inbox"] / "nope.wav"), missing_ok=True)
    with pytest.raises(FileNotFoundError):
        safe_unlink_tmp(roots["obsidian"] / ".nope.md.tmp", missing_ok=False)


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        (".2026-08-29 Voice.md.tmp", True),
        (".a.tmp", True),
        ("2026-08-29 Voice.md", False),
        ("note.md.tmp", False),  # 先頭の '.' が無い
        (".note.md", False),  # 末尾が .tmp でない
        (".tmp", False),  # 実体の名前が無い（'.' と '.tmp' が重なっているだけ）
    ],
)
def test_safe_unlink_tmp_requires_the_tmp_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, allowed: bool
) -> None:
    roots = _patch_roots(monkeypatch, tmp_path)
    target = roots["obsidian"] / name
    target.write_text("x", encoding="utf-8")
    if allowed:
        safe_unlink_tmp(target)
        assert not target.exists()
    else:
        with pytest.raises(ValueError, match="一時ファイルではありません"):
            safe_unlink_tmp(target)
        assert target.exists()


# --- 恒久識別子（§8.1 / §8.5） ------------------------------------------


PINNED_DEVICE_ID = "DJIMIC3"
PINNED_RELPATH = "TX_MIC001_20260829_071201/TX01_MIC002_20260829_071204_orig.wav"
PINNED_PARTKEY = "DJIMIC3/TX_MIC001_20260829_071201/TX01_MIC002_20260829_071204_orig.wav"
PINNED_SLUG = "a5d046dce76cfedc"


def test_partkey_is_pinned() -> None:
    """**`partkey` の算出結果を固定する。§8.5 の唯一の禁則がこれである。**

    この値はノートの frontmatter（`voicedock_recording_keys`。§13.3）に載り、
    §14.1 の削除条件は「ノートがこの鍵を含むか」だけを見る。

    **算出規則を変えると、既に保存したノートの鍵と食い違う。**§14.1 は偽に倒れるので
    削除事故にはならないが、**それ以前に保存した録音が永久に削除対象にならない。**

    したがって**このテストが落ちたら、期待値を書き換えて通すのではなく、
    変更をやめるか、保存済みノートを書き換える手順を同時に用意する**こと。
    v5.0 までの §8.5 は「振り直しに繋がる構文」を代理で禁止していたが、
    v5.1 は不変量そのものをここで固定する（v5.0→v5.1 の変更 M-2）。
    """
    key = paths.partkey_for(PINNED_DEVICE_ID, DevicePath(PurePosixPath(PINNED_RELPATH)))
    assert key == PINNED_PARTKEY


def test_the_spec_names_partkey_for_as_the_only_source() -> None:
    """§5.3 の Part キーが `partkey_for()` であること（v5.2→v5.3 の変更 O-4）。

    **v5.2 まで §5.3 は `part_key = (transmitter_id, mic_index, started_at)` と書いていた。**
    v5.1 で §8.1 が自然キーへ移ったのに、§5.3 は v4.x の複合キーのままだった。しかも
    **`transmitter_id` は §5.2 自身が「弱い識別子。恒久 ID として使わない」と書いている値**
    である。素直に読んだ実装者は二重処理防止をこの組で書き、**主キー制約とは別の
    同一性判定が 2 系統できる。**
    """
    block = spec_section_code("5.3", "python")
    assert "partkey_for" in block, "§5.3 が partkey_for() 以外の算出規則を示している"
    assert "transmitter_id" not in block, "§5.3 に v4.x の複合キーが戻っている"


def test_key_slug_is_pinned() -> None:
    """`key_slug` も固定する。`staging` / `transcripts` / `analysis` のパスになる。

    こちらが変わっても**削除の安全性は動かない**（正は `partkey` であり、slug は
    ファイル名のための派生値である）。ただし変えると作業中の Part の中間成果物が
    見つからなくなり、§9.4 の再開規則が最初からやり直す。
    """
    assert paths.key_slug(paths.PartKey(PINNED_PARTKEY)) == PINNED_SLUG
    assert len(PINNED_SLUG) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", PINNED_SLUG)


def test_key_slug_accepts_a_session_key() -> None:
    """`session_key` も受ける（`analysis_path` に使う。§8.3）。"""
    slug = paths.key_slug(paths.SessionKey("DJIMIC3:20260829"))
    assert re.fullmatch(r"[0-9a-f]{16}", slug)


def test_key_slug_differs_per_key() -> None:
    other = PINNED_PARTKEY.replace("MIC002", "MIC003")
    assert paths.key_slug(paths.PartKey(other)) != PINNED_SLUG


def test_partkey_round_trips() -> None:
    key = paths.partkey_for(PINNED_DEVICE_ID, DevicePath(PurePosixPath(PINNED_RELPATH)))
    assert paths.device_id_of(key) == PINNED_DEVICE_ID
    assert paths.relpath_of(key) == PurePosixPath(PINNED_RELPATH)


def test_relpath_of_returns_a_device_path() -> None:
    """戻り値は `PurePosixPath` なので `open()` / `stat()` を書けない（§11.2）。"""
    key = paths.partkey_for(PINNED_DEVICE_ID, DevicePath(PurePosixPath(PINNED_RELPATH)))
    rel = paths.relpath_of(key)
    assert isinstance(rel, PurePosixPath)
    assert not hasattr(rel, "open")
    assert not hasattr(rel, "stat")


def test_partkey_survives_a_device_id_with_a_space() -> None:
    """`NO NAME` のような Volume 名でも鍵は作れる（§5.4）。

    ファイル名には使えないが、それは `key_slug()` が引き受ける。
    """
    key = paths.partkey_for("NO NAME", DevicePath(PurePosixPath(PINNED_RELPATH)))
    assert key == f"NO NAME/{PINNED_RELPATH}"
    assert paths.device_id_of(key) == "NO NAME"


@pytest.mark.parametrize(
    ("device_id", "rel", "reason"),
    [
        ("", PINNED_RELPATH, "device_id が空"),
        ("a/b", PINNED_RELPATH, "device_id に '/'"),
        (".hidden", PINNED_RELPATH, "device_id が '.' 始まり"),
        (PINNED_DEVICE_ID, "../escape.wav", "relpath に '..'"),
        (PINNED_DEVICE_ID, "/absolute.wav", "relpath が絶対パス"),
        (PINNED_DEVICE_ID, ".Trashes/x.wav", "relpath の要素が '.' 始まり"),
    ],
)
def test_partkey_for_rejects_bad_input(device_id: str, rel: str, reason: str) -> None:
    """**鍵を組み立てる前に弾く。**壊れた鍵がノートへ載ると、あとから直す手段が無い。"""
    with pytest.raises(ValueError):
        paths.partkey_for(device_id, DevicePath(PurePosixPath(rel)))


def test_partkey_for_reuses_is_safe_relpath() -> None:
    """relpath の検査を二重実装していないこと（§11.1 の「規則を 2 か所に書かない」）。

    `is_safe_relpath` が偽にするものは `partkey_for` も必ず拒否する。
    """
    for rel in ("", ".", "..", "/x", "a/../b", ".hidden/x.wav", "a/\x01/b"):
        candidate = DevicePath(PurePosixPath(rel))
        if is_safe_relpath(candidate):
            continue
        with pytest.raises(ValueError):
            paths.partkey_for(PINNED_DEVICE_ID, candidate)


def test_partkey_and_relpath_are_consistent_with_the_spec_example() -> None:
    """SPEC §8.2 / §14.1.1 の例に出てくる値と一致すること。

    §14.1.1 の reaper 検証 12 は `<device_id>/<relpath>` が `partkey` と一致することを
    確かめる。**その等式をここで固定する。**
    """
    spec = (Path(__file__).parents[2] / "docs" / "SPEC.md").read_text(encoding="utf-8")
    assert PINNED_PARTKEY in spec, "SPEC の例と鍵が食い違っている"
    key = paths.partkey_for(PINNED_DEVICE_ID, DevicePath(PurePosixPath(PINNED_RELPATH)))
    assert f"{paths.device_id_of(key)}/{paths.relpath_of(key)}" == key
