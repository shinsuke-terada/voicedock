"""`docs/SPEC.md` から表・一覧を取り出すヘルパ。

**SPEC と実装の整合をテストで固定するためにある。**§15.1 にエラーコードを 1 行足したのに
enum を忘れた、という事故を即座に落とす。

`make test` は `docs/` を読み取り専用でマウントする（SPEC §17.4）。SPEC が見つからない
場合は **skip せず fail する**。見つからないのは設定の壊れであり、黙って通すと
整合テストが存在しないのと同じになる。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import pytest
import yaml

SPEC_PATH = Path(__file__).parents[1] / "docs" / "SPEC.md"


def spec_text() -> str:
    """`docs/SPEC.md` の全文。見つからなければ fail する。"""
    if not SPEC_PATH.is_file():
        pytest.fail(
            f"docs/SPEC.md が見つかりません: {SPEC_PATH}。"
            "make test は docs/ を ro マウントする（SPEC §17.4）。マウント設定を確認すること"
        )
    return SPEC_PATH.read_text(encoding="utf-8")


# 次の見出し。`#` の後を「数字」か「付録」に限る。
# `^#+ ` だけで切ると、§7.2 の yaml ブロック中の `# ==========` をコメントではなく
# 見出しと見なしてしまい、節が途中で切れる。
_NEXT_HEADING = r"(?=^#{1,6} (?:\d|付録)|\Z)"

_HEADING_RE = re.compile(r"^#{1,6} (?:\d|付録)")
_FENCE_RE = re.compile(r"^\s*```")


def _section(heading: str) -> str:
    r"""`### <heading>` から次の見出しまでの本文を返す。

    **コードフェンスの中は見出しとして扱わない。**正規表現だけで切っていた頃は、
    ブロック内の `# 2026-09-12 に …` のような**数字で始まるコメント**が
    `^#{1,6} \d` に一致し、**節が途中で切れていた**（§18.6 の bash ブロックが
    読めなくなっていた。#13 で発覚）。`_NEXT_HEADING` は他のヘルパが使うので残す。
    """
    text = spec_text()
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("#") and line.lstrip("#").startswith(f" {heading}"):
            start = index + 1
            break
    if start is None:
        pytest.fail(f"SPEC に見出し {heading!r} が見つかりません")
    in_fence = False
    for index in range(start, len(lines)):
        line = lines[index]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and _HEADING_RE.match(line):
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


def spec_error_codes() -> dict[str, str]:
    """§15.1 の表から「エラーコード → リトライ列の原文」を返す。"""
    rows = re.findall(
        r"^\| `([A-Z][A-Z_]*)` \| ([^|]*?) \| ([^|]*?) \| ([^|]*?) \| ([^|]*?) \|",
        _section("15.1"),
        re.M,
    )
    codes = {code: retry.strip() for code, _cat, _where, retry, _next in rows}
    if not codes:
        pytest.fail("SPEC §15.1 の表を読み取れませんでした")
    return codes


def spec_error_categories() -> dict[str, str]:
    """§15.1 の表から「エラーコード → 分類列の原文」を返す。"""
    rows = re.findall(
        r"^\| `([A-Z][A-Z_]*)` \| ([^|]*?) \|",
        _section("15.1"),
        re.M,
    )
    return {code: category.strip() for code, category in rows}


def spec_startup_aborting_codes() -> set[str]:
    """§15.1 の表で遷移先が「起動中止」であるエラーコード。"""
    rows = re.findall(
        r"^\| `([A-Z][A-Z_]*)` \| [^|]*? \| [^|]*? \| [^|]*? \| ([^|]*?) \|",
        _section("15.1"),
        re.M,
    )
    return {code for code, next_state in rows if "起動中止" in next_state}


def spec_error_next_states() -> dict[str, str]:
    """§15.1 の表から「エラーコード → 遷移先列の原文」を返す。"""
    rows = re.findall(
        r"^\| `([A-Z][A-Z_]*)` \| [^|]*? \| [^|]*? \| [^|]*? \| ([^|]*?) \|",
        _section("15.1"),
        re.M,
    )
    if not rows:
        pytest.fail("SPEC §15.1 の遷移先列を読み取れませんでした")
    return {code: next_state.strip() for code, next_state in rows}


def spec_event_names() -> list[str]:
    """§16.4 のコードブロックからイベント名を出現順に返す。"""
    m = re.search(r"```text\n(.*?)\n```", _section("16.4"), re.S)
    if m is None:
        pytest.fail("SPEC §16.4 のコードブロックを読み取れませんでした")
    return [name.strip() for name in re.split(r"[/\n]", m.group(1)) if name.strip()]


def spec_body_event_mentions() -> dict[str, list[int]]:
    """本文（§16.4 より前）が `LEVEL event_name` の形で出力を指示している名前を返す。

    **v5.0 で §16.4 を 68 件から 28 件へ削ったとき、本文がまだ名前を指示している
    2 件（`already_known` / `audio_probe_failed`）が一覧から落ちた。**`log.py` は
    未登録のイベント名に `ValueError` を投げるので、**これは文書の食い違いではなく
    「書けば必ず落ちるコード」である。**3 版にわたって誰も気づかなかった
    （v5.2→v5.3 の変更 O-5）。

    戻り値は `{イベント名: [SPEC の行番号, ...]}`。
    """
    text = spec_text()
    body = text[: text.index("### 16.4")]
    found: dict[str, list[int]] = {}
    for match in re.finditer(r"`?(?:DEBUG|INFO|WARN|ERROR)[ \t]+([a-z][a-z0-9_]{4,})`?", body):
        # **同じ行に限る。**`logging:` の設定例は `level: INFO` の次の行が `format: text`
        # なので、改行をまたぐと "INFO format" を拾ってしまう
        found.setdefault(match.group(1), []).append(body[: match.start()].count("\n") + 1)
    return found


def spec_tree_modules() -> set[str]:
    """§6 の木が列挙している `src/voicedock/` のモジュール名（`.py` を除く）。

    **木は「どこに何があるか」の唯一の索引である。**古いと読み手が存在しないファイルを
    探す。v5.3 まで 6 モジュール（`heartbeat` / `discover` / `raw` / `wiki` / `status` /
    `health`）が載っていなかった（v5.3→v5.4 の変更 P-3）。

    **`src/voicedock/` の範囲に限って拾う。**木には `tests/` や `poc/` の `.py` も
    並ぶので、全体から正規表現で集めると別の層のファイルが混ざる。
    """
    text = spec_text()
    start = text.index("## 6. ")
    block = text[start : text.index("## 7. ", start)]
    src_start = block.index("voicedock/")
    src_block = block[src_start : block.index("config/", src_start)]
    return set(re.findall(r"([a-z_]+)\.py", src_block))


def spec_subcommands() -> list[str]:
    """§17.1 の 1 つめの表（残すサブコマンド）から名前を出現順に返す。

    **v5.0 まで `tests/unit/test_cli.py` はこの一覧を直書きしていた。**SPEC の表に
    サブコマンドを 1 行足しても何も落ちず、実装と食い違ったまま通ってしまう穴があった。
    """
    head, sep, _tail = _section("17.1").partition("**v5.0 で削除した")
    if not sep:
        pytest.fail("SPEC §17.1 の「残す」表と「削除した」表の境目が見つかりません")
    found = re.findall(r"^\| `([a-z][a-z-]*)` \| ", head, re.M)
    if not found:
        pytest.fail("SPEC §17.1 のサブコマンド表を読み取れませんでした")
    return found


def spec_removed_subcommands() -> list[str]:
    """§17.1 の 2 つめの表（v5.0 で削除したサブコマンド）から名前を出現順に返す。"""
    _head, sep, tail = _section("17.1").partition("**v5.0 で削除した")
    if not sep:
        pytest.fail("SPEC §17.1 の「残す」表と「削除した」表の境目が見つかりません")
    found = re.findall(r"^\| `([a-z][a-z-]*)` \| ", tail, re.M)
    if not found:
        pytest.fail("SPEC §17.1 の削除済みサブコマンド表を読み取れませんでした")
    return found


def spec_exit_codes() -> dict[int, str]:
    """§17.3 の表から「終了コード → 意味」を返す。"""
    rows = re.findall(r"^\| `(\d+)` \| ([^|]*?) \|", _section("17.3"), re.M)
    if not rows:
        pytest.fail("SPEC §17.3 の表を読み取れませんでした")
    return {int(code): meaning.strip() for code, meaning in rows}


def spec_log_text_examples() -> dict[str, str]:
    """§16.2 の text 例から「イベント名 → タイムスタンプ以降の全文」を返す。

    タイムスタンプは実行時刻になるため落とす。残り（レベル・イベント名・全フィールド）は
    実装の出力とバイト単位で一致しなければならない。
    """
    m = re.search(r"```text\n(.*?)\n```", _section("16.2"), re.S)
    if m is None:
        pytest.fail("SPEC §16.2 の text 例を読み取れませんでした")
    examples: dict[str, str] = {}
    for line in m.group(1).splitlines():
        _ts, rest = line.split(" ", 1)
        examples[rest.split()[1]] = rest
    return examples


def spec_log_json_example() -> str:
    """§16.2 の json 例の 1 行。"""
    m = re.search(r"```json\n(.*?)\n```", _section("16.2"), re.S)
    if m is None:
        pytest.fail("SPEC §16.2 の json 例を読み取れませんでした")
    return m.group(1).strip()


def spec_config_example() -> dict[str, object]:
    """§7.2 の yaml ブロックを safe_load して返す（`config.example.yaml` の正）。"""
    m = re.search(r"```yaml\n(.*?)\n```", _section("7.2"), re.S)
    if m is None:
        pytest.fail("SPEC §7.2 の yaml ブロックを読み取れませんでした")
    document = yaml.safe_load(m.group(1))
    if not isinstance(document, dict):
        pytest.fail("SPEC §7.2 の yaml ブロックがマッピングになっていません")
    return document


def spec_skip_reasons() -> set[str]:
    """本文が `reason=<name>` として名指しする理由（§5.3 / §10.2 / §16.4 / §20.1）。

    **`reason=` の値は実装の定数ではなく SPEC の語である。**定数を持っているだけだと、
    値を書き換えてもテストが一緒に動いて通ってしまう（実際に通った）。
    """
    found = set(re.findall(r"reason=([a-z_]+)", spec_text()))
    if not found:
        pytest.fail("SPEC の reason= を読み取れませんでした")
    return found


def spec_unit_test_targets() -> list[str]:
    """§20.1 の表の「対象」列を出現順に返す。

    **件数のずれが最も危ない**（#55 の振り返り）。SPEC に 1 行足して実装を忘れると、
    **検査が足りないまま「完了」になる。**`docs/TEST_COVERAGE.md` と突き合わせる。
    """
    rows = re.findall(r"^\| (.+?) \| .+? \|$", _section("20.1"), re.M)
    targets = [row.strip() for row in rows if row.strip() not in {"対象", "---"}]
    if not targets:
        pytest.fail("SPEC §20.1 の表を読み取れませんでした")
    return targets


def spec_rule_ids(section: str, prefix: str) -> list[str]:
    """`### <section>` の表から `<prefix>-n` の規則 ID を出現順に返す。

    **件数のずれが最も危ない。**#55 の振り返りのとおり、名前の変更は grep で見つかるが
    **件数の変更は本文を読まないと分からない**。SPEC に規則を 1 行足して実装を
    忘れると、**検査が足りないまま「完了」になる。**
    """
    found: list[str] = re.findall(
        rf"^\| \*{{0,2}}({re.escape(prefix)}-\d+)\*{{0,2}} \|", _section(section), re.M
    )
    if not found:
        pytest.fail(f"SPEC §{section} の {prefix}-* を読み取れませんでした")
    return found


def spec_section_text(section: str) -> str:
    """`### <section>` の本文をそのまま返す。

    **表にもコードブロックにも入っていない規定を固定するために使う。**§10.4 の
    「再オープンの契機」のように、散文でしか書けない規則がある。
    """
    return _section(section)


def spec_section_code(section: str, language: str, index: int = 0) -> str:
    """`### <section>` の `index` 番目の ```<language> ブロックを返す。

    実装が SPEC のコードブロックの**写し**であることを固定するために使う
    （§5.2 の正規表現など）。写しであることをテストで縛らないと、
    **SPEC と実装が静かに食い違う。**
    """
    found: list[str] = re.findall(rf"```{language}\n(.*?)\n```", _section(section), re.S)
    if len(found) <= index:
        pytest.fail(
            f"SPEC §{section} に {language} ブロックが {index + 1} 個ありません（{len(found)} 個）"
        )
    return found[index]


def spec_helper_conf() -> str:
    """§7.4 の 1 つめの ```sh ブロック（`helper/helper.example.conf` の正）。

    §7.2 と `config/config.example.yaml` の関係と同じである。**Helper の設定は
    `helper.conf` にしか無い**ので（§7.4）、SPEC と example が食い違うと
    「ユーザーが手で書く唯一の設定」が壊れる。
    """
    found: list[str] = re.findall(r"```sh\n(.*?)\n```", _section("7.4"), re.S)
    if not found:
        pytest.fail("SPEC §7.4 の sh ブロックを読み取れませんでした")
    return found[0]


def spec_json_block(section: str, index: int = 0) -> dict[str, object]:
    """`### <section>` の `index` 番目の ```json ブロックを parse して返す。"""
    found = re.findall(r"```json\n(.*?)\n```", _section(section), re.S)
    if len(found) <= index:
        pytest.fail(
            f"SPEC §{section} に json ブロックが {index + 1} 個ありません（{len(found)} 個）"
        )
    document = json.loads(found[index])
    if not isinstance(document, dict):
        pytest.fail(f"SPEC §{section} の json ブロック {index} がマッピングではありません")
    return document


def spec_validation_rules() -> dict[str, str]:
    """§7.3 の表から「規則 ID → 規則本文」を出現順に返す。

    **打ち消し（`~~`）の行は除く。**v4.1 で V-5 / V-6、v4.2 で V-28 を廃止しており、
    廃止された規則を実装することはない（番号は詰めない）。
    """
    rows = re.findall(r"^\| (V-\d+) \| ([^|]*?) \| ([^|]*?) \|", _section("7.3"), re.M)
    rules = {rule: body.strip() for rule, body, _error in rows if not body.strip().startswith("~~")}
    if not rules:
        pytest.fail("SPEC §7.3 の表を読み取れませんでした")
    return rules


def spec_schema_sql() -> list[str]:
    """§8.2 / §8.3 / §8.4 / §8.5 の ```sql ブロックを出現順に返す。

    これを空の DB へ流したものが**スキーマの正**である。マイグレーションで作った構造と
    `PRAGMA` レベルで突き合わせることで、SPEC に列を足して写し忘れる事故が落ちる。
    """
    blocks: list[str] = []
    for section in ("8.2", "8.3", "8.4", "8.5"):
        found = re.findall(r"```sql\n(.*?)\n```", _section(section), re.S)
        if not found:
            pytest.fail(f"SPEC §{section} の sql ブロックを読み取れませんでした")
        blocks += found
    return blocks


def spec_retry_reset_statuses() -> set[str]:
    """§15.2 の「工程を通過したら 0 にリセットする」で名指しされた状態。"""
    matched = re.search(
        r"`retry_count` は「現在の工程での連続失敗回数」を意味する.*?（(.*?)への遷移時）",
        _section("15.2"),
        re.S,
    )
    if matched is None:
        pytest.fail("SPEC §15.2 の retry_count リセット規則を読み取れませんでした")
    return set(re.findall(r"`([A-Z_]+)`", matched.group(1)))


# --- §9 の状態機械 -------------------------------------------------------

_TRANSITION_SKIP_SOURCES: Final = frozenset({"現状態", "---", "—", "各工程通過"})


def spec_retry_settings() -> dict[str, object]:
    """§15.2 冒頭の `retry:` ブロック（`max_attempts` / `backoff_seconds`）。

    **`config.example.yaml` との一致は V-* が見ているが、規定そのものは §15.2 にある。**
    ここを読むことで「SPEC の数字を変えたのにテストが通る」を防ぐ。
    """
    document = yaml.safe_load(spec_section_code("15.2", "yaml"))
    if not isinstance(document, dict) or "retry" not in document:
        pytest.fail("SPEC §15.2 の retry ブロックを読み取れませんでした")
    retry = document["retry"]
    if not isinstance(retry, dict):
        pytest.fail("SPEC §15.2 の retry ブロックが辞書ではありません")
    return retry


def spec_delete_evaluation_backoff() -> list[int]:
    """§15.2 の `delete_evaluation_backoff_seconds`（`SAVED` の再評価間隔）。"""
    matched = re.search(
        r"`cleanup\.delete_evaluation_backoff_seconds`（`\[([\d, ]+)\]`）", _section("15.2")
    )
    if matched is None:
        pytest.fail("SPEC §15.2 の delete_evaluation_backoff_seconds を読み取れませんでした")
    return [int(value) for value in matched.group(1).split(",")]


def spec_max_attempts_exempt_codes() -> set[str]:
    """§15.2 が「`max_attempts` の対象外」と名指しするエラーコードと状態。

    **工程内リトライで回してはならないもの**であり、契機（Helper の復帰 / デバイスの
    再接続）を待って無期限に再評価する。
    """
    codes: set[str] = set()
    for line in _section("15.2").splitlines():
        head, marker, _ = line.partition("は `max_attempts` の対象外")
        if marker:
            codes.update(re.findall(r"`([A-Z_]+)`", head))
    if not codes:
        pytest.fail("SPEC §15.2 の max_attempts 対象外を読み取れませんでした")
    return codes


def spec_retry_count_is_unchanged_on_retry() -> list[str]:
    """§9.3 の「工程内リトライ」行が `retry_count` をどう書いているか（S-1）。

    **増やす場所は `FAILED` への遷移だけである。**両方で増やすと二重に数え、
    `max_attempts: 3` で実際には 4 回試すことになる。
    """
    found = re.findall(r"^\| `FAILED` \| 工程内リトライ \|.*\| (.*?) \|$", spec_text(), re.M)
    if len(found) != 2:
        pytest.fail(f"§9.3 の工程内リトライ行が 2 行ありません（{len(found)} 行）")
    return found


def spec_states(section: str) -> list[str]:
    """§9.1 / §9.2 の状態表から状態名を出現順に返す。"""
    found = re.findall(r"^\| `([A-Z_]+)` \|", _section(section), re.M)
    if not found:
        pytest.fail(f"SPEC §{section} の状態表を読み取れませんでした")
    return found


def spec_mermaid_edges(section: str) -> set[tuple[str, str]]:
    """§9.1 / §9.2 の mermaid 図の辺。`[*] -->`（初期状態）は含めない。"""
    m = re.search(r"```mermaid\n(.*?)\n```", _section(section), re.S)
    if m is None:
        pytest.fail(f"SPEC §{section} の mermaid を読み取れませんでした")
    return {
        (source, target)
        for source, target in re.findall(
            r"^\s*(\[\*\]|[A-Z_]+)\s*-->\s*([A-Z_]+)", m.group(1), re.M
        )
        if source != "[*]"
    }


def spec_initial_state(section: str) -> str:
    """`[*] --> X` の X。"""
    m = re.search(r"```mermaid\n(.*?)\n```", _section(section), re.S)
    if m is None:
        pytest.fail(f"SPEC §{section} の mermaid を読み取れませんでした")
    found: list[str] = re.findall(r"^\s*\[\*\]\s*-->\s*([A-Z_]+)", m.group(1), re.M)
    if len(found) != 1:
        pytest.fail(f"SPEC §{section} の初期状態が 1 つに定まりません: {found}")
    return found[0]


def spec_transition_edges(entity: str) -> set[tuple[str, str]]:
    """§9.3 の遷移表を辺集合へ正規化する。

    `entity` は `"part"` または `"session"`。次の行は辺にしない。

    - 現状態が `—`（行の作成）または `各工程通過`（`retry_count` の注記）
    - 次状態が `（遷移なし）`
    - 次状態が `X のまま`（§9.3 の注記どおり**遷移ではない**）

    現状態の `` `A` / `B` `` は両方へ展開し、次状態の `直前の進行中状態（...）` は
    括弧内の状態名へ展開する。
    """
    body = _section("9.3")
    if "**Session:**" not in body:
        pytest.fail("SPEC §9.3 の Part / Session の境目が見つかりません")
    part_chunk, session_chunk = body.split("**Session:**", 1)
    chunk = {"part": part_chunk, "session": session_chunk}[entity]

    edges: set[tuple[str, str]] = set()
    for source, _event, _guard, target, _effect in re.findall(
        r"^\| (.+?) \| (.+?) \| (.+?) \| (.+?) \| (.+?) \|$", chunk, re.M
    ):
        if source in _TRANSITION_SKIP_SOURCES or target == "（遷移なし）" or "のまま" in target:
            continue
        for start in re.findall(r"`([A-Z_]+)`", source):
            for end in re.findall(r"`([A-Z_]+)`", target):
                edges.add((start, end))
    if not edges:
        pytest.fail(f"SPEC §9.3 の {entity} 表を読み取れませんでした")
    return edges


def spec_recovery_map() -> list[tuple[str, str]]:
    """§9.4 の巻き戻し表（`A -> B` のコードブロック）。"""
    m = re.search(r"```text\n((?:\w+\s+->\s+\w+.*\n)+)```", _section("9.4"))
    if m is None:
        pytest.fail("SPEC §9.4 の巻き戻し表を読み取れませんでした")
    return re.findall(r"^(\w+)\s+->\s+(\w+)", m.group(1), re.M)


def spec_status_tuple(name: str) -> list[str]:
    """§14.1 のコードブロックにある `NAME = (...)` の中身。"""
    m = re.search(rf"^{re.escape(name)} = \((.*?)\)$", _section("14.1"), re.S | re.M)
    if m is None:
        pytest.fail(f"SPEC §14.1 に {name} が見つかりません")
    return re.findall(r'"([A-Z_]+)"', m.group(1))


def spec_part_terminal_paragraph() -> set[str]:
    """§9.1 の「**終端状態**（Session の進行判定に使う）」の段落から状態名を取る。"""
    m = re.search(r"\*\*終端状態\*\*[^:：]*[:：]\s*(.+)", _section("9.1"))
    if m is None:
        pytest.fail("SPEC §9.1 の終端状態の段落を読み取れませんでした")
    return set(re.findall(r"`([A-Z_]+)`", m.group(1)))
