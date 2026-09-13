"""CLI のパーサ定義とディスパッチ（SPEC §17.1, §17.3）。

設定の読み込みは `config.py` に任せ、**このモジュールは環境変数を読まない。**
SPEC §7 は「設定の出所を二重にしない」と規定しており、ここで env を直読みすると
`config.yaml` と出所が二重になる。
"""

from __future__ import annotations

import argparse
import sys

from voicedock import __version__, db, doctor, health, status, worker
from voicedock.config import ConfigError, load_config, logger_for, startup_notices
from voicedock.errors import EXIT_CONFIG, EXIT_ERROR, EXIT_OK

# --- サブコマンド（SPEC §17.1） ------------------------------------------
# 未実装のものも --help に並べ、呼ばれたら EXIT_ERROR を返す（IMPLEMENTED が唯一の出所）。
SUBCOMMANDS: dict[str, str] = {
    "service": "常駐サービスを起動する（コンテナの既定 CMD）",
    "status": "現在の処理状況サマリを表示する",
    "doctor": "環境診断（§19.2）",
    "health": "health check（§19.1）",
    "version": "バージョンを表示する",
}
"""SPEC §17.1 の全サブコマンド。**引数を取るものは無い。**

v5.0 で `scan` / `history` / `show` / `retry` / `pending` / `cleanup` を削除した
（v4.6→v5.0 の変更 L-4）。
**`health` は `compose.yaml` の healthcheck が呼ぶので消せない。**
"""

IMPLEMENTED: frozenset[str] = frozenset({"doctor", "health", "service", "status", "version"})

UNIMPLEMENTED_MESSAGE = "voicedock: サブコマンド '{name}' は未実装です（SPEC §17.1）。"


def build_parser() -> argparse.ArgumentParser:
    """SPEC §17.1 の全サブコマンドを登録したパーサを返す。"""
    parser = argparse.ArgumentParser(
        prog="voicedock",
        description="DJI Mic 3 の録音をローカル完結で文字起こし・要約し Obsidian へ保存する",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", metavar="<subcommand>")

    for name, help_text in SUBCOMMANDS.items():
        sub.add_parser(name, help=help_text, description=help_text)

    return parser


def dispatch(args: argparse.Namespace) -> int:
    """パース済み引数を実行する。戻り値がプロセスの終了コードになる。"""
    command: str | None = args.command

    if command is None:
        build_parser().print_help()
        return EXIT_ERROR

    if command == "version":
        print(__version__)
        return EXIT_OK

    if command == "doctor":
        return doctor.run()

    if command == "health":
        return health.run()

    if command == "status":
        return status.run()

    if command == "service":
        return _service()

    print(UNIMPLEMENTED_MESSAGE.format(name=command), file=sys.stderr)
    return EXIT_ERROR


def _service() -> int:
    """常駐サービス（§10.0 の `worker_loop`）。

    設定を読み、DB を開き、`worker.Worker` を回す。**SIGTERM で `service_stopping` を
    出して安全に止まる**（`tini` が転送する。§18.3）。
    """
    try:
        cfg = load_config()
    except ConfigError as e:
        # 設定が読めていない時点では logging.format / level / timezone が分からず、
        # 構造化ログを組み立てられない。stderr のプレーンテキストで出す。
        print(
            f"voicedock: 設定エラー（SPEC §7.3）。起動を中止します: {e.path}",
            file=sys.stderr,
        )
        for violation in e.violations:
            print(f"  {violation.render()}", file=sys.stderr)
        return EXIT_CONFIG

    log = logger_for(cfg)
    for notice in startup_notices(cfg):
        log.warning("config_warning", rule=notice.rule, message=notice.message)

    # スキーマは起動時に自動適用する（SPEC §8.5）。未適用の版が無ければ何も書かない
    with db.connect(
        cfg.database.path, busy_timeout_ms=cfg.database.busy_timeout_ms, tz=cfg.tz
    ) as database:
        worker.Worker(cfg=cfg, log=log, database=database).run()
    return EXIT_OK
