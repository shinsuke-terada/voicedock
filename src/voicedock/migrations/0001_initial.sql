-- VoiceDock スキーマ v1（SPEC §8.2 / §8.3 / §8.4 / §8.5 の写し）
--
-- ★このファイルは v1 完全形である。SPEC §8.5 は破壊的変更を禁じており、
--   後から列や制約を足すのは ALTER TABLE ADD COLUMN に限られる。
--   「新テーブル → INSERT SELECT → RENAME」は AUTOINCREMENT を振り直し、
--   §14.1 の削除条件（recordings.id の同一性に賭けている）を壊す。
--   そのため今は使わない列（source_size_* / source_mtime_* / source_deleted_at /
--   delete_attempts / auto_retry_rounds）も含めて全部入れてある。
--
-- INSERT INTO schema_version は書かない。db.py が適用後に 1 行入れる。

-- ============================================================
-- SPEC §8.3 sessions（1 デバイス・1 日）
-- ============================================================

CREATE TABLE sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key         TEXT    NOT NULL UNIQUE,   -- "<device_id>:<YYYYMMDD>"
    day_date            TEXT    NOT NULL,          -- 'YYYY-MM-DD'（config.timezone）
    device_id           TEXT    NOT NULL,

    started_at          TEXT,           -- その日の最初の Part の開始時刻
    ended_at            TEXT,
    recorded_seconds    REAL,           -- 実録音時間の合計
    part_count          INTEGER NOT NULL DEFAULT 0,
    failed_part_count   INTEGER NOT NULL DEFAULT 0,

    title               TEXT,           -- LLM 生成。ファイル名には使わない
    analysis_path       TEXT,           -- /data/analysis/<session_id>.json

    -- Raw ノート（文字起こし生データ）
    raw_output_path     TEXT,           -- Vault 内の相対パス
    raw_output_sha256   TEXT,

    -- Daily ノート（整理済み）
    output_path         TEXT,           -- Vault 内の相対パス
    output_sha256       TEXT,

    status              TEXT    NOT NULL,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    auto_retry_rounds   INTEGER NOT NULL DEFAULT 0,
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
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 由来
    device_id             TEXT    NOT NULL,   -- Volume 名（不安定。参考情報）
    source_folder         TEXT    NOT NULL,   -- TX_MIC001_20260829_071201
    transmitter_id        TEXT    NOT NULL,   -- TX01（不安定。恒久 ID にしない）
    mic_index             INTEGER NOT NULL,   -- MIC002 -> 2
    started_at            TEXT    NOT NULL,   -- ISO8601 +09:00。ファイル名由来
    duration_seconds      REAL,               -- ffprobe 由来。失敗時は NULL のまま続行
    ended_at              TEXT,               -- started_at + duration_seconds

    -- Variant（最大 2）。削除時の同定に使う（§14.1）
    -- ★v4.0: ボリュームルートからの相対パスを格納する。絶対パスを持たない（§14.1.1）
    source_path_denoised  TEXT,               -- 'TX_MIC001_.../TX01_..._.wav'
    source_path_orig      TEXT,
    source_size_denoised  INTEGER,
    source_size_orig      INTEGER,
    source_mtime_denoised REAL,
    source_mtime_orig     REAL,
    -- 読み込むのは primary_variant のみ。もう一方の sha256 は NULL のまま
    sha256_denoised       TEXT,
    sha256_orig           TEXT,
    -- Helper がコピー時に算出した値。コンテナが再計算して照合する（§10.5）
    sha256_helper         TEXT,

    primary_variant       TEXT,               -- 'denoised' | 'orig'

    -- 作業成果物
    inbox_path            TEXT,               -- /inbox/<device_id>/<folder>/<name>.wav
    staging_dir           TEXT,
    normalized_path       TEXT,               -- /data/staging/<id>/audio16k.wav
    transcript_path       TEXT,               -- /data/transcripts/parts/<id>.json

    -- 所属
    session_id            INTEGER REFERENCES sessions(id) ON DELETE SET NULL,

    -- 状態（§9.1）
    status                TEXT    NOT NULL,
    retry_count           INTEGER NOT NULL DEFAULT 0,   -- 現在の工程での連続失敗回数
    auto_retry_rounds     INTEGER NOT NULL DEFAULT 0,   -- 自動再試行の実施回数
    error_code            TEXT,
    error_message         TEXT,

    -- タイムスタンプ
    source_deleted_at     TEXT,               -- 不可逆操作の記録（§14）
    updated_at            TEXT    NOT NULL    -- 状態が変わるたびに更新する
);

-- 論理的な同一 Part の二重登録を防ぐ（ファイル名由来の自然キー）
CREATE UNIQUE INDEX idx_recordings_partkey
    ON recordings (transmitter_id, mic_index, started_at, source_folder);

-- 内容同一性による二重処理防止。NULL は重複可なので未ハッシュ行は衝突しない
CREATE UNIQUE INDEX idx_recordings_sha_denoised
    ON recordings (sha256_denoised) WHERE sha256_denoised IS NOT NULL;
CREATE UNIQUE INDEX idx_recordings_sha_orig
    ON recordings (sha256_orig)     WHERE sha256_orig     IS NOT NULL;

CREATE INDEX idx_recordings_status   ON recordings (status);
CREATE INDEX idx_recordings_session  ON recordings (session_id);
CREATE INDEX idx_recordings_started  ON recordings (started_at);

-- ============================================================
-- SPEC §8.4 events（監査ログ）
-- ============================================================

CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT NOT NULL,      -- 'recording' | 'session'
    entity_id     INTEGER NOT NULL,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    error_code    TEXT,
    detail        TEXT,               -- 音声内容・本文は決して入れない（§16.3）
    created_at    TEXT NOT NULL
);

CREATE INDEX idx_events_entity ON events (entity_type, entity_id, id);

-- ============================================================
-- SPEC §8.5 schema_version
-- ============================================================

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,   -- rowid の別名。二重適用を構造的に防ぐ
    applied_at TEXT    NOT NULL
);
