"""セッション分組（1 日 = 1 セッション）・Block 算出・絶対時刻での統合（SPEC §10.4, §10.8）。

**時刻モデルをここで 1 度だけ決める。**Raw ノート（#24）・Daily ノート（#28）・LLM 入力（#27）は
どれも「全 Part の transcript を時刻順に並べる」処理である。**先にどこかで
`offset += duration` 方式を書くと、v3.0 の既知バグ（Part 間のギャップが詰まる・
`duration` が NULL で例外）がそのまま複製される。**

**統合結果は永続化しない**（§10.8）。DB にもファイルにも書かず、戻り値だけで渡す。

鍵の算出（`session_key_for`）は `paths.py` にある。§8.5 の唯一の禁則は
「`partkey` / `session_key` の算出規則を変えるな」であり、**2 つの鍵は同格**なので
同じ場所で同じ形のテストに守らせる。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from voicedock import paths
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording, Session
from voicedock.paths import PartKey, SessionKey
from voicedock.states import SESSION_INITIAL, PartStatus, SessionStatus

EXCLUDED_FROM_MERGE: frozenset[str] = frozenset({PartStatus.FAILED, PartStatus.SKIPPED})
"""統合から除外する Part の状態（§10.8）。件数は `sessions.failed_part_count` に残す。"""


# --- §11.2 のデータ構造 --------------------------------------------------


@dataclass(frozen=True)
class AbsoluteSegment:
    """絶対時刻を持つ 1 区間（§11.2）。

    **`at` は `part.started_at + seg.start` である**（§10.8）。相対オフセットの
    累積加算をしない。Part 間に無録音区間があっても実時間が保たれ、誤差も累積しない。
    """

    at: datetime
    end_at: datetime
    text: str


@dataclass(frozen=True)
class SessionTranscript:
    """1 セッション分の統合結果（§11.2）。**永続化しない**（§10.8）。"""

    day_date: date
    segments: list[AbsoluteSegment]
    blocks: list[tuple[datetime, datetime]]
    excluded_partkeys: list[PartKey]


class RelativeSegment(Protocol):
    """`transcribe.Transcript` の 1 区間（§11.2）。**Part 先頭からの相対秒。**"""

    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...

    @property
    def text(self) -> str: ...


class PartTranscript(Protocol):
    """`transcribe.Transcript`（§11.2）を構造的に受ける。

    **具象クラスをここで定義しない。**§11.2 は `Transcript` / `TranscriptSegment` を
    `transcribe.py`（#20）に置くと決めており、ここで作ると同じ型が 2 つになる。
    """

    @property
    def segments(self) -> Sequence[RelativeSegment]: ...


TranscriptLoader = Callable[[Recording], PartTranscript | None]
"""Part の transcript を読む関数（#20 が与える）。`None` は「読めなかった」。"""


# --- 分組（§10.4） -------------------------------------------------------


@dataclass(frozen=True)
class GroupResult:
    """1 回の分組の結果。"""

    assigned: tuple[tuple[PartKey, SessionKey], ...]
    created: tuple[SessionKey, ...]
    """新しく `OPEN` で作った `sessions` の鍵。"""


def group_parts(
    database: Database,
    *,
    cfg: Config,
    now: datetime | None = None,
) -> GroupResult:
    """`session_key IS NULL` の Part を「1 デバイス・1 日」へまとめる（§10.4）。

    **対象は `session_key IS NULL` の Part のみである。**再接続のたびに全件を
    引き直すと、**処理済みの Part が別のセッションへ移りうる。**

    **ログを出さない。**§16.4 は `session_opened` / `session_ready` を v5.0 で削除し、
    行き先を「**`events` テーブル**（状態遷移そのもの）」と定めている。ここで起きることは
    すべて状態遷移なので、`events` に残れば足りる。

    **セッションが `OPEN` でなくても `session_key` は割り当てる。**§9.3 の再オープンは
    「同日の新 Part が **`RAW_SAVED`** になったとき」に起きる（#32）。分組は
    `DISCOVERED` の時点で走るので、**ここで再オープンを判断してはならない。**
    """
    moment = now if now is not None else datetime.now(cfg.tz)
    assigned: list[tuple[PartKey, SessionKey]] = []
    created: list[SessionKey] = []

    for part in database.ungrouped_recordings():
        started = datetime.fromisoformat(part.started_at)
        key = paths.session_key_for(part.device_id, started, tz=cfg.tz)
        session = database.get_session(key)
        if session is None:
            database.insert_session(_new_session(key, part.device_id, now=moment), now=moment)
            created.append(key)
            status: str = SESSION_INITIAL
        else:
            status = session.status

        _attach(database, part, key, now=moment)
        _refresh(database, key, now=moment)
        if status == SessionStatus.OPEN:
            # **`OPEN → OPEN` は遷移である**（§9.3 の注記）。`READY のまま` /
            # `SAVED のまま` と違い、Part が増えたことを events に残す
            database.record_transition(
                EntityType.SESSION,
                key,
                from_status=SessionStatus.OPEN,
                to_status=SessionStatus.OPEN,
                detail=part.partkey,
                now=moment,
            )
        assigned.append((PartKey(part.partkey), key))

    return GroupResult(assigned=tuple(assigned), created=tuple(created))


