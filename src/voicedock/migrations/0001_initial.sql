-- VoiceDock スキーマ v1（SPEC §8.2 / §8.3 / §8.4 / §8.5 の写し）
--
-- ★v5.1 でこのファイルを 1 回だけ書き直した。SCHEMA_VERSION は 1 のままである。
--   本番データが 1 行も無い間だけ許される操作であり、ストア方式と識別子の決定
--   （docs/STORE.md / #60）を #16 の着手前に置いたのは、この機会を 1 回で使い切るためである。
--
-- ★識別子は自然キーである（§8.1）。recordings.partkey / sessions.session_key。
--   §14.1 の削除条件はノートの frontmatter に載っている鍵と DB の鍵が同じ Part を
--   指すことに賭けており、連番だと振り直しで対応が壊れうる。自然キーなら数える対象が
--   無いので壊れようがない（v5.0→v5.1 の変更 M-1）。
--
-- ★TEXT PRIMARY KEY に NOT NULL を明記している。SQLite では INTEGER PRIMARY KEY だけが
--   rowid の別名として NULL を弾き、TEXT PRIMARY KEY は NULL を許す（標準 SQL からの
--   既知の逸脱）。省略すると partkey が NULL の行を作れてしまう。
--
-- ★WITHOUT ROWID は使わない。TEXT 主キーでは理にかなうが、lastrowid の意味と PRAGMA 出力が
--   変わり、db.structure() の突き合わせに影響する。最適化のために挙動を変えない。
--
-- ★§8.5 の唯一の禁則は「partkey / session_key の算出規則を変えるな」である。
--   テーブルの作り直し自体は許される（作り直しても鍵は変わらない）。
--
-- INSERT INTO schema_version は書かない。db.py が適用後に 1 行入れる。

-- ============================================================
-- SPEC §8.3 sessions（1 デバイス・1 日）
--   recordings が session_key を参照するので先に作る
-- ============================================================

CREATE TABLE sessions (
    -- 恒久識別子（§8.1）。NOT NULL の省略は不可（§8.2 の注記と同じ理由）
    session_key         TEXT    PRIMARY KEY NOT NULL,   -- "<device_id>:<YYYYMMDD>"
    day_date            TEXT    NOT NULL,          -- 'YYYY-MM-DD'（config.timezone）
    device_id           TEXT    NOT NULL,

    started_at          TEXT,           -- その日の最初の Part の開始時刻
    ended_at            TEXT,
    recorded_seconds    REAL,           -- 実録音時間の合計
    part_count          INTEGER NOT NULL DEFAULT 0,
    failed_part_count   INTEGER NOT NULL DEFAULT 0,

    title               TEXT,           -- LLM 生成。ファイル名には使わない
    analysis_path       TEXT,           -- /data/analysis/<slug>.json（slug = key_slug(session_key)）

    -- Raw ノート（文字起こし生データ）
    raw_output_path     TEXT,           -- Vault 内の相対パス
    raw_output_sha256   TEXT,

    -- Daily ノート（整理済み）
    output_path         TEXT,           -- Vault 内の相対パス
    output_sha256       TEXT,

    status              TEXT    NOT NULL,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    regenerated_count   INTEGER NOT NULL DEFAULT 0,
    delete_attempts     INTEGER NOT NULL DEFAULT 0,   -- 削除評価のバックオフ段数
    error_code          TEXT,
    error_message       TEXT,

    source_deleted_at   TEXT,                 -- 不可逆操作の記録（§14）
    updated_at          TEXT    NOT NULL      -- 状態が変わるたびに更新する
);

CREATE INDEX idx_sessions_status ON sessions (status);
CREATE INDEX idx_sessions_day    ON sessions (day_date);

-- ============================================================
-- SPEC §8.2 recordings（Part）
-- ============================================================

