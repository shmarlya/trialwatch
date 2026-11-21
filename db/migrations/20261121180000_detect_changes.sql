-- migrate:up

-- Two changes a transfer could miss before.
--
-- 1. A trial changed later on the same day it was transferred. The source's list gives only the day
--    it last changed a trial, so a transfer made on that day can't tell whether a change came after
--    it: the evidence now counts as stale whenever the list's day is the transfer's day or later
--    (until a transfer on a later day). The transfer's day is taken in UTC; CTIS dates its changes in
--    European time, which is ahead of UTC, so a change made after a transfer always has a day on or
--    after the transfer's UTC day.
-- 2. A source replaced a document's file without changing its version label. Transfers now download
--    the current file of every listed document and compare its bytes with the version held, so the
--    same label can belong to several versions of a document (their files tell them apart).
SET LOCAL trialwatch.actor = '1';

CREATE OR REPLACE VIEW core.evidence_status AS
SELECT ts.source_id, ts.source_key, ts.trial_id,
       l.source_key IS NOT NULL AS listed, l.delisted_at, l.title, l.source_updated_on, l.summary,
       CASE
           WHEN e.status IS NULL THEN 'not transferred'
           WHEN e.status = 'failed' THEN 'failed'
           WHEN l.source_updated_on >= (e.transferred_at AT TIME ZONE 'UTC')::date THEN 'stale'
           ELSE 'complete'
       END AS status,
       e.transferred_at, e.ingest_run_id, e.document_kinds, e.documents, e.pages, e.errors
FROM core.trial_source ts
LEFT JOIN core.source_listing l USING (source_id, source_key)
LEFT JOIN core.evidence_transfer e USING (source_id, source_key);

ALTER TABLE core.document_version DROP CONSTRAINT document_version_document_id_version_key;
COMMENT ON COLUMN core.document_version.version IS
    'the source''s version label (CTIS systemVersion), or 1 for an upload; a source may replace a file without '
    'changing it, so a label can repeat: file_sha256 tells the versions apart';


-- migrate:down
SET LOCAL trialwatch.actor = '1';
COMMENT ON COLUMN core.document_version.version IS NULL;
-- fails if a source replaced a file under the same label since: those versions can't share it
ALTER TABLE core.document_version ADD CONSTRAINT document_version_document_id_version_key UNIQUE (document_id, version);
CREATE OR REPLACE VIEW core.evidence_status AS
SELECT ts.source_id, ts.source_key, ts.trial_id,
       l.source_key IS NOT NULL AS listed, l.delisted_at, l.title, l.source_updated_on, l.summary,
       CASE
           WHEN e.status IS NULL THEN 'not transferred'
           WHEN e.status = 'failed' THEN 'failed'
           WHEN l.source_updated_on > coalesce(e.source_updated_on, (e.transferred_at AT TIME ZONE 'UTC')::date)
               THEN 'stale'
           ELSE 'complete'
       END AS status,
       e.transferred_at, e.ingest_run_id, e.document_kinds, e.documents, e.pages, e.errors
FROM core.trial_source ts
LEFT JOIN core.source_listing l USING (source_id, source_key)
LEFT JOIN core.evidence_transfer e USING (source_id, source_key);
