"""ファイル名の sanitize・atomic write・保存検証（SPEC §13.5, §13.6, §13.7）。

**保存検証をレンダラより先に作る。これは安全設計上の順序制約である。**

§14.1 の削除条件は「音声のコピーが正しかったこと」ではなく
**「テキストが Vault に確実に残っていること」**を根拠にしている。検証を後から足すと、
検証前に保存されたノートが DB 上 `SAVED` として残り、
**Phase 7 で削除を有効化した瞬間に一括誤削除の入力になる。**

だから R-1〜R-6 / W-1〜W-9 をここに独立した関数として置き、
#24 / #28 のレンダラと #36 の `cleaner.py` が**同じ実装を再利用する**
（検証ロジックが 2 本に分かれない）。

**このモジュールは削除しない。**一時ファイルの削除は `paths.safe_unlink_tmp()` を通す
（§14.4 N-10）。`tests/unit/test_no_device_delete.py` が `src/voicedock/*.py` を
AST で全走査しており、削除呼び出しを書いた時点で落ちる。

**frontmatter の書き出しに PyYAML を使わない。**`yaml.dump` は値によって引用を省くため、
**LLM 生成タグに `:` や `#` が入ると frontmatter が壊れる。**壊れると W-5 / W-6 が落ち、
LLM は同じタグを再生成するのでリトライも効かず、**その日の Daily ノートが恒久的に
作れなくなる**（§1.3 優先順位 2 の違反）。読み取りだけ `yaml.safe_load` を使う。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from voicedock import paths
from voicedock.paths import VaultPath

# --- §13.5 sanitize -----------------------------------------------------

_CONTROL_CHARS: Final = re.compile(r"[\x00-\x1f\x7f]")
"""S-2: U+0000–U+001F と U+007F。"""

_REPLACED_CHARS: Final = re.compile(r'[/\\:*?"<>|]')
"""S-3: パス区切りと Windows で使えない文字。`-` へ置換する。"""

_REMOVED_CHARS: Final = re.compile(r"[#^\[\]]")
"""S-4: Obsidian のリンク・ブロック参照記法と衝突する文字。除去する。"""

_WHITESPACE: Final = re.compile(r"\s+")
"""S-5: 連続する空白。"""

FALLBACK_NAME: Final = "Untitled"
"""S-8: 結果が空になったときの名前。"""

RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
"""S-9: Windows の予約デバイス名。**大小文字を区別しない。**"""


def sanitize_filename(name: str, *, max_bytes: int) -> str:
    """§13.5 の S-1〜S-9 を**この順で**適用する。

    **順序に意味がある。**S-5（空白の正規化）より先に S-3（`/` → `-`）を行うので
    `a / b` が `a - b` になる。S-9（予約名に `_`）は S-7（切り詰め）の**後**に置く —
    先に付けると切り詰めで `_` が落ちうる。

    ファイル名は通常は日付から生成されるので sanitize の対象が現れない。この規則は
    **タグ名・`[[]]` のリンク先・`raw.granularity: part` のカスタムテンプレート**に適用する。
    """
    text = unicodedata.normalize("NFC", name)  # S-1
    text = _CONTROL_CHARS.sub("", text)  # S-2
    text = _REPLACED_CHARS.sub("-", text)  # S-3
    text = _REMOVED_CHARS.sub("", text)  # S-4
    text = _WHITESPACE.sub(" ", text).strip()  # S-5
    text = text.strip(".")  # S-6
    text = _truncate_utf8(text, max_bytes)  # S-7
    if not text:  # S-8
        text = FALLBACK_NAME
    if text.upper() in RESERVED_NAMES:  # S-9
        text = f"{text}_"
    return text


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """S-7: UTF-8 バイト数が `max_bytes` 以下になるまで末尾のコードポイントを削る。

    **文字数ではなくバイト数で規定するのは、日本語 80 文字が 240 バイトになり
    ファイル名長の上限を超えるためである。**

    **結合文字が末尾に残ったらさらに 1 つ削る。**`が` を `か` + U+3099 で表した文字列を
    途中で切ると、**濁点だけが孤立して次の文字に付く**。書記素クラスタ単位の分割は
    標準ライブラリでは実装できないため採用しない（§11.4）。
    """
    while len(text.encode("utf-8")) > max_bytes and text:
        text = text[:-1]
    while text and unicodedata.combining(text[-1]):
        text = text[:-1]
    return text


# --- frontmatter --------------------------------------------------------

FRONTMATTER_DELIMITER: Final = "---"
SESSION_KEY_FIELD: Final = "voicedock_session_key"
RECORDING_KEYS_FIELD: Final = "voicedock_recording_keys"
"""§13.3 / §13.7。

