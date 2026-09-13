"""`helper/` の bash が macOS の bash 3.2 と POSIX 環境で動くことを静的に固定する。

**`make test` はコンテナの bash 5.2 で走るので、bash 3.2 互換は動作では確認できない。**
macOS 同梱の `/bin/bash` は 3.2.57 であり（2007 年リリース。Apple が GPLv3 を避けて
更新していない）、そこで落ちる構文をコンテナのテストは 1 つも検出しない。
**だから静的に見る。**

あわせて §14.4 の禁則のうち静的に見えるもの（N-18 / N-19）と、
`helper.example.conf` と SPEC §7.4 の一致もここで固定する。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_helper_conf

HELPER_DIR = REPO_ROOT / "helper"
INGEST = HELPER_DIR / "voicedock-ingest"
INSTALL = HELPER_DIR / "install.sh"
EXAMPLE_CONF = HELPER_DIR / "helper.example.conf"
DOCTOR_SH = REPO_ROOT / "scripts" / "doctor.sh"

pytestmark = pytest.mark.skipif(not INGEST.is_file(), reason="helper/ がマウントされていない")


def bash_files() -> list[Path]:
    """静的検査の対象。**`scripts/doctor.sh` も macOS の bash 3.2 で走る**（§19.2）。"""
    found = [INGEST, INSTALL, DOCTOR_SH]
    missing = [p.name for p in found if not p.is_file()]
    assert missing == [], f"helper/ のスクリプトが足りない: {missing}"
    return found


def strip_comments(text: str) -> str:
    """`#` 行コメントを落とす（コメント中の語で誤検出しないため）。

    行頭から見て `#` より後を落とす。文字列中の `#` は落とさないよう、
    行頭が `#` か、空白の直後の `#` だけを扱う。
    """
    lines = []
    for line in text.splitlines():
        stripped = re.sub(r"(^|\s)#.*$", "", line)
        lines.append(stripped)
    return "\n".join(lines)


# bash 4 以降でしか動かない構文。macOS の /bin/bash（3.2）では構文エラーか無言の誤動作になる
BASH4_ONLY: dict[str, re.Pattern[str]] = {
    "declare -A（連想配列）": re.compile(r"\bdeclare\s+-A\b"),
    "local -A（連想配列）": re.compile(r"\blocal\s+-A\b"),
    "mapfile": re.compile(r"\bmapfile\b"),
    "readarray": re.compile(r"\breadarray\b"),
    "${x^^} / ${x^}（大文字化）": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\^"),
    "${x,,} / ${x,}（小文字化）": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*,"),
    "&>>（追記リダイレクト）": re.compile(r"&>>"),
    "[[ -v ]]": re.compile(r"\[\[\s+-v\s"),
    "${x@Q} などの変換": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*@[QEPAa]\}"),
    "coproc": re.compile(r"\bcoproc\b"),
}


@pytest.mark.parametrize("label", sorted(BASH4_ONLY))
def test_helper_avoids_bash4_only_syntax(label: str) -> None:
    """macOS の bash 3.2 で動かない構文が無いこと。

    **コンテナの bash 5.2 では通ってしまうので、動作テストでは捕まらない。**
    """
    pattern = BASH4_ONLY[label]
    offenders = [
        p.name
        for p in bash_files()
        if pattern.search(strip_comments(p.read_text(encoding="utf-8")))
    ]
    assert offenders == [], f"{label} は bash 3.2 で使えない: {offenders}"


@pytest.mark.parametrize(
    ("label", "snippet"),
    [
        ("declare -A（連想配列）", "declare -A seen\n"),
        ("mapfile", "mapfile -t lines < file\n"),
        ("${x^^} / ${x^}（大文字化）", 'printf "%s" "${name^^}"\n'),
        ("&>>（追記リダイレクト）", "cmd &>> out.log\n"),
        ("[[ -v ]]", "[[ -v FOO ]] && echo yes\n"),
    ],
)
def test_the_bash4_checks_actually_detect_violations(label: str, snippet: str) -> None:
    """**検査そのものが効くこと。**文字列を直接与えて確認する。"""
    assert BASH4_ONLY[label].search(snippet), f"{label} を検出できていない"


def test_comments_do_not_trigger_false_positives() -> None:
    """コメント中の語で誤検出しないこと（ingest の冒頭が実際に列挙している）。"""
    text = "#   declare -A / mapfile / readarray / ${x^^} を使わない\nfoo=1\n"
    for pattern in BASH4_ONLY.values():
        assert not pattern.search(strip_comments(text))


def test_strict_mode_is_enabled() -> None:
    """`set -euo pipefail` があること。

    `pipefail` が無いと `cat src | tee dst | sha256` の途中の失敗を見逃し、
    **壊れたコピーに正しい SHA-256 が付く**（§10.5 のコピー検証が意味を失う）。
    """
    for path in bash_files():
        assert "set -euo pipefail" in path.read_text(encoding="utf-8"), path.name


def test_shebang_is_bash() -> None:
    for path in bash_files():
        assert path.read_text(encoding="utf-8").startswith("#!/bin/bash\n"), path.name


def test_does_not_parse_ls_output() -> None:
    """`ls` の出力でファイル名を列挙していないこと。

    **ボリューム名には空白が入る**（`Macintosh HD` / 出荷時の `NO NAME`）。§5.4 が
    配列を要求しているのと同じ問題で、`ls` の出力を割ると名前が壊れる。
    """
    for path in bash_files():
        body = strip_comments(path.read_text(encoding="utf-8"))
        assert not re.search(r"(^|[|(;`$]|\bfor\s+\w+\s+in\s+)\s*\bls\b", body), path.name


def test_does_not_depend_on_jq() -> None:
    """`jq` を呼んでいないこと。**macOS は同梱しない**（判断 8）。"""
    for path in bash_files():
        assert not re.search(r"\bjq\b", strip_comments(path.read_text(encoding="utf-8"))), path.name


def test_empty_array_expansion_is_guarded() -> None:
    """空配列の展開が裸で書かれていないこと。

    **bash 3.2 の `set -u` では空配列の `"${ARR[@]}"` が
    「unbound variable」で落ちる**（bash 4.4 以降は落ちない）。§5.4 の規範実装は
    `[ ${#ARR[@]} -gt 0 ] &&` で囲んでいる。
    """
    body = strip_comments(INGEST.read_text(encoding="utf-8"))
    for name in ("INCLUDE_VOLUMES", "EXCLUDE_VOLUMES", "ALL_RELPATH"):
        for line_number, line in enumerate(body.splitlines(), start=1):
            if f'"${{{name}[@]}}"' not in line:
                continue
            # 同じ行か直前の数行に件数の検査があること
            window = "\n".join(body.splitlines()[max(0, line_number - 4) : line_number])
            assert f"${{#{name}[@]}} -gt 0" in window, (
                f"{name} の展開が件数検査で囲まれていない（{line_number} 行目）: {line.strip()}"
            )


# --- §14.4 の禁則（静的に見えるもの） ------------------------------------


def test_ingest_never_writes_to_the_device() -> None:
    """**N-11 / N-18**: デバイス上のパスへ `rm` / `mv` / リダイレクトを書かないこと。

    `voicedock-ingest` が触ってよいのは `$VOICEDOCK_HOME` 配下だけである
    （デバイスに対しては読み取りと `diskutil` のマウント操作のみ）。
    """
    body = strip_comments(INGEST.read_text(encoding="utf-8"))
    device_vars = ("$src", '"$src"', "$VOLUMES_ROOT", '"$VOLUMES_ROOT"', "${CAND_SRC")
    for line in body.splitlines():
        if not re.search(r"\b(rm|mv|rmdir|truncate|chmod|chown)\b", line):
            continue
        for var in device_vars:
            assert var not in line, (
                f"デバイス上のパスへ破壊的操作を書いている（N-18）: {line.strip()}"
            )


def test_only_ingest_calls_diskutil() -> None:
    """**N-19**: `diskutil` を呼ぶのは `voicedock-ingest` だけであること。

    マウント操作を ingest に閉じることで **reaper を POSIX 互換に保ち、§20.4 を CI で
    実行可能にする。**reaper が `diskutil` を呼ぶと Linux で動かせなくなる。
    """
    for path in sorted(HELPER_DIR.iterdir()):
        if not path.is_file() or path.name in {"voicedock-ingest", "helper.example.conf"}:
            continue
        body = strip_comments(path.read_text(encoding="utf-8"))
        assert "diskutil" not in body, f"{path.name} が diskutil を呼んでいる（N-19）"


def test_install_does_not_place_the_reaper_by_default() -> None:
    """**安全ロック 2-A**: 既定で `voicedock-reaper` を配置しないこと（§14.2）。

    引数なしの経路に `install_reaper` が現れてはならない。**存在しないプログラムは
    実行できない**というのがこのロックの強度である。
    """
    body = INSTALL.read_text(encoding="utf-8")
    matched = re.search(r'^\s*""\)\n(.*?)^\s*\*\)', body, re.S | re.M)
    assert matched is not None, "install.sh の引数なしの分岐が見つからない"
    assert "install_reaper" not in matched.group(1), (
        "既定で reaper を配置している（ロック 2-A 違反）"
    )
    assert "--with-reaper" in body


# --- helper.example.conf と SPEC §7.4 の一致 -----------------------------


def test_helper_conf_matches_spec() -> None:
    """`helper/helper.example.conf` が §7.4 の `sh` ブロックと完全一致すること。

    §7.2 と `config/config.example.yaml` の関係と同じである（`test_config.py` の
    `test_example_matches_spec`）。**Helper の設定は `helper.conf` にしか無い**ので
    （§7.4 の一本化）、SPEC と example が食い違うと
    **「ユーザーが手で書く唯一の設定」が壊れる。**
    """
    assert EXAMPLE_CONF.read_text(encoding="utf-8").rstrip("\n") == spec_helper_conf().rstrip("\n")


def test_example_conf_covers_every_validated_key() -> None:
    """§7.4 の検証 1〜7 が見る全キーが example にあること。"""
    body = EXAMPLE_CONF.read_text(encoding="utf-8")
    for key in (
        "VOLUMES_ROOT",
        "VOICEDOCK_HOME",
        "INCLUDE_VOLUMES",
        "EXCLUDE_VOLUMES",
        "MOUNT_MODE",
        "DELETE_SOURCE_AUDIO",
        "STABILITY_FAST_PATH_SECONDS",
        "STABILITY_INTERVAL_SECONDS",
        "STABILITY_CHECKS",
        "MAX_SCAN_DEPTH",
    ):
        assert re.search(rf"^{key}=", body, re.M), f"{key} が helper.example.conf に無い"


def test_example_conf_declares_arrays() -> None:
    """`INCLUDE_VOLUMES` / `EXCLUDE_VOLUMES` が**配列として**書かれていること（検査 3）。

    v4.0 の例は `EXCLUDE_VOLUMES="Macintosh HD Time Machine*"` という空白区切りの
    文字列で、**除外がまったく働かなかった**（§7.4）。
    """
    body = EXAMPLE_CONF.read_text(encoding="utf-8")
    assert re.search(r"^INCLUDE_VOLUMES=\(", body, re.M)
    assert re.search(r"^EXCLUDE_VOLUMES=\(", body, re.M)


# --- plist（§10.1） -----------------------------------------------------


def plist_body() -> str:
    """XML コメントを落とした plist。

    plist の冒頭コメントは「`KeepAlive` を書いてはならない」と**禁止対象の語を含む。**
    bash 側で `#` コメントを落としているのと同じ理由で落とす。
    """
    text = (HELPER_DIR / "com.voicedock.ingest.plist").read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def test_plist_has_both_start_triggers() -> None:
    """`StartOnMount` と `StartInterval` の両方があること（§10.1）。

    前者が無いと接続に反応せず、後者が無いと取りこぼしと heartbeat の更新が止まる。
    """
    plist = plist_body()
    assert "<key>StartOnMount</key>" in plist
    assert "<key>StartInterval</key>" in plist
    assert "<string>com.voicedock.ingest</string>" in plist, "DH-12 が見る Label"


def test_plist_has_no_keepalive() -> None:
    """`KeepAlive` を書いていないこと。

    ingest は**1 回走って終わる**プログラムである。常駐させると §10.1 の
    「ポーリングループを持たない」に反し、未接続時のアイドル CPU が 0% でなくなる
    （§21.2 Phase 1 の受け入れ条件）。
    """
    assert "KeepAlive" not in plist_body()


def test_the_wav_is_placed_before_the_tombstone() -> None:
    """**`.wav` を確定してから `.meta.json` を置く順序であること**（§10.2）。

    この順序が壊れていても**通常の実行では観測できない。**差が出るのは
    **2 つの `mv` の間で落ちたとき**だけである。

    正しい順序（`.wav` → `.meta.json`）で落ちると `.wav` だけが残る。墓標が無いので
    次回の Helper が**再コピーし、復帰する。**

    誤った順序（`.meta.json` → `.wav`）で落ちると `.meta.json` だけが残る。墓標が
    あるので次回の Helper は**コピーせず、その録音が静かに失われる。**

    クラッシュを注入するテストは書けないので、**順序そのものを静的に固定する。**
    """
    body = strip_comments(INGEST.read_text(encoding="utf-8"))
    wav_mv = body.index('mv "$partial" "$dst_dir/$name"')
    meta_mv = body.index('mv "$dst_dir/$name.meta.json.tmp" "$dst_dir/$name.meta.json"')
    assert wav_mv < meta_mv, (
        "`.meta.json` を `.wav` より先に置いている。2 つの mv の間で落ちると、"
        "墓標だけが残ってその録音が永久にコピーされない（§10.2）"
    )
