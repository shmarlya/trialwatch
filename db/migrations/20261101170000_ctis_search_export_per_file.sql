-- migrate:up

-- An export is the result of one search: its filters give each row a meaning (e.g. "Phase II-IV",
-- or "has a paediatric investigation plan"). The same row in two exports of the same day is two
-- facts, so the file name joins the key; loading the same file twice still adds nothing.
ALTER TABLE ctis.search_export_snapshot
    DROP CONSTRAINT search_export_snapshot_ct_number_captured_on_content_hash_key,
    ADD CONSTRAINT search_export_snapshot_row_key UNIQUE (ct_number, captured_on, source_file, content_hash);


-- migrate:down
-- The old key allows one copy of a row per day: later copies (other files) are deleted first.
DELETE FROM ctis.search_export_snapshot a
USING ctis.search_export_snapshot b
WHERE a.ct_number = b.ct_number AND a.captured_on = b.captured_on AND a.content_hash = b.content_hash
  AND a.id > b.id;
ALTER TABLE ctis.search_export_snapshot
    DROP CONSTRAINT search_export_snapshot_row_key,
    ADD CONSTRAINT search_export_snapshot_ct_number_captured_on_content_hash_key UNIQUE (ct_number, captured_on, content_hash);