**v5.1 で `voicedock_recording_ids` から改名した**（識別子が自然キーになったため。
v5.0→v5.1 の変更 M-8）。改名したのは、**古い形式のノートが R-6 / W-7 を
黙って通らないようにするため**である。
"""


def yaml_quote(value: str) -> str:
    """YAML の文字列値を**必ず**二重引用符で囲む（§13.4）。

    **`yaml.dump` を使わない。**値によって引用を省くため、`session_key`（`:` を含む）や
    LLM 生成タグ（`#` や `:` を含みうる）で **frontmatter が壊れる。**
    壊れると W-5 / W-6 が落ち、**LLM は同じタグを再生成するのでリトライも効かない。**
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = _CONTROL_CHARS.sub("", escaped)
    return f'"{escaped}"'


def render_frontmatter(fields: Mapping[str, object]) -> str:
    """frontmatter を組み立てる。末尾に区切りと改行を含む。

    リストは**ブロック形式**（1 行 1 要素）にする。`voicedock_recording_keys` は
    32 Part の日で約 2.4 KB になり（§13.3）、フロー形式では editor で読めない。
    """
    lines = [FRONTMATTER_DELIMITER]
    for key, value in fields.items():
        if isinstance(value, str):
            lines.append(f"{key}: {yaml_quote(value)}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, int | float):
            lines.append(f"{key}: {value}")
        elif value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, Sequence):
            if not value:
                # **空リストは `[]` と書く。**`key:` だけだと YAML が `null` と読み、
                # 「鍵が 0 件」と「鍵の欄が無い」が区別できなくなる。
                # Part を 0 件持つ Session は実在する（`session_empty`。§16.4）
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {yaml_quote(str(item))}" for item in value)
        else:  # pragma: no cover - 呼び手が渡さない型
            lines.append(f"{key}: {yaml_quote(str(value))}")
    lines.append(FRONTMATTER_DELIMITER)
    return "\n".join(lines) + "\n"


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """`(frontmatter 本体, 本文)` を返す。形が違えば `None`（W-5）。

    **先頭が `---\\n` で始まり、対応する終端 `---` の行があること**を要求する。
    """
    if not text.startswith(FRONTMATTER_DELIMITER + "\n"):
        return None
    rest = text[len(FRONTMATTER_DELIMITER) + 1 :]
    matched = re.search(r"^---\s*$", rest, re.M)
    if matched is None:
        return None
    return rest[: matched.start()], rest[matched.end() :]


def parse_frontmatter(text: str) -> dict[str, Any] | None:
    """frontmatter を辞書で返す。読めなければ `None`。**例外を投げない。**

    外部から改竄されたノートを読む可能性があるため、壊れた YAML は「不明」として扱う。
    **不明は安全側へ倒す**（＝削除条件が偽になる。§14.1）。
    """
    parts = split_frontmatter(text)
    if parts is None:
        return None
    try:
        document = yaml.safe_load(parts[0])
    except yaml.YAMLError:
        return None
    return document if isinstance(document, dict) else None


