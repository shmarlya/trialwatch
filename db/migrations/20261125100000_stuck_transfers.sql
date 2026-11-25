-- migrate:up

-- A trial whose transfer fails on day after day (a file too large to download, a PDF that always breaks)
-- was transferred again by every refresh, its documents with it. `failures` counts the days its
-- transfer failed in a row (several failed attempts on one day, e.g. a job's retries during an outage,
-- count once), `failed_on` the last of them; a complete transfer sets them back. From the third day it
-- is `stuck`: the refresh leaves it for a person, until the source changes it.
ALTER TABLE core.evidence_transfer
    ADD COLUMN failures integer NOT NULL DEFAULT 0 CHECK (failures >= 0),
    ADD COLUMN failed_on date;

CREATE OR REPLACE VIEW core.evidence_status AS
 SELECT ts.source_id,
    ts.source_key,
    ts.trial_id,
    (l.source_key IS NOT NULL) AS listed,
    l.delisted_at,
    l.title,
    l.source_updated_on,
    l.summary,
        CASE
            WHEN (e.status IS NULL) THEN 'not transferred'::text
            WHEN (e.status = 'failed'::text) THEN 'failed'::text
            WHEN (l.source_updated_on >= ((e.transferred_at AT TIME ZONE 'UTC'::text))::date) THEN 'stale'::text
            ELSE 'complete'::text
        END AS status,
    e.transferred_at,
    e.ingest_run_id,
    e.document_kinds,
    e.documents,
    e.pages,
    e.errors,
    l.recruiting,
    COALESCE(e.failures, 0) AS failures,
    COALESCE(e.status = 'failed'::text AND e.failures >= 3
        AND NOT COALESCE(l.source_updated_on >= e.failed_on, false), false) AS stuck
   FROM ((core.trial_source ts
     LEFT JOIN core.source_listing l USING (source_id, source_key))
     LEFT JOIN core.evidence_transfer e USING (source_id, source_key));

-- migrate:down

-- a view can't lose columns in place
DROP VIEW core.evidence_status;

CREATE VIEW core.evidence_status AS
 SELECT ts.source_id,
    ts.source_key,
    ts.trial_id,
    (l.source_key IS NOT NULL) AS listed,
    l.delisted_at,
    l.title,
    l.source_updated_on,
    l.summary,
        CASE
            WHEN (e.status IS NULL) THEN 'not transferred'::text
            WHEN (e.status = 'failed'::text) THEN 'failed'::text
            WHEN (l.source_updated_on >= ((e.transferred_at AT TIME ZONE 'UTC'::text))::date) THEN 'stale'::text
            ELSE 'complete'::text
        END AS status,
    e.transferred_at,
    e.ingest_run_id,
    e.document_kinds,
    e.documents,
    e.pages,
    e.errors,
    l.recruiting
   FROM ((core.trial_source ts
     LEFT JOIN core.source_listing l USING (source_id, source_key))
     LEFT JOIN core.evidence_transfer e USING (source_id, source_key));

ALTER TABLE core.evidence_transfer DROP COLUMN failed_on, DROP COLUMN failures;
