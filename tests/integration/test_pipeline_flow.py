"""§20.2 の通しフローを 1 本流す。

```text
偽 inbox（.meta.json 付き）→ SHA-256 照合 → 変換 → Whisper mock → Raw ノート
    → Session merge → LLM mock → Daily ノート → Atomic write → 保存検証
```

**各段の単体テストが緑でも、つながっていることは誰も見ていない。**#28 の実装中、
単体テストが全部緑のまま「Timeline が常に fallback を使う」「時刻の空白を跨いで
overlap を運ぶ」という 2 つの実バグが残っており、**通しで動かして初めて見つかった。**

**`worker.Worker` を回す。**`pipeline` を直接叩くと**配線が抜けていても通る**
（`worker.requeue_failed()` の `TODO` が単体テスト全緑のまま残っていた実例がある）。

**ネットワークは `tests/conftest.py` の `no_network` が全テストで遮断している**（§14.5）。
LLM は `httpx.MockTransport` で組み立て、**外部へ 1 バイトも出さない**（§14.4 N-7）。
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml

from tests.fixtures.fake_tree import DEFAULT_RECORDINGS, DEVICE_ID, ORIG, build_fake_inbox
from tests.fixtures.fake_whisper import write_fake_whisper
from tests.fixtures.make_wav import WavFormat
from tests.helpers import complete_tree, llm_response, merge, write_heartbeat
from voicedock import db, notes, paths, worker
from voicedock import log as log_module
from voicedock.config import Config, load_config
from voicedock.db import Database
from voicedock.log import Logger
from voicedock.states import PART_TERMINAL, PartStatus, SessionStatus

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 9, 13, 9, 0, 0, tzinfo=JST)
"""偽 inbox の録音は 2026-09-12 と 09-13 にある。**翌朝**に回すことで
2026-09-12 のセッションが「過去日」として閉じる（§10.4）。"""

ORIG_RECORDINGS = tuple(r for r in DEFAULT_RECORDINGS if ORIG in r.variants)
"""`_orig` を持つ録音だけが Part になる（§5.3）。"""

DAY = "2026-09-12"
SESSION_KEY = f"{DEVICE_ID}:20260912"


@dataclass
class Flow:
    """1 回流したあとの成果物。**複数のテストが同じ 1 回を見る。**"""

    cfg: Config
    database: Database
    vault: Path
    inbox: Path
    data: Path
    log: str
    requests: list[httpx.Request] = field(default_factory=list)

    def session(self) -> object:
        row = self.database.get_session(SESSION_KEY)
        assert row is not None, "セッションが作られていない"
        return row

    def daily_note(self) -> Path:
        found = sorted(self.vault.rglob("*Voice.md"))
        assert len(found) == 1, f"Daily ノートが 1 本ではない: {found}"
        return found[0]

    def raw_note(self) -> Path:
        found = sorted(self.vault.rglob("*raw.md"))
        assert len(found) == 1, f"Raw ノートが 1 本ではない: {found}"
        return found[0]


@pytest.fixture(params=[WavFormat.PCM24, WavFormat.FLOAT32], ids=["pcm24", "float32"])
def wav_format(request: pytest.FixtureRequest) -> WavFormat:
    """**両形式で通す**（§20.2）。§5.1 のとおり形式は DJI 側の設定で変わる。"""
    result: WavFormat = request.param
    return result


@pytest.fixture
def flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wav_format: WavFormat) -> Iterator[Flow]:
    """通しで 1 回流す。**マウント点をすべて `tmp_path` の下に再現する。**"""
    data = tmp_path / "data"
    for sub in ("staging", "transcripts/parts", "analysis"):
        (data / sub).mkdir(parents=True)
    vault = tmp_path / "obsidian"
    vault.mkdir()
    # WikiLink の相手として実在させる（§13.8 は Vault に在るノートだけをリンクする）
    (vault / "VoiceDock.md").write_text("# VoiceDock\n", encoding="utf-8")
    inbox = build_fake_inbox(tmp_path / "inbox", fmt=wav_format)
    state = tmp_path / "state"
    state.mkdir()
    fake = tmp_path / "fake"
    fake.mkdir()
    whisper = write_fake_whisper(fake / "whisper-cli")

    for name, value in {
        "INBOX_ROOT": inbox,
        "DATA_ROOT": data,
        "VAULT_ROOT": vault,
        "TRANSCRIPTS_ROOT": data / "transcripts" / "parts",
        "ANALYSIS_ROOT": data / "analysis",
    }.items():
        monkeypatch.setattr(paths, name, value)

    document = merge(
        complete_tree(tmp_path),
        {
            "database": {"path": str(data / "voicedock.db")},
            "obsidian": {"root": str(vault)},
            "import": {"inbox_root": str(inbox), "staging_root": str(data / "staging")},
            "transcription": {"executable": str(whisper)},
        },
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("VOICEDOCK_CONFIG", str(config))
    monkeypatch.setenv("VOICEDOCK_LLM_URL", "http://model-runner.docker.internal/engines/v1")
    monkeypatch.setenv("VOICEDOCK_LLM_MODEL", "ai/test-model")

    write_heartbeat(state, updated_at=NOW.isoformat(), helper_version="5.5.0")
    (state / "inventory.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "generated_at": NOW.isoformat(),
                "mount_readonly": True,
                "device_free_bytes": {},
                "devices": {},
            }
        ),
        encoding="utf-8",
    )

    seen: list[httpx.Request] = []
    # **`llm.py` が `httpx.Client()` を作る経路を差し替える。**`complete()` は `client`
    # 引数を受けるが、**`pipeline` は渡さない**（§12.1 の呼び出しは設定だけで完結する）
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(seen))

    cfg = load_config()
    stream = io.StringIO()
    log = Logger(level="DEBUG", fmt="text", stream=stream, tz=JST)
    with db.connect(cfg.database.path, tz=JST, now=NOW) as database:
        runner = worker.Worker(
            cfg=cfg, log=log, database=database, state_root=state, clock=lambda: NOW
        )
        runner.start()
        runner.tick()
        yield Flow(
            cfg=cfg,
            database=database,
            vault=vault,
            inbox=inbox,
            data=data,
            log=stream.getvalue(),
            requests=seen,
        )


def _mock_client_factory(seen: list[httpx.Request]) -> object:
    """`llm.py` の `httpx.Client()` を差し替える（§20.2）。

    **`pipeline` は `client` を渡さない**（§12.1 の呼び出しは設定だけで完結する）ので、
    引数注入ではなく `httpx.Client` そのものを置き換える。

    Map 段と Reduce 段で応答の形が違う。**振り分けはプロンプトが `title` を要求して
    いるかで行う** — 最終段だけが `title` と `tags` を持つ（§12.2 / v5.4→v5.5 の変更 Q-1）。
    """
    final = llm_response("session_analysis")
    partial = llm_response("partial_analysis")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"]
        body = final if '"title"' in prompt else partial
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}]},
        )

    # **本物の `Client` を先に掴んでおく。**`llm.httpx` は `httpx` そのものなので、
    # 差し替えたあとに `httpx.Client` を呼ぶと自分自身を呼んで無限再帰する
    real_client = httpx.Client

    def factory(*_args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    return factory


# --- F-1〜F-11 -----------------------------------------------------------


@pytest.mark.needs_ffmpeg
def test_only_orig_parts_are_registered(flow: Flow) -> None:
    """F-1: `_orig` を持つ録音だけが Part になる（§5.3）。**ノイズは登録されない。**

    `.meta.json` を持たない `.wav`（コピー途中）・AppleDouble・拡張子違いは
    Part 候補にしてはならない（§10.2）。
    """
    rows = flow.database.conn.execute("SELECT partkey, status FROM recordings").fetchall()
    assert {row["partkey"] for row in rows} == {r.partkey() for r in ORIG_RECORDINGS}


@pytest.mark.needs_ffmpeg
def test_every_part_reaches_a_terminal_state(flow: Flow) -> None:
    """F-1: Part が終端状態まで進む（§9.1）。**無音は `SKIPPED`、失敗ではない**（§10.6）。

    **終端は `RAW_SAVED` である。**`RAW_SAVED → COMPLETED` は §9.3 の「削除要求」行で、
    `delete_source_audio == false` なら `COMPLETED` へ進むが、**その評価は #36 の
    `cleaner.py` が入ってからである。**セッションの進行判定には `RAW_SAVED` で足りる
    （§9.1 の終端状態一覧）。
    """
    terminal = {status.value for status in PART_TERMINAL}
    rows = flow.database.conn.execute("SELECT partkey, status FROM recordings").fetchall()
    assert all(row["status"] != PartStatus.FAILED for row in rows), [dict(r) for r in rows]
    assert all(row["status"] in terminal for row in rows), [dict(r) for r in rows]
    assert {row["status"] for row in rows} == {PartStatus.RAW_SAVED}, [dict(r) for r in rows]


@pytest.mark.needs_ffmpeg
def test_the_session_is_saved(flow: Flow) -> None:
    """F-2: セッションが `SAVED` まで進む（削除は無効なのでここが終端。§9.2）。"""
    session = flow.session()
    assert session.status == SessionStatus.SAVED, session.status  # type: ignore[attr-defined]
    assert session.part_count == len(ORIG_RECORDINGS)  # type: ignore[attr-defined]
    assert session.failed_part_count == 0  # type: ignore[attr-defined]


@pytest.mark.needs_ffmpeg
def test_the_normalized_audio_is_deleted(flow: Flow) -> None:
    """F-3: 文字起こし後に 16 kHz 音声が消える（`delete_normalized_after_transcribe`）。

    **残すと容量が 2 倍になる。**32 Part / 日の運用で効く（§14.1）。
    """
    assert flow.cfg.cleanup.delete_normalized_after_transcribe
    left = sorted(p.name for p in (flow.data / "staging").rglob("*.wav"))
    assert left == [], left


@pytest.mark.needs_ffmpeg
def test_the_inbox_copy_is_removed_after_normalize(flow: Flow) -> None:
    """F-9a: inbox の原本は `NORMALIZED` で消える（`import.inbox_retain: normalized`）。

    **これはデバイスの原本ではない。**Helper が `/inbox` へ置いたコピーであり、変換して
    検証が通った時点で役目が終わる（§9.3 の `NORMALIZING → NORMALIZED` 行）。残すと
    **同じ音声を 3 本（デバイス・inbox・16 kHz）抱える。**
    """
    assert flow.cfg.import_.inbox_retain == "normalized"
    for recording in ORIG_RECORDINGS:
        copied = flow.inbox / DEVICE_ID / recording.relpath(ORIG)
        assert not copied.exists(), copied


@pytest.mark.needs_ffmpeg
def test_no_delete_request_is_written(flow: Flow) -> None:
    """F-9b: **削除要求を 1 件も書かない**（`delete_source_audio: false`。§14.1 / §14.2）。

    **安全ロック 1 が外れていないこと。**ここに JSON が出たら、Helper 側の 2 つのロックが
    同時に外れた瞬間にデバイスの原本が消える。**Phase 7 まで空であること。**
    """
    assert not flow.cfg.cleanup.delete_source_audio
    queue = flow.cfg.cleanup.queue_root
    written = sorted(queue.rglob("*.json")) if queue.exists() else []
    assert written == [], written


@pytest.mark.needs_ffmpeg
def test_the_raw_note_lists_every_part(flow: Flow) -> None:
    """F-4: Raw ノートの `voicedock_recording_keys` が実 Part を含む（§13.7 R-6）。"""
    keys = set(notes.frontmatter_keys(flow.raw_note()))
    assert {r.partkey() for r in ORIG_RECORDINGS} <= keys


@pytest.mark.needs_ffmpeg
def test_the_daily_note_passes_every_save_check(flow: Flow) -> None:
    """F-5: Daily ノートが W-1〜W-9 を**全部**通る（§13.7）。

    **§14.1 の削除条件はこの検証に乗っている。**ここが緩むと、テキストが欠けたまま
    元音声を消す経路ができる。
    """
    session = flow.session()
    path = flow.daily_note()
    results = notes.verify_note(
        path,
        kind=notes.NoteKind.DAILY,
        session_key=SESSION_KEY,
        expected_sha=session.output_sha256,  # type: ignore[attr-defined]
        expected_keys=[r.partkey() for r in ORIG_RECORDINGS],
        require_raw_link=flow.cfg.obsidian.wiki.link_raw,
    )
    assert notes.all_passed(results), notes.failed_rules(results)


@pytest.mark.needs_ffmpeg
def test_the_timeline_does_not_repeat_one_summary(flow: Flow) -> None:
    """F-6: Timeline の各見出しの本文が**同じ文の繰り返しにならない**（§13.4）。

    **これは実際に出たバグである。**Map の中間結果を保存していなかったため、
    Timeline がどの Block でも同じ全体要約（fallback）を出していた。単体テストは
    レンダラに中間結果を直接渡していたので緑のままだった（v5.5→v5.6 の変更 R-1）。
    """
    body = flow.daily_note().read_text(encoding="utf-8")
    assert "## Timeline" in body, body
    timeline = body.split("## Timeline", 1)[1].split("\n## ", 1)[0]
    blocks = [line for line in timeline.splitlines() if line.startswith("### ")]
    assert blocks, timeline
    paragraphs = [
        line.strip()
        for line in timeline.splitlines()
        if line.strip() and not line.startswith(("#", "-"))
    ]
    assert len(set(paragraphs)) == len(paragraphs), paragraphs


@pytest.mark.needs_ffmpeg
def test_tags_have_no_spaces(flow: Flow) -> None:
    """F-7: タグの空白が `-` になる（§13.3）。

    **Obsidian のタグは空白で切れる。**`DJI Mic` をそのまま置くと `#DJI` と `Mic` になる。
    """
    front = notes.parse_frontmatter(flow.daily_note().read_text(encoding="utf-8"))
    assert front is not None
    tags = front["tags"]
    assert isinstance(tags, list)
    assert all(" " not in str(tag) for tag in tags), tags
    assert "DJI-Mic" in tags, tags


@pytest.mark.needs_ffmpeg
def test_the_llm_is_reached_only_through_the_configured_endpoint(flow: Flow) -> None:
    """F-8: LLM 呼び出しが**設定した宛先だけ**であること（§14.4 N-7 / §14.5）。

    外部 API へのフォールバックを書くと**音声の内容が外へ出る。**素の `httpx` は
    `no_network` が遮断するので、ここで見るのは「宛先が 1 つであること」である。
    """
    assert flow.requests, "LLM が 1 回も呼ばれていない"
    hosts = {request.url.host for request in flow.requests}
    assert hosts == {"model-runner.docker.internal"}, hosts


@pytest.mark.needs_ffmpeg
def test_the_second_tick_changes_nothing(flow: Flow) -> None:
    """F-10: 2 周目で何も起きない（**冪等**。§10.2 / §13.6）。

    **`already_known` で止まること**と、**ノートが書き換わらないこと**の両方を見る。
    再生成が走るとノートの mtime と sha256 が変わり、Obsidian の同期が毎周回動く。
    """
    before = {p: p.read_bytes() for p in sorted(flow.vault.rglob("*.md"))}
    calls_before = len(flow.requests)

    runner = worker.Worker(
        cfg=flow.cfg,
        log=Logger(level="DEBUG", fmt="text", stream=io.StringIO(), tz=JST),
        database=flow.database,
        state_root=flow.data.parent / "state",
        clock=lambda: NOW,
    )
    runner.tick()

    after = {p: p.read_bytes() for p in sorted(flow.vault.rglob("*.md"))}
    assert after == before, "2 周目でノートが書き換わった"
    assert len(flow.requests) == calls_before, "2 周目で LLM を呼び直している"


@pytest.mark.needs_ffmpeg
def test_the_flow_logs_the_expected_events(flow: Flow) -> None:
    """§16.4 のイベントが通しで出ること。**段が丸ごと飛んでいたら落ちる。**"""
    expected = (
        "part_discovered",
        "normalize_completed",
        "transcription_completed",
        "raw_note_saved",
        "session_merged",
        "llm_completed",
        "obsidian_saved",
    )
    # **名前が §16.4 に在ることを先に見る。**綴り違いを「出ていない」と読み違えない
    unknown = [name for name in expected if name not in log_module._EVENT_ORDER]
    assert unknown == [], unknown
    for event in expected:
        assert event in flow.log, f"{event} が出ていない\n{flow.log}"


@pytest.mark.needs_ffmpeg
def test_the_git_tracked_fixtures_stay_clean(flow: Flow) -> None:
    """F-11: **git 管理下の fixture に書き込まない**（§20.2）。

    実 fixture を直接与えると、削除テストが実ファイルを消す。`build_fake_inbox()` は
    `tmp_path` を受けるので、`tests/fixtures/` には `.py` と `.json` しか無いはずである。
    """
    fixtures = Path(__file__).parents[1] / "fixtures"
    strays = sorted(
        p.relative_to(fixtures)
        for p in fixtures.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix not in {".py", ".json", ".yaml", ".yml", ".md"}
    )
    assert strays == [], strays