def frontmatter_keys(path: Path) -> tuple[str, ...]:
    """ノートの `voicedock_recording_keys` を返す。読めなければ空（§14.1）。

    **#36 の削除条件がこれを呼ぶ。**`part.partkey in frontmatter_keys(note)` が
    「そのファイルの文字起こしがこのノートに入っている」ことの唯一の根拠である。
    **読めないときに空を返すのは安全側**（削除が起きない）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    document = parse_frontmatter(text)
    if document is None:
        return ()
    value = document.get(RECORDING_KEYS_FIELD)
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def escape_body(body: str) -> str:
    """本文の行頭 `---` を `\\---` へ退避する（§13.4）。

    退避しないと、**W-5 の「対応する終端 `---`」が本文の途中で見つかり**、
    frontmatter の範囲を誤認する。
    """
    return re.sub(r"^---", r"\\---", body, flags=re.M)


# --- §13.6 Atomic Write -------------------------------------------------


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def atomic_write(path: VaultPath, content: bytes) -> str:
    """§13.6 の手順 1〜6 を行い、書き込んだ内容の SHA-256 を返す。

    **手順 7（保存検証）は呼び手が `verify_note()` で行う。**この関数が
    「Raw なのか Daily なのか」を知らなくて済むようにするためである。

    **読み直して SHA-256 を照合してから `os.replace` する**（手順 4 → 5）。
    ここを飛ばすと「書けたつもり」のノートが §14.1 の削除根拠になる。

    **途中で失敗したら一時ファイルを消し、最終ファイルは差し替えない。**
    再生成の失敗で既存ノートを壊さないことが §1.3 の優先順位 1 に直結する。

    Raises:
        OSError: 書き込みに失敗した。
        ValueError: 読み直した SHA-256 が一致しなかった（**`os.replace` は行っていない**）。
    """
    expected = sha256_bytes(content)  # 1
    target = Path(path)
    tmp = target.with_name(f"{paths.TMP_PREFIX}{target.name}{paths.TMP_SUFFIX}")  # 2
    try:
        with open(tmp, "wb") as handle:  # 3  # noqa: PTH123 - fsync に fd が要る
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        actual = sha256_bytes(tmp.read_bytes())  # 4
        if actual != expected:
            raise ValueError(
                f"一時ファイルの SHA-256 が一致しません（最終ファイルは差し替えていません）: "
                f"expected={expected} actual={actual}"
            )
        tmp.replace(target)  # 5  `os.replace(2)` と同じ。同一 FS 内で atomic である
    except BaseException:
        # **後片付けの失敗で元の例外を隠さない。**呼び手が知りたいのは
        # 「なぜ書けなかったか」であり、「片付けにも失敗したか」ではない。
        # `safe_unlink_tmp` は場所違反を `ValueError` にするので（§11.2）、
        # ここで握りつぶさないと**本当の原因が消える。**
        with contextlib.suppress(Exception):
            paths.safe_unlink_tmp(tmp)
        raise
    _fsync_directory(target.parent)  # 6
    return expected


def _fsync_directory(directory: Path) -> None:
    """親ディレクトリを fsync する。**失敗しても致命的としない**（§13.6 手順 6）。

    ファイルシステムによってはディレクトリの `open()` / `fsync()` が通らない。
    ここで落とすと、**実際には書けているのに失敗扱いになる。**
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --- §13.7 保存検証 -----------------------------------------------------


class NoteKind(StrEnum):
    """検証する規則の集合を選ぶ。

    **R-1〜R-4 と W-1〜W-4 は同じ検査である**（存在 / サイズ / UTF-8 / SHA-256）。
    別々に書くと片方だけ直る事故が起きるので、1 つの実装を共有する。
    """

    RAW = "raw"
    DAILY = "daily"


@dataclass(frozen=True)
class CheckResult:
    """1 規則の結果。"""

    rule: str
    """`R-4` / `W-7` などの規則 ID（§13.7）。**SPEC と一致させる。**"""

    ok: bool
    detail: str


DEFAULT_SUMMARY_HEADING: Final = "## Summary"