def _new_session(key: SessionKey, device_id: str, *, now: datetime) -> Session:
    """`OPEN` の `sessions` 行（§9.3 Session 表の 1 行目）。

    `day_date` は**鍵から導く。**Part の `started_at` から取り直すと、鍵と `day_date` が
    食い違う経路ができる（`tz` の扱いを 2 箇所に書くことになる）。
    """
    return Session(
        session_key=key,
        day_date=paths.day_of_session(key).isoformat(),
        device_id=device_id,
        status=SESSION_INITIAL,
        updated_at=now.isoformat(timespec="seconds"),
    )


def _attach(database: Database, part: Recording, key: SessionKey, *, now: datetime) -> None:
    """Part に `session_key` を書く。**`status` は変えない**ので `record_transition` は使わない。"""
    with database.conn:
        database.conn.execute(
            "UPDATE recordings SET session_key = ?, updated_at = ? WHERE partkey = ?",
            (key, now.isoformat(timespec="seconds"), part.partkey),
        )


def _refresh(database: Database, key: SessionKey, *, now: datetime) -> None:
    """`part_count` / `started_at` / `ended_at` / `recorded_seconds` / `failed_part_count`。

    **毎回すべて数え直す。**加算で持つと、1 回の取りこぼしが恒久的なずれになる。
    32 件の集計なので費用は問題にならない（`docs/STORE.md` の実測）。
    """
    row = database.conn.execute(
        "SELECT COUNT(*) AS parts, MIN(started_at) AS first_at, MAX(ended_at) AS last_at, "
        "SUM(duration_seconds) AS seconds, "
        "SUM(CASE WHEN status IN (?, ?) THEN 1 ELSE 0 END) AS excluded "
        "FROM recordings WHERE session_key = ?",
        (PartStatus.FAILED, PartStatus.SKIPPED, key),
    ).fetchone()
    with database.conn:
        database.conn.execute(
            "UPDATE sessions SET part_count = ?, started_at = ?, ended_at = ?, "
            "recorded_seconds = ?, failed_part_count = ?, updated_at = ? WHERE session_key = ?",
            (
                row["parts"],
                row["first_at"],
                row["last_at"],
                row["seconds"],
                row["excluded"],
                now.isoformat(timespec="seconds"),
                key,
            ),
        )


# --- `OPEN` を閉じる（§10.4 の 3 条件） ---------------------------------


def close_open_sessions(
    database: Database,
    *,
    cfg: Config,
    now: datetime | None = None,
) -> tuple[SessionKey, ...]:
    """`OPEN` のセッションを `READY` へ進める（§10.4）。

    §10.4 の 3 条件のうち **1 と 3 は同じ検査である**（「日付が今日でない `OPEN`」）。
    条件 3 は「起動時のリカバリで」という**呼ぶ場面**の指定であって別の規則ではないので、
    起動時にも定期評価にもこの 1 本を呼ぶ。**別の関数を作らない。**

    1. 当該セッションの日付が「今日」でない
    2. 当日であっても、最後の Part 追加から `session.idle_close_seconds` が経過した
    3. 起動時のリカバリで、日付が過去の `OPEN` を検出した（= 1 と同じ）
    """
    moment = now if now is not None else datetime.now(cfg.tz)
    today = moment.astimezone(cfg.tz).date()
    idle_before = moment - timedelta(seconds=cfg.session.idle_close_seconds)

    closed: list[SessionKey] = []
    for session in database.sessions_with_status(SessionStatus.OPEN):
        key = SessionKey(session.session_key)
        stale_day = date.fromisoformat(session.day_date) != today
        idle = datetime.fromisoformat(session.updated_at) <= idle_before
        if not (stale_day or idle):
            continue
        database.record_transition(
            EntityType.SESSION,
            key,
            from_status=SessionStatus.OPEN,
            to_status=SessionStatus.READY,
            detail="stale_day" if stale_day else "idle",
            now=moment,
        )
        closed.append(key)

    return tuple(closed)


