"""§18.5 のバージョン固定を静的に確かめる。

**`latest` / `master` / `main` への無条件依存を禁止する。**更新は手動 PR としてのみ行う。
無人で 1 日中動くものが、こちらの知らないうちに中身を入れ替えられてはならない。

**digest 固定はビルドの再現性だけの話ではない。**§14 の削除は「テキストが Vault に
確実に残っていること」を根拠にしており、その判断を下すコード（ffmpeg / whisper.cpp /
Python 依存）が黙って変わると、**根拠そのものが揺らぐ。**
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT

DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "compose.yaml"
MAKEFILE = REPO_ROOT / "Makefile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PINNED_FILES: tuple[Path, ...] = (DOCKERFILE, COMPOSE, MAKEFILE, WORKFLOW)

FLOATING = re.compile(r":(latest|master|main)\b")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\b")


def strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


def external_from_lines() -> list[str]:
    """`FROM` のうち**外部から取るもの**だけ。多段ビルドの内部参照は除く。"""
    stages = set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", DOCKERFILE.read_text("utf-8"), re.M))
    found: list[str] = []
    for line in DOCKERFILE.read_text("utf-8").splitlines():
        matched = re.match(r"^FROM\s+(\S+)", line)
        if matched and matched.group(1) not in stages:
            found.append(matched.group(1))
    return found


def test_every_base_image_is_pinned_by_digest() -> None:
    """base image が digest で固定されていること（§18.5）。

    **tag の併記は人が読むためで、解決に使われるのは digest だけ**である。
    """
    unpinned = [image for image in external_from_lines() if not DIGEST.search(image)]
    assert unpinned == [], f"digest で固定されていない base image: {unpinned}"


def test_the_internal_stage_reference_is_not_pinned() -> None:
    """**多段ビルドの内部参照を digest で固定しない**（検査が空振りしていないことの確認）。

    `FROM runtime AS dev` のようなステージ参照に digest は付けられない。
    """
    lines = [line for line in DOCKERFILE.read_text("utf-8").splitlines() if line.startswith("FROM")]
    assert any(" AS " in line and "@sha256:" not in line for line in lines)


@pytest.mark.parametrize("path", PINNED_FILES, ids=lambda p: p.name)
def test_no_floating_tags(path: Path) -> None:
    """`latest` / `master` / `main` への無条件依存が 1 つも無いこと（§18.5）。"""
    offenders = [
        line.strip()
        for line in strip_comments(path.read_text("utf-8")).splitlines()
        if FLOATING.search(line)
    ]
    assert offenders == [], f"{path.name} に浮動タグがある（§18.5）: {offenders}"


def test_whisper_cpp_is_pinned_to_a_tag() -> None:
    """whisper.cpp が `ARG WHISPER_CPP_REF` の tag 固定であること（§18.5）。

    **digest ではなく tag なのは SPEC の規定どおり**である。ソースからビルドするので、
    tag が動かないこと（whisper.cpp は tag を打ち直さない）に依存している。
    """
    matched = re.search(r"^ARG WHISPER_CPP_REF=(\S+)", DOCKERFILE.read_text("utf-8"), re.M)
    assert matched is not None, "ARG WHISPER_CPP_REF が無い"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", matched.group(1)), matched.group(1)


def test_the_lock_generation_image_is_pinned() -> None:
    """`UV_IMAGE` のタグが固定されていること（§18.5 / §17.4）。"""
    matched = re.search(r"^UV_IMAGE \?= (\S+)", MAKEFILE.read_text("utf-8"), re.M)
    assert matched is not None
    assert ":" in matched.group(1) and not FLOATING.search(matched.group(1))


def test_the_lock_files_are_committed() -> None:
    """`uv.lock` と `requirements*.lock` がコミットされていること（§18.5 / A-27）。"""
    for name in ("uv.lock", "requirements.lock", "requirements-dev.lock"):
        assert (REPO_ROOT / name).is_file(), f"{name} が無い"


def test_the_ci_actions_are_pinned() -> None:
    """GitHub Actions も浮動参照にしないこと（§18.5 の同じ理由）。

    **`@v7` のような浮動メジャータグも避ける。**§18.5 は「`latest` / `master` / `main`
    への無条件依存」を禁じているが、理由は「知らないうちに中身が入れ替わらないこと」で
    あり、浮動メジャータグは同じ性質を持つ。
    """
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW.read_text("utf-8"))
    assert uses, "workflow に uses が無い"
    for reference in uses:
        _action, _, version = reference.partition("@")
        assert re.fullmatch(r"v\d+\.\d+\.\d+|[0-9a-f]{40}", version), reference
