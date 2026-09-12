"""CLI のパーサ定義とディスパッチ（SPEC §17.1, §17.3）。

このモジュールは設定を読まない。SPEC §7 は「設定の出所を二重にしない」と規定しており、
config 読み込みを前倒しすると環境変数の直読みが混入する。設定は #9（config.py）が担う。
"""

from __future__ import annotations

import argparse
import signal
import sys

from voicedock import __version__
from voicedock.errors import EXIT_ERROR, EXIT_OK

# --- サブコマンド（SPEC §17.1） ------------------------------------------
# 実装済みは version のみ。残りは --help に並べ、呼ばれたら EXIT_ERROR を返す。
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

IMPLEMENTED: frozenset[str] = frozenset({"service", "version"})

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

    if command == "service":
        return _service()

    print(UNIMPLEMENTED_MESSAGE.format(name=command), file=sys.stderr)
    return EXIT_ERROR


def _service() -> int:
    """常駐サービス。

    TODO(#19): SPEC §10.0 の worker_loop() に置き換える。
    現時点ではパイプラインが存在しないため、何も処理せずコンテナを常駐させるだけである。

    即座に終了すると compose の `restart: unless-stopped` によりクラッシュループになり、
    `docker compose exec` による確認が一切できなくなる。そのため待機する。
    実装するときは、この関数の中身だけを差し替えること。
    """
    print(
        "voicedock: service は未実装です（SPEC §10.0 / #19）。"
        "パイプラインは何も処理しません。コンテナを常駐させるためだけに待機します。",
        flush=True,
    )
    signal.pause()  # SIGTERM は tini が転送する（§18.3）
    return EXIT_OK