# --- Block（§10.4） ------------------------------------------------------


class BlockPart(Protocol):
    """Block 算出に要る 2 つの値だけ。`db.Recording` がそのまま満たす。"""

    @property
    def started_at(self) -> str: ...

    @property
    def ended_at(self) -> str | None: ...


def compute_blocks(
    parts: Iterable[BlockPart], *, gap_seconds: int
) -> list[tuple[datetime, datetime]]:
    """連続録音の塊を返す（§10.4）。**分組には使わず、レンダリング時に算出する。**

    前 Part の `ended_at` から次 Part の `started_at` までが `gap_seconds` を超えたら
    別の Block にする。

    **`ended_at` が NULL の Part はそこで必ず区切る**（§10.4「確信が持てない場合は分ける」）。
    `duration_seconds` は ffprobe 失敗時に `NULL` のまま続行するので（§10.5）**実際に存在する。**
    終端が分からないまま繋ぐと、**無録音区間を録音中だったことにしてしまう。**
    """
    blocks: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    end: datetime | None = None
    unknown_end = False

    for part in sorted(parts, key=lambda p: (p.started_at, p.ended_at or "")):
        began = datetime.fromisoformat(part.started_at)
        finished = datetime.fromisoformat(part.ended_at) if part.ended_at else None

        if start is None or end is None:
            start, end = began, finished if finished is not None else began
            unknown_end = finished is None
            continue

        gap = (began - end).total_seconds()
        if unknown_end or gap > gap_seconds:
            blocks.append((start, end))
            start, end = began, finished if finished is not None else began
            unknown_end = finished is None
            continue

        unknown_end = finished is None
        end = max(end, finished if finished is not None else began)

    if start is not None and end is not None:
        blocks.append((start, end))
    return blocks


# --- 統合（§10.8） -------------------------------------------------------


def build_session_transcript(
    parts: Sequence[Recording],
    *,
    day_date: date,
    load: TranscriptLoader,
    gap_seconds: int,
) -> SessionTranscript | None:
    """全 Part の transcript を**絶対時刻**で連結する（§10.8）。

    **時刻は相対オフセットの加算ではなく、各 Part の `started_at` を起点とした
    絶対時刻で持つ。**Part 間に無録音区間があっても実時間が保たれ、誤差も累積しない。
    `offset += duration` 方式は Part 間のギャップを詰め、`duration` が NULL で例外になる。

    **音声自体は結合しない。統合結果は永続化しない**（§10.8）。

    Returns:
        有効な Part が 0 件、または本文が 1 文字も無ければ `None`
        （`session_empty` の判定材料。§9.3）。**空の `SessionTranscript` を返さない** —
        呼び手が「本文が空のノート」を書きうる。
    """
    valid = [part for part in parts if part.status not in EXCLUDED_FROM_MERGE]
    excluded = [PartKey(part.partkey) for part in parts if part.status in EXCLUDED_FROM_MERGE]

    segments: list[AbsoluteSegment] = []
    for part in sorted(valid, key=lambda p: (p.started_at, p.partkey)):
        transcript = load(part)
        if transcript is None:
            continue
        started = datetime.fromisoformat(part.started_at)
        for segment in transcript.segments:
            text = segment.text.strip()
            if not text:
                continue
            segments.append(
                AbsoluteSegment(
                    at=started + timedelta(seconds=segment.start),
                    end_at=started + timedelta(seconds=segment.end),
                    text=text,
                )
            )

    if not segments:
        return None

    segments.sort(key=lambda s: (s.at, s.end_at))
    return SessionTranscript(
        day_date=day_date,
        segments=segments,
        blocks=compute_blocks(valid, gap_seconds=gap_seconds),
        excluded_partkeys=excluded,
    )