CREATE TABLE recordings (
    -- 恒久識別子（§8.1）。'<device_id>/<source_folder>/<filename>'
    -- ★NOT NULL を省略できない。SQLite では INTEGER PRIMARY KEY だけが rowid の別名として
    --   NULL を弾き、TEXT PRIMARY KEY は NULL を許す（標準 SQL からの既知の逸脱）
    partkey               TEXT    PRIMARY KEY NOT NULL,

    -- 由来。すべて partkey とファイル名から導けるが、索引を張るために列で持つ
    -- （partkey == device_id || '/' || source_path の不変量はテストが固定する）
    device_id             TEXT    NOT NULL,   -- Volume 名
    source_folder         TEXT    NOT NULL,   -- TX_MIC001_20260829_071201
    transmitter_id        TEXT    NOT NULL,   -- TX01（不安定。恒久 ID にしない）
    mic_index             INTEGER NOT NULL,   -- MIC002 -> 2
    started_at            TEXT    NOT NULL,   -- ISO8601 +09:00。ファイル名由来
    duration_seconds      REAL,               -- ffprobe 由来。失敗時は NULL のまま続行
    ended_at              TEXT,               -- started_at + duration_seconds

    -- 取り込み元（`_orig` の 1 本だけ。§5.3）。削除時の同定に使う（§14.1）
    -- ★v4.0: ボリュームルートからの相対パスを格納する。絶対パスを持たない（§14.1.1）
    source_path           TEXT,               -- 'TX_MIC001_.../TX01_..._orig.wav'
    source_size           INTEGER,
    source_mtime          REAL,
    sha256                TEXT,
    -- Helper がコピー時に算出した値。コンテナが再計算して照合する（§10.5）
    sha256_helper         TEXT,

    -- 作業成果物。slug = key_slug(partkey)（§11.2）
    inbox_path            TEXT,               -- /inbox/<device_id>/<folder>/<name>.wav
    staging_dir           TEXT,
    normalized_path       TEXT,               -- /data/staging/<slug>/audio16k.wav
    transcript_path       TEXT,               -- /data/transcripts/parts/<slug>.json

    -- 所属
    session_key           TEXT REFERENCES sessions(session_key) ON DELETE SET NULL,

    -- 状態（§9.1）
    status                TEXT    NOT NULL,
    retry_count           INTEGER NOT NULL DEFAULT 0,   -- 現在の工程での連続失敗回数
    error_code            TEXT,
    error_message         TEXT,

    -- タイムスタンプ
    source_deleted_at     TEXT,               -- 不可逆操作の記録（§14）
    updated_at            TEXT    NOT NULL    -- 状態が変わるたびに更新する
);

-- 論理的な同一 Part の二重登録は PRIMARY KEY が防ぐ。
-- v5.0 までの idx_recordings_partkey（transmitter_id, mic_index, started_at, source_folder）は
-- 主キーに包含されたので削除した。新しい鍵はファイル名を丸ごと含むため識別能力は落ちない。

-- 内容同一性による二重処理防止。NULL は重複可なので未ハッシュ行は衝突しない
CREATE UNIQUE INDEX idx_recordings_sha ON recordings (sha256) WHERE sha256 IS NOT NULL;

CREATE INDEX idx_recordings_status   ON recordings (status);
CREATE INDEX idx_recordings_session  ON recordings (session_key);
CREATE INDEX idx_recordings_started  ON recordings (started_at);

-- ============================================================
-- SPEC §8.4 events（監査ログ）
-- ============================================================

CREATE TABLE events (
    -- ここだけ AUTOINCREMENT を残す。追記専用ログの順序付けにしか使わず、誰も参照しない。
    -- 振り直されても壊れるものが無い（§8.1 の識別子の議論はエンティティの話である）
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT NOT NULL,      -- 'recording' | 'session'
    entity_key    TEXT NOT NULL,      -- recordings.partkey | sessions.session_key
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    error_code    TEXT,
    detail        TEXT,               -- 音声内容・本文は決して入れない（§16.3）
    created_at    TEXT NOT NULL
);

CREATE INDEX idx_events_entity ON events (entity_type, entity_key, id);

-- ============================================================
-- SPEC §8.5 schema_version
-- ============================================================

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,   -- rowid の別名。二重適用を構造的に防ぐ
    applied_at TEXT    NOT NULL
);
