"""常駐 worker のループ（SPEC §10.0, §9.4, §15.2）。

**単一 Worker・並列処理なし**（§10.0）。並列化しない理由は CPU 負荷管理、whisper の
同時実行の回避、状態管理の単純化、**削除事故の回避**、LLM 競合の回避である。

**v4.0 以降、このループはデバイスを一切見ない。**見るのは `/inbox`（Helper が原本を置く
場所）と `/state`（Helper の報告）だけである。デバイスの検出・走査・安定性判定は
Helper が行う（§10.1）。

**Helper のハートビートが古ければ取り込みを進めない**（§10.0 / §22 R-23）。Helper が
止まっているのに「新しい録音が無い」と解釈して静かに待ち続ける状態を作らない。

`cli.py` から分けてあるのは、**`argparse` を通さずにループをテストできるようにする**ため
である（`cli.py` は §17.1 のパーサとディスパッチだけを持つ層）。
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Final

from voicedock import __version__, db, device, discover, pipeline, session
from voicedock.config import Config
from voicedock.device import DeviceInventory
from voicedock.heartbeat import DEFAULT_STATE_ROOT, read_heartbeat
from voicedock.log import Logger
from voicedock.paths import PartKey, SessionKey
from voicedock.states import PART_TERMINAL, SessionStatus

STOP_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGTERM, signal.SIGINT)
"""`tini` が転送する停止シグナル（§18.3）。"""


@dataclass
class Stopper:
    """シグナルで立つ停止フラグ。

    **ハンドラの中で何もしない。**DB を触ったりログを書いたりすると、シグナルが
    トランザクションの途中で届いたときに壊れる。**フラグを立てて、ループの区切りで止まる。**
    """

    requested: bool = False

    def request(self, _signum: int = 0, _frame: FrameType | None = None) -> None:
        self.requested = True

    def should_stop(self) -> bool:
        """フラグを読む。**属性を直接見ない。**

        シグナルハンドラが非同期に書き換えるので、`while not self.stopper.requested`
        のように属性を見ると**型検査器が「常に偽」と推論して以降を到達不能と判断する。**
        """
        return self.requested

    def install(self) -> None:
        for number in STOP_SIGNALS:
            signal.signal(number, self.request)


@dataclass
class Worker:
    """1 つのループ。**状態は `last_inventory_empty` だけ**（§15.2 の立ち上がり判定）。"""

    cfg: Config
    log: Logger
    database: db.Database
    state_root: Path = DEFAULT_STATE_ROOT
    stopper: Stopper = field(default_factory=Stopper)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] | None = None

    last_inventory_empty: bool | None = None
    """前回の周回で `inventory.devices` が空だったか。**起動直後は `None`（不明）。**"""

    def now(self) -> datetime:
        return self.clock() if self.clock is not None else datetime.now(self.cfg.tz)

    # --- 起動時（§9.4） --------------------------------------------------

    def start(self) -> None:
        """§10.0 のループに入る前の 1 回だけの処理。"""
        self.log.info(
            "service_started",
            version=__version__,
            schema_version=self.database.schema_version(),
        )
        pipeline.recover_interrupted(self.database, cfg=self.cfg, log=self.log)
        pipeline.close_stale_open_sessions(self.database, cfg=self.cfg, now=self.now())
        # **起動は §15.2 の再投入の契機である。**`last_inventory_empty` を `None` のまま
        # 1 周目へ入ることで、最初の周回で必ず再投入が走る
        self.requeue_failed(None, startup=True)

    # --- 1 周（§10.0） ---------------------------------------------------

    def tick(self) -> bool:
        """1 周回す。取り込みを進めたら真（テストと `run()` の判定に使う）。

        **順序は §10.0 の擬似コードのままである。**並べ替えてはならない — たとえば
        `discover_parts` より先に `process_pending_parts` を呼ぶと、**その周回で
        見つかった Part が 1 周遅れる。**
        """
        inventory = device.read_inventory(self.state_root)
        if self._helper_is_stale():
            # **取り込みを進めない**（§10.0 / §22 R-23）。`inventory` が読めない場合も
            # 同じ扱いにする（不明は安全側へ倒す。§7.5）
            self.log.warning("helper_heartbeat_stale", reason="取り込みを見送る")
            return False

        self.discover_parts()
        self.close_idle_sessions()
        self.process_pending_parts()
        self.process_ready_sessions()
        self.requeue_failed(inventory)
        return True

    def run(self) -> None:
        """停止が要求されるまで回す。"""
        self.stopper.install()
        self.start()
        while not self.stopper.should_stop():
            self.tick()
            if self.stopper.should_stop():
                break
            self.sleep(self.cfg.device.poll_interval_seconds)
        self.log.info("service_stopping", version=__version__)

    # --- 各段 ------------------------------------------------------------

    def _helper_is_stale(self) -> bool:
        """§19.1 H-8 と同じ判定。**`health.py` と論理を共有しない。**

        あちらは「いま unhealthy か」を Docker へ返し、こちらは「取り込みを進めるか」を
        決める。**同じ 1 行の条件を 2 箇所に書くのは避けたいが、`health.py` は
        `Result` を組み立てる都合で結果の形が違う。**条件そのものは
        `Heartbeat.is_stale()` に閉じてあり、ここはそれを呼ぶだけである。
        """
        beat = read_heartbeat(self.state_root)
        if beat is None:
            return True
        return beat.is_stale(self.now(), self.cfg.import_.helper_heartbeat_max_age_seconds)

    def discover_parts(self) -> None:
        """§10.2: `/inbox` を走査して新規 Part を登録する。"""
        discover.discover(self.database, cfg=self.cfg, log=self.log, now=self.now())
        session.group_parts(self.database, cfg=self.cfg, now=self.now())

    def close_idle_sessions(self) -> None:
        """§10.4: `OPEN` を閉じる 3 条件。"""
        session.close_open_sessions(self.database, cfg=self.cfg, now=self.now())

    def process_pending_parts(self) -> None:
        """§10.5〜§10.7 を 1 件ずつ直列に（**古い順**。§10.0）。

        **1 周で 1 件だけにしない。**`poll_interval_seconds` が 5 秒なので、1 件ずつだと
        32 Part の日が 160 秒以上かかる。**停止要求は 1 件ごとに見る** — 30 分の
        文字起こしの途中では止まれないが、次の Part へ進む前には止まれる。
        """
        runner = pipeline.Pipeline(
            database=self.database, cfg=self.cfg, log=self.log, now=self.now()
        )
        for partkey in self.pending_partkeys():
            if self.stopper.should_stop():
                return
            self._with_in_process_retry(
                lambda key=partkey: runner.process_part(PartKey(key)),  # type: ignore[misc]
                db.EntityType.RECORDING,
                partkey,
            )

    def _with_in_process_retry(
        self, run: Callable[[], object], entity: db.EntityType, key: str
    ) -> None:
        """工程内リトライ（§15.2）。`max_attempts` 回、`backoff_seconds` 間隔。

        **待つのはここである。**`pipeline` は時間を持たない（`sleep` を差し替えて
        テストできるようにするため）。

        **停止要求は待機の前後で見る。**30 秒の backoff の途中では止まれないが、
        次の試行へ進む前には止まれる。
        """
        while True:
            run()
            delay = pipeline.in_process_retry(self.database, entity, key, cfg=self.cfg)
            if delay is None or self.stopper.should_stop():
                return
            self.sleep(delay)
            if self.stopper.should_stop():
                return
            if not pipeline.resume_failed(
                self.database, entity, key, log=self.log, reset_retry=False, now=self.now()
            ):
                return

    def pending_partkeys(self) -> list[PartKey]:
        """終端でない Part を `started_at` 昇順で（§10.0）。

        **一覧を先に確定させる。**処理の途中で足された Part を同じ周回で拾うと、
        **停止要求が効かないまま走り続けうる。**
        """
        terminal = [status.value for status in PART_TERMINAL]
        placeholders = ", ".join("?" for _ in terminal)
        rows = self.database.conn.execute(
            "SELECT partkey FROM recordings "  # noqa: S608 - placeholders は ? のみ
            f"WHERE status NOT IN ({placeholders}) ORDER BY started_at, partkey",
            terminal,
        ).fetchall()
        return [PartKey(row["partkey"]) for row in rows]

    def should_requeue(self, inventory: DeviceInventory | None, *, startup: bool = False) -> bool:
        """§15.2: **立ち上がりのエッジ**か。**この判定だけを独立させてある。**

        契機は 2 つだけである。

        | 契機 | 判定 |
        |---|---|
        | デバイスの再接続 | `devices` が**前回は空で今回は非空** |
        | サービスの起動 | §9.4 の復帰処理の一部として 1 回 |

        **`inventory` が非空のまま続く周回では偽になる。**毎周回の空回りを防ぐ
        （1 日 17,280 周で 32 件を引き直すことになる）。

        **`requeue_failed()` から分けてあるのは、中身が #32 待ちでも判定をテストできる
        ようにするため**である。まとめていると「毎周回走らせる」壊し方をしても
        戻り値が変わらず、テストが通ってしまう（実際に通った）。

        **`inventory` が読めない（`None`）ときは「空」として扱う。**不明は安全側へ倒す
        （§7.5）— 再投入しないほうが安全である。
        """
        empty = inventory is None or inventory.is_empty
        rising_edge = self.last_inventory_empty is not False and not empty
        self.last_inventory_empty = empty
        return startup or rising_edge

    def requeue_failed(self, inventory: DeviceInventory | None, *, startup: bool = False) -> int:
        """`FAILED` を直前の進行中状態へ戻す（§15.2）。戻り値は再投入した件数。

        契機の判定は `should_requeue()` が持つ。**分けてあるのは、判定そのものを
        テストできるようにするため**である。

        **戻し方の規則は `pipeline` が持つ。**ここは契機を見て呼ぶだけである。
        """
        if not self.should_requeue(inventory, startup=startup):
            return 0
        return pipeline.requeue_failed(self.database, log=self.log, now=self.now())

    def process_ready_sessions(self) -> None:
        """§10.8〜§10.9 を 1 件ずつ（**Part と同じく直列**。§10.0）。

        **停止要求は 1 件ごとに見る。**LLM の 1 回が最大 1800 秒（§7.2）なので途中では
        止まれないが、次のセッションへ進む前には止まれる。
        """
        runner = pipeline.Pipeline(
            database=self.database, cfg=self.cfg, log=self.log, now=self.now()
        )
        for session_key in self.ready_session_keys():
            if self.stopper.should_stop():
                return
            self._with_in_process_retry(
                lambda key=session_key: runner.process_session(SessionKey(key)),  # type: ignore[misc]
                db.EntityType.SESSION,
                session_key,
            )

    def ready_session_keys(self) -> list[str]:
        """統合へ進めるセッション（§9.3 の `READY → MERGING` のガード）。

        **`MERGING` も拾う。**再オープン（§9.3 の `SAVED` / `COMPLETED` 行）は `MERGING` へ
        戻すので、`READY` だけを見ると**作り直しが次の起動まで動かない**
        （`recover_interrupted()` が巻き戻すのは起動時の 1 回だけである）。

        **中身の処理は #27 が実装する。**ここはガード条件だけを確定させる —
        「全 Part が終端状態」を取り違えると、**進行中の Part を含むセッションを統合して
        本文が欠けたノートを書く**（§14.1 の削除根拠になる）。
        """
        ready: list[str] = []
        for status in (SessionStatus.READY, SessionStatus.MERGING):
            for row in self.database.sessions_with_status(status):
                parts = self.database.recordings_for_session(row.session_key)
                if parts and all(part.status in PART_TERMINAL for part in parts):
                    ready.append(row.session_key)
        return sorted(ready)
