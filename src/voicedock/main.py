"""エントリポイント（SPEC §17.1）。

`[project.scripts]` の wrapper と healthcheck の `python -m voicedock.main` の両方から呼ばれる。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from voicedock.cli import build_parser, dispatch


def main(argv: Sequence[str] | None = None) -> int:
    """終了コードを返す。呼び出し側が sys.exit() する。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