def verify_note(
    path: Path,
    *,
    kind: NoteKind,
    session_key: str,
    expected_sha: str,
    expected_keys: Collection[str],
    summary_heading: str = DEFAULT_SUMMARY_HEADING,
    require_raw_link: bool = False,
) -> tuple[CheckResult, ...]:
    """§13.7 の R-1〜R-6（Raw）または W-1〜W-9（Daily）を評価する。

    **真偽値ではなく「どの規則が落ちたか」を返す。**`OBSIDIAN_VERIFY_FAILED` の
    `detail` に載せるためであり、真偽値だけだと**何が欠けたのかログに出せない。**

    **1 つ落ちても残りを評価する。**最初の失敗で打ち切ると、直すべき箇所が
    1 件ずつしか分からない。

    `expected_keys` の扱いが Raw と Daily で違う。

    | | 条件 |
    |---|---|
    | **R-6** | `voicedock_recording_keys` が `expected_keys` を**すべて含む**（包含） |
    | **W-7** | `voicedock_recording_keys` が `expected_keys` と**完全一致**（集合として） |

    Raw はその日のノートを Part ごとに再生成するので**途中経過では多く載りうる**。
    Daily は統合対象そのものなので一致を要求する。**混同してはならない。**
    """
    prefix = "R" if kind is NoteKind.RAW else "W"
    results: list[CheckResult] = []

    def add(number: int, ok: bool, detail: str) -> None:
        results.append(CheckResult(f"{prefix}-{number}", ok, detail))

    # R-1 / W-1: 存在し、通常ファイルである
    try:
        is_file = path.is_file() and not path.is_symlink()
    except OSError as error:
        is_file = False
        add(1, False, f"stat failed: {error}")
    else:
        add(1, is_file, str(path) if is_file else f"not a regular file: {path}")
    if not is_file:
        return tuple(results)

    # R-2 / W-2: サイズ > 0
    size = path.stat().st_size
    add(2, size > 0, f"{size} bytes")
    if size == 0:
        return tuple(results)

    raw = path.read_bytes()

    # R-3 / W-3: UTF-8 としてデコードできる
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        add(3, False, f"not valid UTF-8: {error}")
        return tuple(results)
    add(3, True, "valid UTF-8")

    # R-4 / W-4: 読み直した SHA-256 が一致する
    actual_sha = sha256_bytes(raw)
    add(4, actual_sha == expected_sha, f"expected={expected_sha} actual={actual_sha}")

    if kind is NoteKind.DAILY:
        # W-5: 先頭が `---\n` で、対応する終端 `---` がある
        parts = split_frontmatter(text)
        add(5, parts is not None, "frontmatter delimiters" if parts else "no frontmatter block")

    document = parse_frontmatter(text)
    key_field = 5 if kind is NoteKind.RAW else 6
    if document is None:
        add(key_field, False, "frontmatter is not parseable YAML")
        add(key_field + 1, False, f"{RECORDING_KEYS_FIELD} unavailable")
        if kind is NoteKind.DAILY:
            _add_daily_body_checks(results, text, summary_heading, require_raw_link)
        return tuple(results)

    # R-5 / W-6: `voicedock_session_key` が一致する
    found_key = document.get(SESSION_KEY_FIELD)
    add(
        key_field,
        found_key == session_key,
        f"expected={session_key!r} found={found_key!r}",
    )

    # R-6 / W-7: `voicedock_recording_keys`
    value = document.get(RECORDING_KEYS_FIELD)
    found_keys = {str(item) for item in value} if isinstance(value, list) else set()
    wanted = set(expected_keys)
    if kind is NoteKind.RAW:
        missing = wanted - found_keys
        add(6, not missing, f"missing={sorted(missing)}" if missing else f"{len(wanted)} keys")
    else:
        add(
            7,
            found_keys == wanted,
            f"missing={sorted(wanted - found_keys)} extra={sorted(found_keys - wanted)}"
            if found_keys != wanted
            else f"{len(wanted)} keys",
        )

    if kind is NoteKind.DAILY:
        _add_daily_body_checks(results, text, summary_heading, require_raw_link)
    return tuple(results)


def _add_daily_body_checks(
    results: list[CheckResult], text: str, summary_heading: str, require_raw_link: bool
) -> None:
    """W-8 / W-9。"""
    results.append(CheckResult("W-8", _has_content_under(text, summary_heading), summary_heading))
    if require_raw_link:
        found = bool(re.search(r"\[\[[^\]]+\]\]", text))
        results.append(CheckResult("W-9", found, "raw link" if found else "no [[link]] found"))
    else:
        results.append(CheckResult("W-9", True, "not required (include_transcript is true)"))


