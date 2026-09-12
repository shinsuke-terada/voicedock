"""CLI のパーサ定義とディスパッチ（SPEC §17.1, §17.3）。

設定の読み込みは `config.py` に任せ、**このモジュールは環境変数を読まない。**
SPEC §7 は「設定の出所を二重にしない」と規定しており、ここで env を直読みすると
`config.yaml` と出所が二重になる。
"""

from __future__ import annotations

import argparse
import signal
import sys

from voicedock import __version__, doctor
from voicedock.config import ConfigError, load_config, logger_for, startup_notices
from voicedock.errors import EXIT_CONFIG, EXIT_ERROR, EXIT_OK

# --- サブコマンド（SPEC §17.1） ------------------------------------------
# 未実装のものも --help に並べ、呼ばれたら EXIT_ERROR を返す（IMPLEMENTED が唯一の出所）。
SUBCOMMANDS: dict[str, str] = {
    "service": "常駐サービスを起動する（コンテナの既定 CMD）",
    "scan": "走査を 1 回だけ実行して結果を表示する",
    "status": "現在の処理状況サマリを表示する",
    "history": "処理履歴を新しい順に表示する（既定 20 件）",
    "show": "セッション 1 件の詳細を表示する",
    "retry": "FAILED からの即時再開",
    "pending": "SOURCE_DELETE_PENDING と自動再試行待ちの一覧を表示する",
    "cleanup": "条件を満たした元音声の削除を要求する",
    "doctor": "環境診断（§19.2）",
    "health": "health check（§19.1）",
    "version": "バージョンを表示する",
}

IMPLEMENTED: frozenset[str] = frozenset({"doctor", "service", "version"})

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
        p = sub.add_parser(name, help=help_text, description=help_text)
        if name == "history":
            p.add_argument("--limit", type=int, default=20, help="表示件数（既定 20）")
            p.add_argument("--status", help="この状態のものだけを表示する")
        elif name == "show":
            p.add_argument("session_id", type=int, help="表示するセッション ID")
        elif name == "retry":
            p.add_argument("id", type=int, help="再試行する ID（既定は Part ID）")
            p.add_argument("--session", action="store_true", help="ID を Session ID として扱う")
            p.add_argument("--force", action="store_true", help="再試行回数の上限を無視する")
        elif name == "cleanup":
            p.add_argument("--backlog", action="store_true", help="COMPLETED の Part も対象にする")
            p.add_argument("--dry-run", action="store_true", help="対象を表示するだけで要求しない")

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

    if command == "service":
        return _service()

    print(UNIMPLEMENTED_MESSAGE.format(name=command), file=sys.stderr)
    return EXIT_ERROR


def _service() -> int:
    """常駐サービス。

    TODO(#19): SPEC §10.0 の worker_loop() に置き換える。
    現時点ではパイプラインが存在しないため、設定を検証したあと常駐するだけである。

    即座に終了すると compose の `restart: unless-stopped` によりクラッシュループになり、
    `docker compose exec` による確認が一切できなくなる。そのため待機する。
    実装するときは、待機している箇所だけを差し替えること。
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
    log.info("service_started", version=__version__)

    print(
        "voicedock: service は未実装です（SPEC §10.0 / #19）。"
        "パイプラインは何も処理しません。コンテナを常駐させるためだけに待機します。",
        flush=True,
    )
    signal.pause()  # SIGTERM は tini が転送する（§18.3）
    return EXIT_OK
