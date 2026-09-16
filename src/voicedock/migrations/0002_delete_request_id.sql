-- ============================================================
-- SPEC §8.2 recordings: delete_request_id
--
-- ★コンテナが「いま自分が出している削除要求の request_id」を持つ列。
--   v5.45 まではこれを queue/delete/ から読もうとしていた。しかし
--   **queue/delete/<id>.json は reaper が所有し、処理した瞬間に消す**
--   （voicedock-reaper の `rm -f "$file"`）。結果が届くとき要求は必ず 0 件なので、
--   照合は常に失敗し、**すべての削除結果を捨てていた**（v5.45→v5.46 の変更 BH-1）。
--
--   「キューが唯一の出所である」という前提が偽だった。**コンテナの事実はコンテナが持つ。**
--
-- ★NULL 可。既存行は「要求を出していない」を意味する。COMPLETED / SKIPPED に
--   要求は出ていないので、移行時に NULL が入るのは正しい状態である。
--
-- ★ALTER TABLE ADD COLUMN は列を末尾に足すので、SPEC §8.2 の DDL でも
--   updated_at の後に置いてある。test_db.py::test_schema_matches_spec は
--   PRAGMA table_info を**順序込み**で比べるため、ここ以外に置けない。
-- ============================================================

ALTER TABLE recordings ADD COLUMN delete_request_id TEXT;