def _has_content_under(text: str, heading: str) -> bool:
    """W-8: 見出しが存在し、その直下の本文が 1 文字以上あるか。

    「直下」は**次の見出しまで**とする。空の `## Summary` を通すと、
    **LLM が何も返さなかった日のノートが「保存成功」になる。**
    """
    matched = re.search(rf"^{re.escape(heading)}\s*$", text, re.M)
    if matched is None:
        return False
    rest = text[matched.end() :]
    next_heading = re.search(r"^#{1,6} ", rest, re.M)
    body = rest[: next_heading.start()] if next_heading else rest
    return bool(body.strip())


def all_passed(results: Collection[CheckResult]) -> bool:
    """全規則を満たしたか。**`verify_note()` の結果をそのまま渡す。**"""
    return all(result.ok for result in results)


def failed_rules(results: Collection[CheckResult]) -> tuple[str, ...]:
    """落ちた規則の ID。`OBSIDIAN_VERIFY_FAILED` の `detail` に載せる。"""
    return tuple(result.rule for result in results if not result.ok)


# --- §13.1 / §13.5 出力先と衝突 ----------------------------------------


def vault_is_available(root: Path, marker: str) -> bool:
    """`root` が**本物の Vault** かどうか（§13.6 / §19.1 H-4 / §19.2 D-13）。

    **「書ける」を「Vault である」と読んではならない**（v5.33→v5.34 の変更 AV-1）。
    Docker は bind mount の source が無ければ**空ディレクトリとして作る**ので、
    Vault を退避してもコンテナからは書ける場所に見え続ける。2026-09-15 の実機では
    その空ディレクトリへ Daily ノートを書き、`obsidian_saved` を報告していた（#134）。

    **目印は `.obsidian/` である。**Obsidian が Vault として開いた場所には必ず在り、
    Docker が作った空ディレクトリには無い。**人間が確かめるときに見るものと同じ**にする。

    **`marker` が空なら検査を無効化する。**Obsidian は Vault ごとに設定フォルダ名を
    変更できるため、逃げ道を必ず用意する — **誤検知でパイプラインが止まる方が害が大きい。**

    **空白だけの場合も無効化する。**`Path(root) / "" == root` なので空文字は分岐が無くても
    通ってしまうが、`vault_marker: " "` は通らない — **名前が半角空白 1 文字のディレクトリを
    探しにいく。**YAML でうっかり書ける形なので、ここで吸収する。

    外付けディスクが未マウント、iCloud / Dropbox が未同期、`OBSIDIAN_VAULT` の綴り間違い、
    Docker Desktop のファイル共有から外れている —— **いずれも「空の新品 Vault」という
    同じ姿になる。**
    """
    if not root.is_dir():
        return False
    if not marker.strip():
        return True
    return (root / marker).is_dir()


def resolve_output_path(folder: VaultPath, basename: str, session_key: str) -> VaultPath:
    """出力先のパスを決める（§13.5 の「重複時」）。

    同名ファイルが既に在り、その frontmatter の `voicedock_session_key` が一致すれば
    **そのパスを返す**（再生成の正常動作であり、上書きしてよい）。
    一致しない場合のみ ` (2)` ` (3)` … を拡張子の前に付ける。

    **読めないファイルは「別物」として扱う**（不明は安全側 = 上書きしない）。
    """
    candidate = Path(folder) / f"{basename}.md"
    if not candidate.exists() or _belongs_to(candidate, session_key):
        return VaultPath(candidate)
    for index in range(2, 100):
        numbered = Path(folder) / f"{basename} ({index}).md"
        if not numbered.exists() or _belongs_to(numbered, session_key):
            return VaultPath(numbered)
    raise ValueError(f"同名ファイルが多すぎます: {candidate}")


def _belongs_to(path: Path, session_key: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    document = parse_frontmatter(text)
    return document is not None and document.get(SESSION_KEY_FIELD) == session_key
