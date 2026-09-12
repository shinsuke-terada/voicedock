"""コンテナ healthcheck（SPEC §19.1 H-1〜H-9）。

`compose.yaml` が 120 秒ごとに `python -m voicedock.main health` を呼ぶ（§18.2）。
**軽量・高速であること**が要件で、`doctor`（§19.2）とは目的が違う。

| | `health`（§19.1） | `doctor`（§19.2） |
|---|---|---|
| 呼ぶ人 | Docker が 120 秒ごと | 人が必要なときに |
| 所要 | 数十 ms | 時間をかけてよい |
| 副作用 | **一切持たない** | 一時ファイルの作成・削除で検証する（D-3 / D-13） |
| LLM | **実リクエストしない** | 実際に投げる（D-12） |
| 出力 | 1 行 1 検査。失敗は終了コード 3 | 整形した表 |

**`doctor` と実装を共有しない。**`doctor` は診断のために実書き込みを行うが、
`health` は 120 秒ごとに走るので **Vault に一時ファイルを撒いてはならない。**
同じ関数にすると、どちらかの要件が静かに破られる。

**DJI Mic が未接続であることを unhealthy にしてはならない**（§19.1）。
通常はデバイスが接続されていない状態が正常である。

**逆に H-8（Helper の停止）は unhealthy にする。**Helper が止まると
**録音は 1 本も取り込まれないのに、コンテナは正常に見え続ける**（§22 R-23）。
無人稼働が前提である以上、この沈黙は `docker ps` の `unhealthy` として
見えなければならない。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Final

from voicedock import db, paths
from voicedock.config import Config, ConfigError, ConfigUnreadable, load_config
from voicedock.errors import EXIT_OK, EXIT_UNHEALTHY
from voicedock.heartbeat import DEFAULT_STATE_ROOT, read_heartbeat


@dataclass(frozen=True)
class Result:
    """1 検査の結果。"""

    check: str
    """`H-2` などの規則 ID（§19.1）。**番号は SPEC と一致させる。**"""

    ok: bool
    detail: str

    def render(self) -> str:
        mark = "ok" if self.ok else "FAIL"
        return f"{self.check} {mark} {self.detail}"


@dataclass(frozen=True)
class Context:
    """検査が使う入力。テストが差し替える。"""

    cfg: Config
    state_root: Path
    now: datetime


# --- 各検査（§19.1） ----------------------------------------------------
# H-1（Python プロセスが生存している）は、このプログラムが動いていることで満たされる。
# 明示的な検査を持たないが、**番号を飛ばさず結果に出す** — H-2 から始まると
# 「H-1 はどこへ行った」と読む人が仕様を疑うことになる。


def check_process(_ctx: Context) -> Result:
    return Result("H-1", True, "process alive")


def check_database(ctx: Context) -> Result:
    """H-2: SQLite へ接続でき `schema_version` を読める。

    **接続する前に存在を確かめる。**`sqlite3.connect()` は `migrate=False` でも
    **空のファイルを作ってしまう。**120 秒ごとに走る検査が `/data` にファイルを
    置くのは副作用であり、かつ「DB が無い」ことを報告できなくなる
    （`doctor` D-2 が同じ順序で書かれている）。
    """
    path = ctx.cfg.database.path
    if not path.is_file():
        return Result("H-2", False, f"database is missing: {path}")
    try:
        with db.connect(
            path,
            busy_timeout_ms=ctx.cfg.database.busy_timeout_ms,
            tz=ctx.cfg.tz,
            migrate=False,
        ) as database:
            version = database.schema_version()
    except (sqlite3.Error, OSError, RuntimeError) as error:
        return Result("H-2", False, f"database unreadable: {type(error).__name__}")
    if version is None:
        return Result("H-2", False, "schema_version is missing (migration not applied)")
    return Result("H-2", True, f"schema v{version}")


def check_data_volume(_ctx: Context) -> Result:
    """H-3: `/data` が書き込み可能。"""
    return _access("H-3", paths.DATA_ROOT, write=True)


def check_vault(_ctx: Context) -> Result:
    """H-4: `/obsidian` が読み書き可能。

    **`os.access` に留める。**120 秒ごとに走るので、実書き込みで検証すると
    **Vault に一時ファイルを撒き続ける。**実書き込みの検証は `doctor` D-13 が担う。
    """
    return _access("H-4", paths.VAULT_ROOT, write=True)


def check_whisper_executable(ctx: Context) -> Result:
    """H-5: `transcription.executable` が存在し実行可能。"""
    path = ctx.cfg.transcription.executable
    if not path.is_file():
        return Result("H-5", False, f"missing: {path}")
    if not os.access(path, os.X_OK):
        return Result("H-5", False, f"not executable: {path}")
    return Result("H-5", True, str(path))


def check_whisper_model(ctx: Context) -> Result:
    """H-6: `transcription.model` が存在する。

    **モデル未取得は unhealthy である**（`fetch-models.sh` を実行していない状態。§18.6）。
    文字起こしが 1 本も通らないのに healthy に見えてはならない。
    """
    path = ctx.cfg.transcription.model
    if not path.is_file():
        return Result("H-6", False, f"missing: {path} (run scripts/fetch-models.sh)")
    return Result("H-6", True, str(path))


def check_llm_env(ctx: Context) -> Result:
    """H-7: `VOICEDOCK_LLM_URL` / `VOICEDOCK_LLM_MODEL` が設定されている。

    **実リクエストは行わない**（`doctor` D-12 が担う）。ここで LLM へ投げると
    120 秒ごとにモデルを起こすことになる。
    """
    missing = [
        name
        for name in (ctx.cfg.llm.endpoint_env, ctx.cfg.llm.model_env)
        if not os.environ.get(name)
    ]
    if missing:
        return Result("H-7", False, f"unset: {', '.join(missing)}")
    return Result("H-7", True, "endpoint and model are set")


def check_helper(ctx: Context) -> Result:
    """H-8: `state/heartbeat.json` が `helper_heartbeat_max_age_seconds` より新しい。

    **これだけは「デバイス未接続」と違って unhealthy にする。**Helper が止まると
    録音は 1 本も取り込まれないのに、コンテナは正常に見え続ける（§22 R-23）。
    **症状が「何も起きない」なので、検出しなければ誰も気づかない。**
    """
    max_age = ctx.cfg.import_.helper_heartbeat_max_age_seconds
    heartbeat = read_heartbeat(ctx.state_root)
    if heartbeat is None:
        return Result("H-8", False, "heartbeat.json is missing or unreadable")
    if heartbeat.is_stale(ctx.now, max_age):
        age = heartbeat.age_seconds(ctx.now)
        shown = "unknown" if age is None else f"{age:.0f}s"
        return Result("H-8", False, f"helper heartbeat is stale (age {shown} > {max_age}s)")
    if heartbeat.config_error:
        # **設定エラーも unhealthy にする。**Helper は動いているが取り込みを行わない
        # 状態であり（§7.4）、H-8 の目的（沈黙の検出）はこちらも捕まえるべきである
        return Result("H-8", False, f"helper config_error: {heartbeat.config_error}")
    return Result("H-8", True, f"helper alive (v{heartbeat.helper_version or '?'})")


def check_queues(_ctx: Context) -> Result:
    """H-9: `/inbox` と `/queue` が読み書き可能。"""
    for root in (paths.INBOX_ROOT, paths.QUEUE_ROOT):
        result = _access("H-9", root, write=True)
        if not result.ok:
            return result
    return Result("H-9", True, f"{paths.INBOX_ROOT} and {paths.QUEUE_ROOT} are writable")


CHECKS: Final[tuple[Callable[[Context], Result], ...]] = (
    check_process,
    check_database,
    check_data_volume,
    check_vault,
    check_whisper_executable,
    check_whisper_model,
    check_llm_env,
    check_helper,
    check_queues,
)
"""§19.1 の H-1〜H-9 をこの順に実行する。**`tests/unit/test_health.py` が SPEC と突き合わせる。**"""


def _access(check: str, root: Path, *, write: bool) -> Result:
    """`os.access` による読み書き可否。**一時ファイルを作らない。**"""
    if not root.is_dir():
        return Result(check, False, f"not a directory: {root}")
    if not os.access(root, os.R_OK):
        return Result(check, False, f"not readable: {root}")
    if write and not os.access(root, os.W_OK):
        return Result(check, False, f"not writable: {root}")
    return Result(check, True, str(root))


def evaluate(ctx: Context) -> list[Result]:
    """全検査を実行する。**1 つが失敗しても残りを実行する。**

    最初の失敗で打ち切ると、`docker logs` に出る情報が 1 行だけになる。
    120 秒ごとの検査で全体像が毎回見えることに価値がある。
    """
    return [check(ctx) for check in CHECKS]


def run(
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    now: datetime | None = None,
    out: IO[str] | None = None,
) -> int:
    """`voicedock health` の本体。失敗があれば終了コード 3（§17.3）。

    **設定が読めない場合も unhealthy（3）にする。**§17.3 の 2（設定エラー）は
    起動時の中止に使う番号であり、healthcheck の文脈では Docker が見る値は
    「0 か否か」だけである。**3 に統一して「不健康」として扱う。**
    """
    stream = out if out is not None else sys.stdout
    try:
        cfg = load_config()
    except (ConfigError, ConfigUnreadable) as error:
        print(f"H-0 FAIL config unreadable: {error}", file=stream)
        return EXIT_UNHEALTHY

    moment = now if now is not None else datetime.now(tz=cfg.tz)
    results = evaluate(Context(cfg=cfg, state_root=state_root, now=moment))
    _write(stream, results)
    return EXIT_OK if all(result.ok for result in results) else EXIT_UNHEALTHY


def _write(out: IO[str], results: Sequence[Result]) -> None:
    for result in results:
        print(result.render(), file=out)
    failed = [result.check for result in results if not result.ok]
    if failed:
        print(f"unhealthy: {', '.join(failed)}", file=out)
    else:
        print(f"healthy: {len(results)} checks passed", file=out)
