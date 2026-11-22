-- migrate:up

-- Recruiting trials first (ADR-008, patient search). A source's list says whether it recruits a trial
-- now (CTIS: overall status "Authorised, recruiting" or "Ongoing, recruiting"); that is kept with the
-- listed trial, re-read at every sync, so the public search can drop a trial the moment its list stops
-- calling it recruiting (the list is fresher than the stored record). Transfers take recruiting trials
-- first, so the search fills up before the long first pass through every trial ends.
-- NULL: the list doesn't say (an older listing, or a source whose list has no status).
SET LOCAL trialwatch.actor = '1';

ALTER TABLE core.source_listing ADD COLUMN recruiting boolean;
COMMENT ON COLUMN core.source_listing.recruiting IS
    'the source''s list says the trial recruits now (CTIS: overall status 3 or 4); NULL: it doesn''t say';

CREATE OR REPLACE VIEW core.evidence_status AS
SELECT ts.source_id, ts.source_key, ts.trial_id,
       l.source_key IS NOT NULL AS listed, l.delisted_at, l.title, l.source_updated_on, l.summary,
       CASE
           WHEN e.status IS NULL THEN 'not transferred'
           WHEN e.status = 'failed' THEN 'failed'
           WHEN l.source_updated_on >= (e.transferred_at AT TIME ZONE 'UTC')::date THEN 'stale'
           ELSE 'complete'
       END AS status,
       e.transferred_at, e.ingest_run_id, e.document_kinds, e.documents, e.pages, e.errors,
       l.recruiting
FROM core.trial_source ts
LEFT JOIN core.source_listing l USING (source_id, source_key)
LEFT JOIN core.evidence_transfer e USING (source_id, source_key);


-- migrate:down
SET LOCAL trialwatch.actor = '1';

DROP VIEW core.evidence_status;
CREATE VIEW core.evidence_status AS
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
ALTER TABLE core.source_listing DROP COLUMN recruiting;
