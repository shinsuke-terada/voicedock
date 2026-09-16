"""README と SPEC が名乗る検査の件数が、実装と一致することを固定する。

**同じずれが 2 度起きた。**DH-16（#95）と DH-17（#104）を足したとき、
**`doctor.sh` は増えたのに README と SPEC は「5 件」のままだった。**D-20（#120）でも
同じことが起きた。**件数の直書きは、直す場所が 3 つに散っている**（実装 / SPEC / README）。

§20.5 の 3 番目の形である — **件数を直書きすると、実装を直したときにテストと文書だけが
古いまま残る。**ここが唯一の突き合わせ点になる。
"""

from __future__ import annotations

import re

from tests.helpers import REPO_ROOT
from voicedock import doctor

README = REPO_ROOT / "README.md"
DOCTOR_SH = REPO_ROOT / "scripts" / "doctor.sh"


def host_check_count() -> int:
    """`scripts/doctor.sh` の `main()` が呼ぶ検査の数。

    **実行順のコメントではなく実際の呼び出しを数える。**コメントは古くなりうる。
    """
    body = DOCTOR_SH.read_text(encoding="utf-8")
    main = body[body.index("\nmain() {") :]
    main = main[: main.index("\n}\n")]
    return len(re.findall(r"^    check_[a-z_]+", main, re.M))


def container_check_count() -> int:
    return len(doctor.CHECKS)


def test_the_host_and_container_counts_are_what_we_think() -> None:
    """**まずここを見る。**下の 2 つが落ちたとき、実装が変わったのか文書が古いのかを分ける。"""
    assert host_check_count() == 7
    assert container_check_count() == 13


def test_the_readme_states_the_real_counts() -> None:
    """README の「ホスト N 件 → コンテナ M 件（合計 N+M）」が実装と一致すること。"""
    text = README.read_text(encoding="utf-8")
    host = host_check_count()
    container = container_check_count()
    total = host + container

    assert f"ホスト側の {host} 件" in text, f"README のホスト件数が {host} でない"
    assert f"コンテナ内の {container} 件" in text, f"README のコンテナ件数が {container} でない"
    assert f"（合計 {total}）" in text, f"README の合計が {total} でない"
    assert f"ホスト {host} 件 → コンテナ {container} 件の診断（合計 {total}）" in text


def test_the_spec_states_the_real_counts() -> None:
    """§21.1 の記述が実装と一致すること。"""
    from tests.spec_sync import spec_text

    text = spec_text()
    host = host_check_count()
    container = container_check_count()
    assert f"DH 表（**{host} 件**）" in text
    assert (
        f"ホスト側の DH **{host} 件**に続けてコンテナ内の D **{container} 件**が実行される"
        f"（**合計 {host + container}**）" in text
    )


def test_the_readme_does_not_pin_the_spec_version() -> None:
    """**README に SPEC の版を直書きしない。**

    `詳細仕様書 v5.2` のまま v5.30 まで放置されていた。**版は SPEC 自身が名乗る。**
    """
    text = README.read_text(encoding="utf-8")
    assert not re.search(r"詳細仕様書\s*v\d+\.\d+", text)


def test_every_command_the_readme_shows_exists() -> None:
    """README のセットアップ手順が指すファイルが実在すること。"""
    text = README.read_text(encoding="utf-8")
    for rel in re.findall(r"^(\./(?:helper|scripts)/[a-z._-]+)", text, re.M):
        assert (REPO_ROOT / rel).is_file(), f"README が無いファイルを指している: {rel}"


def test_every_make_target_the_readme_lists_exists() -> None:
    """README が並べる `make` のターゲットが Makefile にあること。"""
    text = README.read_text(encoding="utf-8")
    listed = set(re.findall(r"`(up|down|logs|status|test|doctor|models|lock|helper-[a-z]+)`", text))
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    defined = set(re.findall(r"^([a-z-]+):", makefile, re.M))
    missing = sorted(listed - defined)
    assert missing == [], f"README が無い make ターゲットを挙げている: {missing}"
