"""Helper からコンテナへの一方向報告（SPEC §7.5）。

`state/` は `/state:ro` でマウントされる。**コンテナは書き込めない。**コンテナが自分に
都合よく書き換えて削除判断を通す経路を作らないためである（§14.4）。

**このモジュールは例外を投げない。**Helper の書き込み途中を読む可能性があるため、
壊れた JSON・欠落したキーはすべて「不明」として扱う。**不明は常に安全側へ倒す**
（＝削除しない / 警告する）。

`config.yaml`（`extra="forbid"`）と違って **未知のキーは無視する。**`config.yaml` は
人が書くのでタイポが問題になるが、`heartbeat.json` は Helper が書くので
**フィールドが増えてもコンテナが壊れないこと**が優先される（§7.5）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_STATE_ROOT: Final = Path("/state")
HEARTBEAT_FILENAME: Final = "heartbeat.json"


class Heartbeat(BaseModel):
    """`state/heartbeat.json` の内容（SPEC §7.5）。

    **全フィールドを省略可能にする。**Helper の版が古くてフィールドを書いていない場合を
    「不明」として区別できるようにするためで、既定値で埋めてはならない。
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    # `schema` は BaseModel の予約名と衝突するため別名にする
    schema_version: int | None = Field(default=None, alias="schema")
    updated_at: datetime | None = None
    helper_version: str | None = None
    mount_mode: str | None = None
    mount_readonly: bool | None = None
    delete_source_audio: bool | None = None
    reaper_installed: bool | None = None
    device_free_bytes: int | None = None
    """接続中デバイスの空き容量（§17.2 の `Device free space`）。

    **コンテナはデバイスに到達できない**（§14.4 N-3）ので、自分で測る経路は無い。
    Helper が報告しなければ `None`（`status` は `unknown` と出す）。
    """

    include_volumes: tuple[str, ...] = ()
    exclude_volumes: tuple[str, ...] = ()
    config_error: str | None = None

    def age_seconds(self, now: datetime) -> float | None:
        """報告からの経過秒。`updated_at` が無ければ `None`。"""
        if self.updated_at is None:
            return None
        return (now - self.updated_at).total_seconds()

    def is_stale(self, now: datetime, max_age_seconds: int) -> bool:
        """報告が古いか。**`updated_at` が無ければ古い扱いにする**（不明は安全側）。"""
        age = self.age_seconds(now)
        return age is None or age > max_age_seconds


def read_heartbeat(state_root: Path = DEFAULT_STATE_ROOT) -> Heartbeat | None:
    """`state/heartbeat.json` を読む。読めなければ `None`。

    `None` を返すのは、ファイルが無い / 読めない / JSON として壊れている /
    トップレベルが辞書でない / 型が合わない場合である。**いずれも例外にしない**（§7.5）。
    """
    path = state_root / HEARTBEAT_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Heartbeat.model_validate(data)
    except ValidationError:
        return None
