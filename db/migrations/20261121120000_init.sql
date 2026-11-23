-- migrate:up

-- TrialWatch's database. Nothing is in production yet, so everything up to 2026-11-23 is in this one
-- migration, under a version of its own: a database made with the earlier migrations has this
-- version and the later ones in schema_migrations, and keeps its data (the schema is the same). New
-- changes get new files; they are folded in here again before the first deploy.
--
-- Seven schemas (ADR-003 to ADR-008):
-- - core: the central model, the same whatever a trial or document came from: who changed what
--   (actor, audit_log), where data comes from (source, ingest_run, source_listing,
--   evidence_transfer), trials (identifiers, records, sponsors, locations, profiles), documents
--   (file → document → version → page), and what paid API calls cost (api_call, app_setting).
-- - checks: what our checks answered: runs, questions, evidence, judgements, their traces and
--   publications.
-- - auth: who can sign in to the web interface (ADR-006).
-- - jobs: queued workflow steps and their schedules, run by `trialwatch worker`.
-- - geo, vocab: reference data for patient search (GeoNames places, MeSH concepts), loaded offline.
-- - search: the patient search index and AI tags (ADR-008).
-- Gold datasets (hand-checked answers) are versioned files in data/gold/, never in this database.

-- Every session on this database shows and parses timestamps in UTC, whatever the server's or
-- client's own setting. Columns are timestamptz (stored as UTC instants either way); this only fixes
-- how they are displayed and how zone-less input like '2026-10-31 12:00' is read. A client can still
-- override it per session (e.g. a GUI sending its own TimeZone).
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET TimeZone TO %L', current_database(), 'UTC');
END
$$;

CREATE SCHEMA core;
CREATE SCHEMA checks;

-- === who and where from =========================================================================

-- A person or a process (an adapter run, a check) that changes data. Every write to an audited table
-- must say who is writing: the session sets trialwatch.actor to an actor id (trialwatch.db.connect
-- does it), and the audit trigger refuses writes without one.
CREATE TABLE core.actor (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('person', 'process')),
    -- a person's login name, or a process name such as ctis-adapter
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (kind, name)
);
-- One actor for everything no person does: TrialWatch itself, so migrations, seeding the first admin,
-- scheduled jobs, automatic checks and automatic publishing are all recorded as the same process.
-- Older process actors (ctis-adapter, evidence-check, admin-seed) may still be named in the audit log.
COMMENT ON TABLE core.actor IS 'a person, or a process; actor 1 (process trialwatch) is TrialWatch itself: '
    'migrations, scheduled jobs, automatic checks and publishing';
INSERT INTO core.actor (kind, name) VALUES ('process', 'trialwatch');

-- Every insert, update and delete on an audited table, written by core.audit() in the same
-- transaction. Never updated, deleted or truncated (the triggers below refuse it).
CREATE TABLE core.audit_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor_id   bigint NOT NULL REFERENCES core.actor,
    action     text NOT NULL CHECK (action IN ('insert', 'update', 'delete')),
    table_name text NOT NULL,
    -- the changed row's key, e.g. {"id": 12} or {"trial_id": 3, "document_id": 7}
    row_key    jsonb NOT NULL,
    -- the row before and after, without large columns (file bytes, raw payloads)
    old_row    jsonb,
    new_row    jsonb,
    -- why, when the writer gave a reason (the session's trialwatch.note, e.g. "ingest run 12")
    note       text
);
CREATE INDEX audit_log_row_idx ON core.audit_log (table_name, row_key);
CREATE INDEX audit_log_actor_idx ON core.audit_log (actor_id, at);

-- Trigger function for audited tables. Arguments: the key columns, comma-separated, then
-- optionally the columns to leave out of old_row / new_row, comma-separated.
CREATE FUNCTION core.audit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actor_setting text := current_setting('trialwatch.actor', true);
    key_columns   text[] := string_to_array(TG_ARGV[0], ',');
    left_out      text[] := coalesce(string_to_array(nullif(TG_ARGV[1], ''), ','), '{}');
    old_json      jsonb;
    new_json      jsonb;
    key_json      jsonb := '{}';
    key_column    text;
BEGIN
    IF actor_setting IS NULL OR actor_setting = '' THEN
        RAISE EXCEPTION 'who is writing to %? set trialwatch.actor to a core.actor id first', TG_TABLE_NAME
            USING HINT = 'trialwatch.db.connect(actor=...) sets it';
    END IF;
    IF TG_OP <> 'INSERT' THEN
        old_json := to_jsonb(OLD) - left_out;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        new_json := to_jsonb(NEW) - left_out;
    END IF;
    FOREACH key_column IN ARRAY key_columns LOOP
        key_json := key_json || jsonb_build_object(key_column, coalesce(new_json, old_json) -> key_column);
    END LOOP;
    -- an update that changes nothing (e.g. ON CONFLICT ... DO UPDATE to the same values) is not logged
    IF TG_OP = 'UPDATE' AND old_json = new_json THEN
        RETURN NEW;
    END IF;
    INSERT INTO core.audit_log (actor_id, action, table_name, row_key, old_row, new_row, note)
    VALUES (actor_setting::bigint, lower(TG_OP), TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, key_json,
            old_json, new_json, nullif(current_setting('trialwatch.note', true), ''));
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION core.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END
$$;

CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON core.audit_log
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();
-- TRUNCATE fires no row triggers, so it is refused separately
CREATE TRIGGER audit_log_never_truncated BEFORE TRUNCATE ON core.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION core.refuse_change();

-- The rows this migration adds are written by TrialWatch itself (actor 1)
SET LOCAL trialwatch.actor = '1';
CREATE TRIGGER actor_audit AFTER UPDATE OR DELETE ON core.actor
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');

-- Where trials and documents come from (ADR-005): a registry such as CTIS, a publication index, or a
-- person entering them. A source names the adapter that reads it (src/trialwatch/sources/registry.py)
-- and holds its config, one current config checked by the adapter; every run records the config it
-- used. Each stored record carries (source, the source's own key), unique, so fetching it again adds a
-- version instead of a duplicate.
CREATE TABLE core.source (
    id         smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- short stable name used in commands and URLs, e.g. ctis
    code       text NOT NULL UNIQUE CHECK (code ~ '^[a-z][a-z0-9-]*$'),
    kind       text NOT NULL CHECK (kind IN ('registry', 'publications', 'manual')),
    name       text NOT NULL,
    url        text,
    -- the adapter that reads it; one source per adapter
    adapter    text NOT NULL UNIQUE,
    enabled    boolean NOT NULL DEFAULT true,
    -- what to list and which evidence to transfer, e.g. for CTIS
    -- {"listing": {"criteria": {...}, "page_size": 500}, "evidence": {"document_kinds": [...]}}
    config     jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by bigint NOT NULL REFERENCES core.actor
);
CREATE TRIGGER source_audit AFTER INSERT OR UPDATE OR DELETE ON core.source
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');
INSERT INTO core.source (code, kind, name, url, adapter, config, created_by) VALUES
    ('ctis', 'registry', 'EU Clinical Trials Information System', 'https://euclinicaltrials.eu', 'ctis',
     '{"listing": {"criteria": {}, "page_size": 500}, "evidence": {"document_kinds": ["results-summary", "study-report"]}}',
     1),
    ('manual', 'manual', 'Entered by a person in TrialWatch', NULL, 'manual', '{}', 1);

-- One run of a source adapter: syncing a source's list (kind 'list') or transferring trials'
-- evidence (kind 'transfer'): what it was asked with the config it used, how it ended and what it
-- added. Its writes carry the note "ingest run <id>" in the audit log. A run whose process died is
-- closed as failed by the next run (it holds an advisory lock while it is alive).
CREATE TABLE core.ingest_run (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id   smallint NOT NULL REFERENCES core.source,
    kind        text NOT NULL CHECK (kind IN ('list', 'transfer')),
    actor_id    bigint NOT NULL REFERENCES core.actor,
    args        jsonb NOT NULL DEFAULT '{}',
    status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    -- e.g. {"trials": 3, "complete": 2, "documents": 5, "new_versions": 2, "failed": 1}
    counts      jsonb NOT NULL DEFAULT '{}',
    -- what failed, per trial or document
    errors      jsonb NOT NULL DEFAULT '{}',
    -- the job it ran for (jobs.job, its foreign key is added with that table), named by the worker for
    -- its session (trialwatch.job)
    job_id      bigint DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint,
    CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE INDEX ingest_run_source_idx ON core.ingest_run (source_id, started_at DESC);

-- === trials =====================================================================================
--
-- Trial identity: core.trial is our own id. core.trial_identifier maps every known identifier
-- (scheme + normalised value, e.g. eu-ct 2022-500044-38-01, nct NCT01234567) to exactly one trial;
-- incoming records are matched by all their identifiers, and when those point at different trials a
-- core.possible_duplicate is recorded for a person instead of merging (a wrong merge mixes two
-- trials' results).

-- Identifier schemes we recognise, what they identify, and the pattern a normalised value must match
CREATE TABLE core.identifier_scheme (
    code       text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9-]*$'),
    label      text NOT NULL,
    pattern    text NOT NULL,
    identifies text NOT NULL DEFAULT 'trial' CHECK (identifies IN ('trial', 'organisation'))
);
INSERT INTO core.identifier_scheme (code, label, pattern, identifies) VALUES
    ('eu-ct', 'EU CT number (CTIS)', '^\d{4}-\d{6}-\d{2}-\d{2}$', 'trial'),
    ('eudract', 'EudraCT number', '^\d{4}-\d{6}-\d{2}$', 'trial'),
    ('nct', 'ClinicalTrials.gov NCT number', '^NCT\d{8}$', 'trial'),
    ('isrctn', 'ISRCTN', '^ISRCTN\d{8}$', 'trial'),
    ('who-utn', 'WHO Universal Trial Number', '^U\d{4}-\d{4}-\d{4}$', 'trial'),
    ('trialwatch', 'Our own number, for trials added by hand without a registry id', '^TW-\d+$', 'trial'),
    ('ema-org', 'EMA organisation id (Organisation Management Service)', '^ORG-\d+$', 'organisation');

CREATE FUNCTION core.check_identifier() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    expected text;
BEGIN
    SELECT pattern INTO expected FROM core.identifier_scheme WHERE code = NEW.scheme;
    IF NEW.value !~ expected THEN
        RAISE EXCEPTION '% is not a valid % identifier (%)', NEW.value, NEW.scheme, expected;
    END IF;
    RETURN NEW;
END
$$;

-- A clinical trial, whatever source told us about it
CREATE TABLE core.trial (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by   bigint NOT NULL REFERENCES core.actor,
    -- the source through which the trial first came in
    first_source smallint NOT NULL REFERENCES core.source,
    -- set when a person confirms this trial duplicates another; it is kept, and reads follow the link
    merged_into  bigint REFERENCES core.trial,
    CHECK (merged_into IS DISTINCT FROM id)
);

CREATE TABLE core.trial_identifier (
    scheme     text NOT NULL REFERENCES core.identifier_scheme,
    -- normalised (upper case, no spaces); checked against the scheme's pattern
    value      text NOT NULL,
    trial_id   bigint NOT NULL REFERENCES core.trial,
    -- the source that gave us this identifier
    source_id  smallint NOT NULL REFERENCES core.source,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by bigint NOT NULL REFERENCES core.actor,
    PRIMARY KEY (scheme, value)
);
CREATE INDEX trial_identifier_trial_idx ON core.trial_identifier (trial_id);
CREATE TRIGGER trial_identifier_valid BEFORE INSERT OR UPDATE ON core.trial_identifier
    FOR EACH ROW EXECUTE FUNCTION core.check_identifier();

-- Which trial a source's own key belongs to: one source key is always one trial
CREATE TABLE core.trial_source (
    source_id  smallint NOT NULL REFERENCES core.source,
    -- the source's own id for the trial, e.g. the EU CT number for CTIS
    source_key text NOT NULL,
    trial_id   bigint NOT NULL REFERENCES core.trial,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by bigint NOT NULL REFERENCES core.actor,
    PRIMARY KEY (source_id, source_key)
);
CREATE INDEX trial_source_trial_idx ON core.trial_source (trial_id);

-- What one source says about a trial, one row per version: a new row only when the content differs
-- from the latest one (content that changes back to an earlier version is a new row again)
CREATE TABLE core.trial_record (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id    smallint NOT NULL,
    source_key   text NOT NULL,
    -- sha256 of the canonical payload, without fields that change on every fetch
    content_hash bytea NOT NULL CHECK (length(content_hash) = 32),
    -- the record as the source gave it
    payload      jsonb COMPRESSION lz4 NOT NULL,
    -- the adapter's normalised reading of it, the same shape for every source: titles, status,
    -- dates, primary endpoints (what checks ask questions about)
    registered   jsonb NOT NULL DEFAULT '{}',
    fetched_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by   bigint NOT NULL REFERENCES core.actor,
    FOREIGN KEY (source_id, source_key) REFERENCES core.trial_source
);
CREATE INDEX trial_record_latest_idx ON core.trial_record (source_id, source_key, fetched_at DESC, id DESC);

-- The latest record per source and trial. Written per trial_source row (not as DISTINCT ON over all
-- records), so a query for one trial reads only that trial's records.
CREATE VIEW core.trial_record_current AS
SELECT s.trial_id, r.id, r.source_id, r.source_key, r.content_hash, r.payload, r.registered, r.fetched_at,
       r.created_by
FROM core.trial_source s
CROSS JOIN LATERAL (
    SELECT * FROM core.trial_record r
    WHERE r.source_id = s.source_id AND r.source_key = s.source_key
    ORDER BY r.fetched_at DESC, r.id DESC
    LIMIT 1
) r;

-- Two trials that may be the same one (their identifiers came in together), for a person to decide
CREATE TABLE core.possible_duplicate (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trial_a    bigint NOT NULL REFERENCES core.trial,
    trial_b    bigint NOT NULL REFERENCES core.trial,
    -- why: which identifiers point at which trial, and from which source
    evidence   jsonb NOT NULL,
    status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'merged', 'distinct')),
    decided_by bigint REFERENCES core.actor,
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by bigint NOT NULL REFERENCES core.actor,
    CHECK (trial_a < trial_b),
    CHECK ((status = 'open') = (decided_at IS NULL)),
    UNIQUE (trial_a, trial_b)
);

-- Sponsors are organisations, stored once however many trials they sponsor; matched like trials: by
-- an identifier a source gives (CTIS gives EMA's organisation id, ORG-…), else, for a name typed by
-- hand, by the same name.
CREATE TABLE core.organisation (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text NOT NULL CHECK (btrim(name) <> ''),
    -- lower case, single-spaced: how a name typed by hand finds an organisation we already hold
    name_key     text GENERATED ALWAYS AS (lower(regexp_replace(btrim(name), '\s+', ' ', 'g'))) STORED,
    -- as the source describes it, e.g. "Pharmaceutical company", "Educational Institution"
    kind         text,
    country      text,
    commercial   boolean,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by   bigint NOT NULL REFERENCES core.actor,
    -- the source through which the organisation first came in
    first_source smallint NOT NULL REFERENCES core.source,
    -- set when a person confirms this organisation is the same as another; it is kept, reads follow the link
    merged_into  bigint REFERENCES core.organisation,
    CHECK (merged_into IS DISTINCT FROM id)
);
CREATE INDEX organisation_name_key_idx ON core.organisation (name_key);

CREATE TABLE core.organisation_identifier (
    scheme          text NOT NULL REFERENCES core.identifier_scheme,
    -- normalised (upper case, no spaces); checked against the scheme's pattern
    value           text NOT NULL,
    organisation_id bigint NOT NULL REFERENCES core.organisation,
    source_id       smallint NOT NULL REFERENCES core.source,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by      bigint NOT NULL REFERENCES core.actor,
    PRIMARY KEY (scheme, value)
);
CREATE INDEX organisation_identifier_organisation_idx ON core.organisation_identifier (organisation_id);
CREATE TRIGGER organisation_identifier_valid BEFORE INSERT OR UPDATE ON core.organisation_identifier
    FOR EACH ROW EXECUTE FUNCTION core.check_identifier();

-- A trial's sponsors: one primary and any number of co-sponsors
CREATE TABLE core.trial_sponsor (
    trial_id        bigint NOT NULL REFERENCES core.trial,
    organisation_id bigint NOT NULL REFERENCES core.organisation,
    -- the trial's main sponsor; the others are co-sponsors
    is_primary      boolean NOT NULL,
    -- the source that said so
    source_id       smallint NOT NULL REFERENCES core.source,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by      bigint NOT NULL REFERENCES core.actor,
    -- no longer a sponsor: kept, with who and when, instead of deleted. 'source': the source stopped
    -- listing it (listing it again brings it back); 'person': a person removed it (it stays removed)
    removed_at      timestamptz,
    removed_by      bigint REFERENCES core.actor,
    removal         text CHECK (removal IN ('source', 'person')),
    CHECK ((removed_at IS NULL) = (removed_by IS NULL) AND (removed_at IS NULL) = (removal IS NULL)),
    PRIMARY KEY (trial_id, organisation_id)
);
CREATE INDEX trial_sponsor_organisation_idx ON core.trial_sponsor (organisation_id);

-- === a source's list and its evidence (ADR-005) ===================================================

-- What a source's list says about a trial it lists: a copy of the list, rewritten by every listing
-- sync (so not in the audit log); the trial itself is in core.trial and core.trial_source.
CREATE TABLE core.source_listing (
    source_id         smallint NOT NULL,
    source_key        text NOT NULL,
    title             text,
    -- the day the source last changed the trial, as its list says
    source_updated_on date,
    -- the list's row as the source gave it (status, phase, sponsor, ...)
    summary           jsonb NOT NULL DEFAULT '{}',
    first_listed_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_listed_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- the listing run that saw it last
    last_run          bigint NOT NULL REFERENCES core.ingest_run,
    -- set when a complete listing sync no longer listed it (kept; listing it again clears it)
    delisted_at       timestamptz,
    -- Recruiting trials first (ADR-008): re-read at every sync, so the public search drops a trial the
    -- moment its list stops calling it recruiting (the list is fresher than the stored record), and
    -- transfers take recruiting trials first
    recruiting        boolean,
    PRIMARY KEY (source_id, source_key),
    FOREIGN KEY (source_id, source_key) REFERENCES core.trial_source
);
COMMENT ON COLUMN core.source_listing.recruiting IS
    'the source''s list says the trial recruits now (CTIS: overall status 3 or 4); NULL: it doesn''t say';

-- Whether a trial's evidence came in from a source: one row per (source, key), replaced by each
-- transfer (its history is in the audit log). 'complete': the record, every document of the kinds
-- transferred and the page text of each came in; 'failed': something did not, with what in errors.
CREATE TABLE core.evidence_transfer (
    source_id         smallint NOT NULL,
    source_key        text NOT NULL,
    status            text NOT NULL CHECK (status IN ('complete', 'failed')),
    transferred_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    transferred_by    bigint NOT NULL REFERENCES core.actor,
    -- the run that did it (none for documents a person added)
    ingest_run_id     bigint REFERENCES core.ingest_run,
    -- the document kinds the transfer brought in (NULL: every kind); a check reading other kinds of
    -- this source must wait for a transfer that brings them
    document_kinds    text[],
    -- the listing's source_updated_on when the transfer ran
    source_updated_on date,
    documents         integer NOT NULL DEFAULT 0,
    pages             integer NOT NULL DEFAULT 0,
    -- what failed: "record", a document's key, or a file -> the error
    errors            jsonb NOT NULL DEFAULT '{}',
    CHECK ((status = 'complete') = (errors = '{}'::jsonb)),
    PRIMARY KEY (source_id, source_key),
    FOREIGN KEY (source_id, source_key) REFERENCES core.trial_source
);

-- Each trial's state at each source: listed or not (and recruiting, as its list says), and its
-- evidence 'not transferred', 'failed', 'stale' (complete, but the source changed the trial since) or
-- 'complete'. The source's list gives only the day it last changed a trial, so a transfer made on that
-- day can't tell whether a change came after it: the evidence is stale whenever the list's day is the
-- transfer's day or later (until a transfer on a later day). The transfer's day is taken in UTC; CTIS
-- dates its changes in European time, ahead of UTC, so a change made after a transfer always has a day
-- on or after the transfer's UTC day.
CREATE VIEW core.evidence_status AS
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

-- === documents ==================================================================================
--
-- Three layers: core.file (the bytes, stored once by sha256) → core.document (the logical document,
-- identity = its source's key) → core.document_version (which file a document had from when;
-- replaced versions are kept). core.trial_document links documents to trials; core.page holds the
-- text of each page of a file.

-- File contents, one row per distinct file. The bytes never change.
CREATE TABLE core.file (
    sha256             bytea PRIMARY KEY CHECK (length(sha256) = 32),
    size_bytes         bigint NOT NULL CHECK (size_bytes = length(content)),
    media_type         text,
    content            bytea STORAGE EXTERNAL NOT NULL,
    -- sha256 of the file's normalised page text, once extracted: the same text in different bytes
    -- (a re-saved or re-uploaded PDF) has the same fingerprint; none for a file without text (scanned)
    text_fingerprint   bytea CHECK (length(text_fingerprint) = 32),
    -- when its page text was extracted (core.page); NULL: not yet
    pages_extracted_at timestamptz,
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by         bigint NOT NULL REFERENCES core.actor
);
CREATE INDEX file_text_fingerprint_idx ON core.file (text_fingerprint) WHERE text_fingerprint IS NOT NULL;

-- Text of each page of a file, for retrieval and for checking quotes
CREATE TABLE core.page (
    file_sha256 bytea NOT NULL REFERENCES core.file,
    -- 1-based, as a PDF viewer counts
    page_no     integer NOT NULL CHECK (page_no >= 1),
    text        text NOT NULL,
    -- library and version that extracted the text, e.g. pypdf 7.1.0
    extractor   text NOT NULL,
    -- 'simple': no stemming or stop words, because documents come in many languages
    tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    PRIMARY KEY (file_sha256, page_no)
);
CREATE INDEX page_tsv_idx ON core.page USING gin (tsv);

-- What kind of document it is, in our own words (each source's types map to these)
CREATE TABLE core.document_kind (
    code        text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9-]*$'),
    label       text NOT NULL,
    -- a kind that reports results
    has_results boolean NOT NULL DEFAULT false
);
INSERT INTO core.document_kind (code, label, has_results) VALUES
    ('results-summary', 'Summary of results', true),
    ('lay-summary', 'Lay summary of results', true),
    ('study-report', 'Clinical study report or synopsis', true),
    ('publication', 'Journal publication', true),
    ('protocol', 'Protocol', false),
    ('other', 'Other', false);

-- A document, as one source identifies it (CTIS document uuid, a DOI, an upload id)
CREATE TABLE core.document (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id  smallint NOT NULL REFERENCES core.source,
    source_key text NOT NULL,
    kind       text NOT NULL REFERENCES core.document_kind,
    title      text,
    -- ISO 639-1, when known
    language   text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by bigint NOT NULL REFERENCES core.actor,
    UNIQUE (source_id, source_key)
);

-- Which file a document had: a new row when the source replaces the file; the old row is kept and
-- gets replaced_at. Transfers download the current file of every listed document and compare its
-- bytes with the version held, so a file replaced under the same version label is caught too.
CREATE TABLE core.document_version (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  bigint NOT NULL REFERENCES core.document,
    version      text NOT NULL,
    file_sha256  bytea NOT NULL REFERENCES core.file,
    -- when the source says this version was submitted or published, if it says (CTIS: the
    -- submission date of the results it belongs to)
    published_at timestamptz,
    fetched_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    replaced_at  timestamptz,
    created_by   bigint NOT NULL REFERENCES core.actor
);
COMMENT ON COLUMN core.document_version.version IS
    'the source''s version label (CTIS systemVersion), or 1 for an upload; a source may replace a file without '
    'changing it, so a label can repeat: file_sha256 tells the versions apart';
CREATE INDEX document_version_file_idx ON core.document_version (file_sha256);
-- at most one current (not replaced) version per document
CREATE UNIQUE INDEX document_version_current_idx ON core.document_version (document_id) WHERE replaced_at IS NULL;

CREATE VIEW core.document_current AS
SELECT d.id, d.source_id, d.source_key, d.kind, d.title, d.language, d.created_at, d.created_by,
       v.id AS version_id, v.version, v.file_sha256, v.published_at, v.fetched_at
FROM core.document d JOIN core.document_version v ON v.document_id = d.id AND v.replaced_at IS NULL;

-- Which documents belong to which trial. A document may belong to several trials.
CREATE TABLE core.trial_document (
    trial_id    bigint NOT NULL REFERENCES core.trial,
    document_id bigint NOT NULL REFERENCES core.document,
    -- how we know: the trial's source lists it, an identifier of the trial is in its text, or a person linked it
    basis       text NOT NULL CHECK (basis IN ('listed-by-source', 'identifier-in-text', 'manual')),
    -- the source that asserted the link
    source_id   smallint NOT NULL REFERENCES core.source,
    -- 1 for a source's listing or a person; lower for a match found in the text
    confidence  real NOT NULL DEFAULT 1 CHECK (confidence > 0 AND confidence <= 1),
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by  bigint NOT NULL REFERENCES core.actor,
    -- unlinked: kept, with who and when, instead of deleted. 'source': the source no longer lists the
    -- document (listing it again links it again); 'person': a person unlinked it (it stays unlinked)
    removed_at  timestamptz,
    removed_by  bigint REFERENCES core.actor,
    removal     text CHECK (removal IN ('source', 'person')),
    CHECK ((removed_at IS NULL) = (removed_by IS NULL) AND (removed_at IS NULL) = (removal IS NULL)),
    PRIMARY KEY (trial_id, document_id)
);
CREATE INDEX trial_document_document_idx ON core.trial_document (document_id);

-- === audit of the central tables ================================================================

-- Every change to these tables is in core.audit_log (large columns left out)
CREATE TRIGGER trial_audit AFTER INSERT OR UPDATE OR DELETE ON core.trial
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');
CREATE TRIGGER trial_identifier_audit AFTER INSERT OR UPDATE OR DELETE ON core.trial_identifier
    FOR EACH ROW EXECUTE FUNCTION core.audit('scheme,value');
CREATE TRIGGER trial_source_audit AFTER INSERT OR UPDATE OR DELETE ON core.trial_source
    FOR EACH ROW EXECUTE FUNCTION core.audit('source_id,source_key');
CREATE TRIGGER trial_record_audit AFTER INSERT OR UPDATE OR DELETE ON core.trial_record
    FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'payload,registered');
CREATE TRIGGER possible_duplicate_audit AFTER INSERT OR UPDATE OR DELETE ON core.possible_duplicate
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');
CREATE TRIGGER organisation_audit AFTER INSERT OR UPDATE OR DELETE ON core.organisation
    FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'name_key');
CREATE TRIGGER organisation_identifier_audit AFTER INSERT OR UPDATE OR DELETE ON core.organisation_identifier
    FOR EACH ROW EXECUTE FUNCTION core.audit('scheme,value');
CREATE TRIGGER trial_sponsor_audit AFTER INSERT OR UPDATE OR DELETE ON core.trial_sponsor
    FOR EACH ROW EXECUTE FUNCTION core.audit('trial_id,organisation_id');
CREATE TRIGGER evidence_transfer_audit AFTER INSERT OR UPDATE OR DELETE ON core.evidence_transfer
    FOR EACH ROW EXECUTE FUNCTION core.audit('source_id,source_key');
CREATE TRIGGER file_audit AFTER INSERT OR UPDATE OR DELETE ON core.file
    FOR EACH ROW EXECUTE FUNCTION core.audit('sha256', 'content');
CREATE TRIGGER document_audit AFTER INSERT OR UPDATE OR DELETE ON core.document
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');
CREATE TRIGGER document_version_audit AFTER INSERT OR UPDATE OR DELETE ON core.document_version
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');
CREATE TRIGGER trial_document_audit AFTER INSERT OR UPDATE OR DELETE ON core.trial_document
    FOR EACH ROW EXECUTE FUNCTION core.audit('trial_id,document_id');
-- File bytes and page text are never changed after they are stored
CREATE TRIGGER file_never_changes BEFORE UPDATE OF content, sha256, size_bytes OR DELETE ON core.file
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER page_never_changes BEFORE UPDATE OR DELETE ON core.page
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();

-- === checks (ADR-004) ===========================================================================
--
-- A check run adds rows and never updates them (except closing the run), so each run is a version:
-- the questions it asked each trial, the evidence the model cited, the judgement, and every step.

-- One run: who started it, the whole configuration (with the exact prompt and requirement-set texts
-- it used and their sha256), and how it ended. A run whose process died is closed as failed by the
-- next run (it holds an advisory lock while it is alive).
CREATE TABLE checks.run (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    check_name          text NOT NULL,
    actor_id            bigint NOT NULL REFERENCES core.actor,
    model               text NOT NULL,
    effort              text NOT NULL,
    -- the prompt file (checks/prompts/<prompt>.md) and its text as sent
    prompt              text NOT NULL,
    prompt_sha256       bytea NOT NULL CHECK (length(prompt_sha256) = 32),
    prompt_text         text NOT NULL,
    -- the requirement set (checks/requirement_sets/<requirements>.json) and its text as read
    requirements        text NOT NULL,
    requirements_sha256 bytea NOT NULL CHECK (length(requirements_sha256) = 32),
    requirements_text   text NOT NULL,
    -- retrieval settings and anything else that shapes the run
    params              jsonb NOT NULL DEFAULT '{}',
    status              text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed', 'stopped')),
    -- why a run ended early (e.g. bad credentials, Ctrl-C, its process died)
    error               text,
    started_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at         timestamptz,
    trials              integer NOT NULL DEFAULT 0,
    errors              integer NOT NULL DEFAULT 0,
    -- tokens and cost: sums of the run's core.api_call rows
    input_tokens        bigint NOT NULL DEFAULT 0,
    output_tokens       bigint NOT NULL DEFAULT 0,
    cache_write_tokens  bigint NOT NULL DEFAULT 0,
    cache_read_tokens   bigint NOT NULL DEFAULT 0,
    cost_usd            numeric(14, 6) NOT NULL DEFAULT 0,
    -- the job it ran for (jobs.job, its foreign key is added with that table)
    job_id              bigint DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint,
    CHECK ((status = 'running') = (finished_at IS NULL))
);

-- What was asked about each trial, and which pages the model was shown for it
CREATE TABLE checks.question (
    run_id    bigint NOT NULL REFERENCES checks.run,
    trial_id  bigint NOT NULL REFERENCES core.trial,
    -- the requirement's key, e.g. participants, endpoint:<registered endpoint id>
    key       text NOT NULL,
    -- the registered item the question is about (e.g. the endpoint's text), if any
    item_text text,
    -- pages shown in the call that answered it: [{"file": <sha256 hex>, "page": n}, ...]
    pages     jsonb NOT NULL,
    -- true when it was asked again with every page
    widened   boolean NOT NULL DEFAULT false,
    PRIMARY KEY (run_id, trial_id, key)
);

-- The evidence the model cited for a question, and whether its quote is really on that page
CREATE TABLE checks.evidence (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id              bigint NOT NULL,
    trial_id            bigint NOT NULL,
    key                 text NOT NULL,
    -- true: found; false: not found; NULL: the model said it doesn't know
    found               boolean,
    -- numeric_result | narrative_only | chart_only | redacted | not_analysed | other
    kind                text,
    quote               text,
    -- the document version and page the quote cites
    document_version_id bigint REFERENCES core.document_version,
    file_sha256         bytea REFERENCES core.file,
    page_no             integer,
    note                text,
    -- the quote occurs (normalised) on the cited page; unverified evidence is ignored by the rules
    verified            boolean NOT NULL,
    FOREIGN KEY (run_id, trial_id, key) REFERENCES checks.question
);
CREATE INDEX evidence_question_idx ON checks.evidence (run_id, trial_id, key);

-- The judgement per trial per run. A part is NULL when undecided (a person decides), never a guess.
CREATE TABLE checks.judgement (
    run_id             bigint NOT NULL REFERENCES checks.run,
    trial_id           bigint NOT NULL REFERENCES core.trial,
    -- full | not full | undecided; NULL only with an error
    label              text CHECK (label IN ('full', 'not full', 'undecided')),
    -- the parts by name, as the requirement set names them, e.g. {"participants": true, ...}
    parts              jsonb NOT NULL DEFAULT '{}',
    -- per question and part: the value, the rule that set it and the evidence id it rests on
    trace              jsonb,
    -- the inputs read at that moment: the trial record and the document versions
    trial_record_id    bigint REFERENCES core.trial_record,
    document_versions  bigint[] NOT NULL DEFAULT '{}',
    -- the model that actually answered (another one when a refusal fell back), per call
    served_by          text[],
    -- tokens and cost: sums of the trial's core.api_call rows in the run
    input_tokens       integer,
    output_tokens      integer,
    duration_ms        integer,
    error              text,
    cache_write_tokens integer,
    cache_read_tokens  integer,
    cost_usd           numeric(14, 6),
    CHECK ((label IS NULL) = (error IS NOT NULL)),
    PRIMARY KEY (run_id, trial_id)
);
CREATE INDEX judgement_trial_idx ON checks.judgement (trial_id, run_id DESC);

-- Every step of judging one trial, in order: the questions, the pages, retrieval, each model call
-- (the request as sent and the raw answer), quote verification and the rules. Pages are referenced by
-- file and page number, not copied; the prompt is in checks.run.
CREATE TABLE checks.step (
    run_id      bigint NOT NULL REFERENCES checks.run,
    trial_id    bigint NOT NULL REFERENCES core.trial,
    seq         integer NOT NULL,
    -- questions | pages | retrieval | call | widening | verification | rules | error
    name        text NOT NULL,
    at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    duration_ms integer,
    data        jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, trial_id, seq)
);

-- The latest successful judgement of each trial against each requirement set, from a run that has
-- ended, and whether its inputs have changed since (outdated): a document version it read was
-- replaced or unlinked, a document of the kinds its requirement set reads is linked now in a version
-- it did not read, or another record of the trial came in after the run started.
CREATE VIEW checks.judgement_current AS
SELECT DISTINCT ON (j.trial_id, r.requirements)
       j.run_id, j.trial_id, j.label, j.parts, j.trace, j.trial_record_id, j.document_versions, j.served_by,
       j.input_tokens, j.output_tokens, j.duration_ms, r.model, r.prompt, r.requirements, r.started_at,
       (EXISTS (SELECT FROM core.document_version v
                JOIN core.trial_document l ON l.document_id = v.document_id AND l.trial_id = j.trial_id
                WHERE v.id = ANY (j.document_versions) AND (v.replaced_at IS NOT NULL OR l.removed_at IS NOT NULL))
        OR EXISTS (SELECT FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
                   WHERE l.trial_id = j.trial_id AND l.removed_at IS NULL
                     AND NOT (d.version_id = ANY (j.document_versions))
                     AND d.kind IN (SELECT jsonb_array_elements_text(r.requirements_text::jsonb -> 'document_kinds')))
        OR EXISTS (SELECT FROM core.trial_record_current c
                   WHERE c.trial_id = j.trial_id AND c.id IS DISTINCT FROM j.trial_record_id
                     AND c.fetched_at > r.started_at)) AS outdated
FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
WHERE j.error IS NULL AND r.status <> 'running'
ORDER BY j.trial_id, r.requirements, j.run_id DESC;

-- Everything is public (ADR-007, revised 2026-11-22): every judgement without an error is published by
-- its run as the run finishes, whatever its label and however often judgements of its kind agreed with
-- the gold datasets; that agreement is recorded with it (confidence, when measured, and basis, always).
-- No person publishes. Staff may withdraw a judgement with a reason; a withdrawn one stays public,
-- marked withdrawn with its reason, so nothing disappears silently. A publication is never deleted.
CREATE TABLE checks.publication (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id bigint NOT NULL,
    trial_id bigint NOT NULL,
    published_by bigint NOT NULL REFERENCES core.actor (id),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    withdrawn_by bigint REFERENCES core.actor (id),
    withdrawn_at timestamptz,
    note text,
    confidence real CHECK (confidence >= 0 AND confidence <= 1),
    basis text,
    FOREIGN KEY (run_id, trial_id) REFERENCES checks.judgement (run_id, trial_id),
    -- named as the squashed migrations left it (an earlier check took the name publication_check)
    CONSTRAINT publication_check1 CHECK ((withdrawn_at IS NULL) = (withdrawn_by IS NULL))
);
COMMENT ON TABLE checks.publication IS 'every finished judgement without an error, published by its run (ADR-007); withdrawn ones stay, marked';
COMMENT ON COLUMN checks.publication.note IS 'why staff withdrew it: shown to the public';
COMMENT ON COLUMN checks.publication.confidence IS
    'share of gold items judgements of its kind agreed with, once enough were compared (checks/publishing.py); NULL: not measured';
COMMENT ON COLUMN checks.publication.basis IS 'how its kind compared with gold labels when it was published';
-- a judgement is published at most once at a time
CREATE UNIQUE INDEX publication_live_idx ON checks.publication (run_id, trial_id) WHERE withdrawn_at IS NULL;
CREATE INDEX publication_trial_idx ON checks.publication (trial_id);

-- Only a finished judgement without an error can be published: the public never sees a run in progress
CREATE FUNCTION checks.publishable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NOT EXISTS (SELECT FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
                   WHERE j.run_id = NEW.run_id AND j.trial_id = NEW.trial_id
                     AND j.error IS NULL AND r.status <> 'running') THEN
        RAISE EXCEPTION 'judgement of trial % in run % can''t be published: it has an error or its run is still running',
            NEW.trial_id, NEW.run_id;
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER publication_publishable BEFORE INSERT ON checks.publication
    FOR EACH ROW EXECUTE FUNCTION checks.publishable();
CREATE TRIGGER publication_audit AFTER INSERT OR DELETE OR UPDATE ON checks.publication
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');

-- The judgement the public sees per trial and requirement set: the newest published one, withdrawn or
-- not (a withdrawn one says so, with its reason), with whether its inputs changed since (as
-- checks.judgement_current says for every judgement)
CREATE VIEW checks.judgement_published AS
SELECT DISTINCT ON (j.trial_id, r.requirements)
       j.run_id, j.trial_id, j.label, j.parts, j.trace, j.trial_record_id, j.document_versions,
       r.model, r.prompt, r.requirements, r.started_at, p.published_at, p.confidence, p.basis,
       ((EXISTS (SELECT FROM core.document_version v
                   JOIN core.trial_document l ON l.document_id = v.document_id AND l.trial_id = j.trial_id
                  WHERE v.id = ANY (j.document_versions) AND (v.replaced_at IS NOT NULL OR l.removed_at IS NOT NULL)))
        OR (EXISTS (SELECT FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
                     WHERE l.trial_id = j.trial_id AND l.removed_at IS NULL
                       AND NOT (d.version_id = ANY (j.document_versions))
                       AND d.kind IN (SELECT jsonb_array_elements_text(r.requirements_text::jsonb -> 'document_kinds'))))
        OR (EXISTS (SELECT FROM core.trial_record_current c
                     WHERE c.trial_id = j.trial_id AND c.id IS DISTINCT FROM j.trial_record_id
                       AND c.fetched_at > r.started_at))) AS outdated,
       p.withdrawn_at, p.note AS withdrawn_note
FROM checks.publication p
JOIN checks.judgement j ON j.run_id = p.run_id AND j.trial_id = p.trial_id
JOIN checks.run r ON r.id = j.run_id
ORDER BY j.trial_id, r.requirements, j.run_id DESC, p.id DESC;

-- === spend ======================================================================================
--
-- What TrialWatch spends on paid APIs. Every billed model attempt is one core.api_call row with its
-- tokens, the price list it was charged at and its cost in US dollars: the one source of every money
-- total (per call, trial, run, job, month). Prices are the current list in
-- src/trialwatch/costs/prices.json, copied into each row, so a later price change never rewrites
-- what was spent. A model missing from the list is recorded unpriced (cost_usd NULL), never as $0.
CREATE TABLE core.api_call (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- who the call was made for: the person who started the check, or trialwatch
    actor_id           bigint NOT NULL REFERENCES core.actor,
    provider           text NOT NULL DEFAULT 'anthropic',
    -- the model that ran this attempt (after a fallback, the fallback model)
    model              text NOT NULL,
    -- what the call was for: check, annotate
    operation          text NOT NULL,
    check_run_id       bigint REFERENCES checks.run,
    trial_id           bigint REFERENCES core.trial,
    -- the call's number within the trial (1: first call, 2: widening), as in its trace step
    call_number        integer,
    input_tokens       bigint NOT NULL DEFAULT 0,
    output_tokens      bigint NOT NULL DEFAULT 0,
    cache_write_tokens bigint NOT NULL DEFAULT 0,
    cache_read_tokens  bigint NOT NULL DEFAULT 0,
    -- NULL: the model is not in the price list
    cost_usd           numeric(14, 6),
    -- the prices (US dollars per million tokens) the cost was computed with
    prices             jsonb,
    -- the provider's request id, to match the provider's own bills
    request_id         text,
    -- the job it was made for (jobs.job), and the AI tagging run (search.annotation_run): their
    -- foreign keys are added with those tables
    job_id             bigint DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint,
    annotation_run_id  bigint
);
COMMENT ON TABLE core.api_call IS 'every billed call to a paid API, with its tokens and cost: the source of all spend totals';
CREATE INDEX api_call_at_idx ON core.api_call (at);
CREATE INDEX api_call_run_idx ON core.api_call (check_run_id, trial_id);

-- Settings an admin changes in the web interface, one current value each, audited
CREATE TABLE core.app_setting (
    key        text PRIMARY KEY,
    value      jsonb,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER app_setting_audit AFTER INSERT OR DELETE OR UPDATE ON core.app_setting
    FOR EACH ROW EXECUTE FUNCTION core.audit('key');
-- The most automatic checks may spend in a calendar month (UTC), in US dollars; null: no cap
INSERT INTO core.app_setting (key, value) VALUES ('spend.monthly_cap_usd', 'null');

-- === sign-in (ADR-006) ==========================================================================
--
-- Each user signs in with their email and a password and has one role (admin or moderator). What a
-- role may do is the chart in src/trialwatch/auth/actions.py, not data here. Each user is also a
-- core.actor, so their writes are audited under their own name.
CREATE SCHEMA auth;

CREATE TABLE auth.app_user (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- lower case, so one address is one user however it is typed; also their core.actor name
    email text NOT NULL UNIQUE CHECK (email = lower(email) AND length(email) <= 254
                                      AND email ~ '^[^@/[:space:]]+@[^@/[:space:]]+\.[^@/[:space:]]+$'),
    -- argon2id; never written to the audit log
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'moderator')),
    actor_id bigint NOT NULL UNIQUE REFERENCES core.actor (id),
    disabled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE auth.app_user IS 'people who can sign in to the web interface; their role picks their actions (auth/actions.py)';

-- The cookie holds a random token. Only its SHA-256 is kept, so reading this table gives no live sessions.
CREATE TABLE auth.session (
    token_sha256 bytea PRIMARY KEY CHECK (length(token_sha256) = 32),
    user_id bigint NOT NULL REFERENCES auth.app_user (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX session_user_idx ON auth.session (user_id);

CREATE TRIGGER app_user_audit AFTER INSERT OR DELETE OR UPDATE ON auth.app_user
    FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'password_hash');

-- === jobs =======================================================================================
--
-- The workflow steps (sync a source's list, transfer evidence, tag recruiting trials, check trials)
-- and the refresh that chains them, queued in the database and run by `trialwatch worker`. A job
-- survives a restart; a failed attempt is tried again later (src/trialwatch/jobs/retry.py says how
-- often and when); a job that ran out of attempts stays failed until someone retries it (/admin/jobs).
CREATE SCHEMA jobs;

-- When a job is due by itself: daily (at its hour), weekly (on its weekday, at its hour) or monthly
-- (on the 1st, at its hour). Schedules start switched off: an admin switches one on at /admin/jobs,
-- so a fresh database never starts paid checks by itself.
CREATE TABLE jobs.schedule (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    kind        text NOT NULL CHECK (kind IN ('sync', 'transfer', 'check', 'refresh', 'annotate')),
    params      jsonb NOT NULL DEFAULT '{}',
    weekday     smallint NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    -- the hour, UTC
    hour_utc    smallint NOT NULL CHECK (hour_utc BETWEEN 0 AND 23),
    enabled     boolean NOT NULL DEFAULT false,
    next_run_at timestamptz NOT NULL,
    frequency   text NOT NULL DEFAULT 'weekly' CHECK (frequency IN ('daily', 'weekly', 'monthly'))
);
COMMENT ON COLUMN jobs.schedule.weekday IS 'ISO day of the week (1 Monday .. 7 Sunday); used when weekly';
COMMENT ON COLUMN jobs.schedule.frequency IS 'daily | weekly | monthly (on the 1st)';
CREATE TRIGGER schedule_audit AFTER INSERT OR DELETE OR UPDATE ON jobs.schedule
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');

CREATE TABLE jobs.job (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('sync', 'transfer', 'check', 'refresh', 'annotate')),
    -- what to do, e.g. {"source": "ctis", "keys": [...]} or {"trial_ids": [...], "config": "default"}
    params           jsonb NOT NULL DEFAULT '{}',
    -- started by TrialWatch itself (a schedule, a refresh), not a person: the monthly spending cap
    -- holds automatic checks
    automatic        boolean NOT NULL DEFAULT false,
    parent_id        bigint REFERENCES jobs.job,
    schedule_id      integer REFERENCES jobs.schedule,
    -- lower runs first
    priority         smallint NOT NULL DEFAULT 100,
    -- queued: waits for a worker (from run_after); running; waiting: for its child jobs; held: by the
    -- spending cap; succeeded / failed / cancelled: ended
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'waiting', 'held', 'succeeded', 'failed', 'cancelled')),
    attempts         integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL CHECK (max_attempts >= 1),
    run_after        timestamptz NOT NULL DEFAULT clock_timestamp(),
    cancel_requested boolean NOT NULL DEFAULT false,
    last_error       text,
    -- what it did, e.g. counts, the runs it made, what failed
    result           jsonb NOT NULL DEFAULT '{}',
    -- the worker running it, and when it last said it is alive
    locked_by        text,
    heartbeat_at     timestamptz,
    created_by       bigint NOT NULL REFERENCES core.actor,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at       timestamptz,
    finished_at      timestamptz,
    CHECK ((status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL))
);
CREATE INDEX job_queue_idx ON jobs.job (priority, id) WHERE status = 'queued';
CREATE INDEX job_status_idx ON jobs.job (status, id DESC);
CREATE INDEX job_parent_idx ON jobs.job (parent_id);
-- a heartbeat alone is not worth an audit row
CREATE TRIGGER job_audit AFTER INSERT OR DELETE OR UPDATE ON jobs.job
    FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'heartbeat_at');

-- The progress lines of a job, per attempt, as its page shows them while it runs
CREATE TABLE jobs.job_line (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id  bigint NOT NULL REFERENCES jobs.job ON DELETE CASCADE,
    attempt integer NOT NULL,
    at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    line    text NOT NULL
);
CREATE INDEX job_line_job_idx ON jobs.job_line (job_id, id);

-- The job a run or a billed call was made for. The worker names it for its session
-- (trialwatch.job), so the code that starts runs and records calls never has to pass it on.
ALTER TABLE checks.run ADD FOREIGN KEY (job_id) REFERENCES jobs.job;
ALTER TABLE core.ingest_run ADD FOREIGN KEY (job_id) REFERENCES jobs.job;
ALTER TABLE core.api_call ADD FOREIGN KEY (job_id) REFERENCES jobs.job;
CREATE INDEX run_job_idx ON checks.run (job_id);
CREATE INDEX ingest_run_job_idx ON core.ingest_run (job_id);
CREATE INDEX api_call_job_idx ON core.api_call (job_id);

-- The refresh: sync every source's list, transfer what changed, tag recruiting trials, check what
-- changed. Weekly, Monday 03:00 UTC, switched off until an admin switches it on
INSERT INTO jobs.schedule (name, kind, weekday, hour_utc, next_run_at)
VALUES ('weekly-refresh', 'refresh', 1, 3,
        date_trunc('week', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '7 days 3 hours');

-- === places (ADR-008, patient search) ===========================================================
--
-- "diabetes Paris" means trials recruiting at a site within some kilometres of Paris, not trials whose
-- site's city is spelled "Paris" (Gustave Roussy is in Villejuif). So every trial site gets a point
-- (latitude, longitude) and a query's place too, both from GeoNames (CC BY 4.0, credited on the site),
-- loaded offline by `trialwatch geo load`: no geocoding service is called per site or per search.
--
-- geo.country, geo.country_name, geo.place, geo.place_name and geo.postcode are reference data,
-- replaced as a whole by each load: they are not audited row by row, the load run is (geo.load_run,
-- with each file's address and sha256), as core.page and core.source_listing are not audited either.
-- geo.address_point is a derived cache: where each distinct site address (country, postcode, city)
-- is, found by `trialwatch geo geocode` and after each transfer; `geo geocode --again` redoes it.
-- core.trial_location carries the keys of its address, so a site finds its point by a plain join.
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE SCHEMA geo;

-- A name for comparing: lower case, no accents, anything but letters and digits as single spaces
-- ("Saint-Étienne" -> "saint etienne", "München" -> "munchen"). IMMUTABLE so indexes and generated
-- columns can use it: the unaccent dictionary it names is fixed.
CREATE FUNCTION geo.name_key(name text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN btrim(regexp_replace(lower(public.unaccent('public.unaccent'::regdictionary, coalesce(name, ''))),
                                '[^[:alnum:]]+', ' ', 'g'));

-- A postcode for comparing: upper case letters and digits only ("1012 AB" -> "1012AB")
CREATE FUNCTION geo.postcode_key(postcode text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN regexp_replace(upper(coalesce(postcode, '')), '[^[:alnum:]]', '', 'g');

-- Great-circle distance in kilometres (haversine, Earth radius 6371 km)
CREATE FUNCTION geo.distance_km(lat1 double precision, lon1 double precision,
                                lat2 double precision, lon2 double precision) RETURNS double precision
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN 2 * 6371 * asin(least(1, sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2))));

CREATE TABLE geo.load_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- each file read: its address (or local path), sha256 and rows taken
    files jsonb NOT NULL DEFAULT '[]',
    countries text[] NOT NULL,
    places integer,
    postcodes integer,
    license text NOT NULL DEFAULT 'GeoNames, CC BY 4.0 (https://www.geonames.org)',
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    created_by bigint NOT NULL REFERENCES core.actor (id)
);
COMMENT ON TABLE geo.load_run IS 'each load of GeoNames reference data (trialwatch geo load)';
CREATE TRIGGER load_run_audit AFTER INSERT OR DELETE OR UPDATE ON geo.load_run
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');

CREATE TABLE geo.country (
    iso2 char(2) PRIMARY KEY,
    iso3 char(3),
    name text NOT NULL,
    -- the country's languages, most spoken first (GeoNames countryInfo, e.g. {fr-BE,nl-BE,de-BE})
    languages text[] NOT NULL DEFAULT '{}',
    -- a member of the EU/EEA: where CTIS trials have sites
    eea boolean NOT NULL DEFAULT false
);
COMMENT ON TABLE geo.country IS 'countries (GeoNames countryInfo), reference data replaced by each geo load';

CREATE TABLE geo.country_name (
    name_key text NOT NULL,
    iso2 char(2) NOT NULL REFERENCES geo.country (iso2),
    name text NOT NULL,
    PRIMARY KEY (name_key, iso2)
);
COMMENT ON TABLE geo.country_name IS
    'every name we know a country by (English, its own languages, CTIS''s spellings), keyed by geo.name_key';

CREATE TABLE geo.place (
    geonameid bigint PRIMARY KEY,
    name text NOT NULL,
    country_code char(2) NOT NULL,
    population bigint NOT NULL DEFAULT 0,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL
);
COMMENT ON TABLE geo.place IS 'populated places of the EU/EEA with 1000+ people (GeoNames cities1000)';
CREATE INDEX place_country_idx ON geo.place (country_code);

CREATE TABLE geo.place_name (
    geonameid bigint NOT NULL REFERENCES geo.place (geonameid) ON DELETE CASCADE,
    name_key text NOT NULL,
    PRIMARY KEY (name_key, geonameid)
);
COMMENT ON TABLE geo.place_name IS
    'every name of a place (its own, ASCII, and alternate names in other languages: Wien, Vienna, Vienne)';
CREATE INDEX place_name_trgm_idx ON geo.place_name USING gin (name_key gin_trgm_ops);

CREATE TABLE geo.postcode (
    country_code char(2) NOT NULL,
    postcode_key text NOT NULL,
    postcode text NOT NULL,
    place_name text NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL
);
COMMENT ON TABLE geo.postcode IS 'postal codes with a point (GeoNames postal code files), EU/EEA';
CREATE INDEX postcode_idx ON geo.postcode (country_code, postcode_key);

CREATE TABLE geo.address_point (
    country_key text NOT NULL,
    postcode_key text NOT NULL,
    city_key text NOT NULL,
    iso2 char(2),
    latitude double precision,
    longitude double precision,
    -- how it was found: postcode | city | city-fuzzy | none (not found: never in a distance search)
    precision text NOT NULL CHECK (precision IN ('postcode', 'city', 'city-fuzzy', 'none')),
    -- the place or postcode it matched, in words
    matched text,
    geocoded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (country_key, postcode_key, city_key),
    CHECK ((precision = 'none') = (latitude IS NULL))
);
COMMENT ON TABLE geo.address_point IS
    'where each distinct site address is (derived from geo.postcode and geo.place; trialwatch geo geocode)';
CREATE INDEX address_point_latitude_idx ON geo.address_point (latitude) WHERE latitude IS NOT NULL;

-- === where trials run and who can join (ADR-008, patient search) ================================

-- Where a trial runs, as each source says: one row per trial site (the site's organisation, city,
-- postcode and country) and one per country with no site listed, each with the trial's status in that
-- country. Read from the source's record at each transfer (the adapter's `locations`), replacing that
-- source's rows; not an input of the evidence check, so it is not part of a record's `registered`
-- reading and changing how it is read stores no new record version. People at a site (investigators,
-- their emails and phones) are never stored here.
CREATE TABLE core.trial_location (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trial_id bigint NOT NULL REFERENCES core.trial (id),
    source_id smallint NOT NULL REFERENCES core.source (id),
    country text NOT NULL CHECK (btrim(country) <> ''),
    city text,
    postcode text,
    -- the site's organisation (a hospital, a clinic); NULL for a country with no site listed
    site text,
    -- the trial's status in this country, as the source gives it (e.g. Ongoing, recruiting; Ended)
    status text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by bigint NOT NULL REFERENCES core.actor (id),
    -- the address's keys, for finding its point in geo.address_point
    country_key text GENERATED ALWAYS AS (geo.name_key(country)) STORED,
    postcode_key text GENERATED ALWAYS AS (geo.postcode_key(postcode)) STORED,
    city_key text GENERATED ALWAYS AS (geo.name_key(city)) STORED
);
COMMENT ON TABLE core.trial_location IS 'where a trial runs, per source: its sites (city, postcode, country) and countries';
CREATE INDEX trial_location_trial_idx ON core.trial_location (trial_id);
CREATE INDEX trial_location_country_idx ON core.trial_location (lower(country));
CREATE INDEX trial_location_city_idx ON core.trial_location (lower(city));
CREATE INDEX trial_location_address_idx ON core.trial_location (country_key, postcode_key, city_key);
CREATE TRIGGER trial_location_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_location
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');

-- A trial's profile: what someone looking for a trial to join needs, read by a source's adapter from
-- the trial's record into one shape for every source (src/trialwatch/sources/profile.py): conditions
-- with their translations and the coded terms the source publishes (MedDRA), therapeutic areas (MeSH
-- tree branches), phase, who can join (age groups, sex, patients or healthy volunteers, the criteria
-- as written), what is tested, and per country the trial's status and whether it recruits now. Never
-- the people at a site.
--
-- Read at every transfer and replaced when it changed; rebuilt from stored records without fetching
-- (`trialwatch source profiles`). Not part of a record's `registered` reading, so reading it
-- differently stores no new record version and outdates no judgement. The audit log keeps who changed
-- a profile and when, not its (large) content: the record it was read from is kept.
CREATE TABLE core.trial_profile (
    trial_id bigint NOT NULL REFERENCES core.trial (id),
    source_id smallint NOT NULL REFERENCES core.source (id),
    -- the record it was read from (NULL: a source with no stored record)
    trial_record_id bigint REFERENCES core.trial_record (id),
    profile jsonb COMPRESSION lz4 NOT NULL,
    profile_hash bytea NOT NULL CHECK (length(profile_hash) = 32),
    -- the trial recruits now somewhere, as the profile says (the source's list first)
    recruiting boolean NOT NULL,
    read_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by bigint NOT NULL REFERENCES core.actor (id),
    PRIMARY KEY (trial_id, source_id)
);
COMMENT ON TABLE core.trial_profile IS
    'what a person looking for a trial needs, per trial and source, read from its record (sources/profile.py)';
CREATE INDEX trial_profile_recruiting_idx ON core.trial_profile (trial_id) WHERE recruiting;
CREATE TRIGGER trial_profile_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_profile
    FOR EACH ROW EXECUTE FUNCTION core.audit('trial_id,source_id', 'profile,read_at');

-- === medical concepts (ADR-008, patient search) =================================================
--
-- MeSH, the US National Library of Medicine's vocabulary (free to use, credited on the site), so a
-- search for "diabetes" finds a trial filed as "Type 2 Diabetes Mellitus", "T2DM" or "NIDDM", however
-- its sponsor wrote it. Only diseases (tree C) and mental disorders (F03): what a trial's condition is.
-- Each descriptor keeps its entry terms (synonyms, many of them lay words: "Heart Attack" for
-- Myocardial Infarction) and its tree numbers, the hierarchy that makes "Diabetes Mellitus" cover type
-- 1, type 2 and gestational diabetes.
--
-- Reference data replaced as a whole by `trialwatch vocab load`: not audited row by row, the load run
-- is (vocab.load_run, with the file's address and sha256). MedDRA, which CTIS codes conditions in, is
-- licensed: only the MedDRA terms CTIS publishes per trial are kept (in each trial's profile).
CREATE SCHEMA vocab;

-- The words of a name in alphabetical order, each once: "Diabetes Mellitus, Type II" and "Type II
-- diabetes mellitus" are the same words
CREATE FUNCTION vocab.words_key(name text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN array_to_string(ARRAY(SELECT DISTINCT word FROM unnest(string_to_array(geo.name_key(name), ' ')) AS word
                                 WHERE word <> '' ORDER BY word), ' ');

CREATE TABLE vocab.load_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vocabulary text NOT NULL,
    version text NOT NULL,
    file text NOT NULL,
    sha256 text NOT NULL,
    descriptors integer,
    terms integer,
    license text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    created_by bigint NOT NULL REFERENCES core.actor (id)
);
COMMENT ON TABLE vocab.load_run IS 'each load of a vocabulary (trialwatch vocab load)';
CREATE TRIGGER load_run_audit AFTER INSERT OR DELETE OR UPDATE ON vocab.load_run
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');

CREATE TABLE vocab.mesh_descriptor (
    ui text PRIMARY KEY CHECK (ui ~ '^D[0-9]+$'),
    name text NOT NULL,
    tree_numbers text[] NOT NULL
);
COMMENT ON TABLE vocab.mesh_descriptor IS 'MeSH descriptors of diseases (C) and mental disorders (F03)';

CREATE TABLE vocab.mesh_term (
    name_key text NOT NULL,
    ui text NOT NULL REFERENCES vocab.mesh_descriptor (ui) ON DELETE CASCADE,
    term text NOT NULL,
    words_key text NOT NULL,
    PRIMARY KEY (name_key, ui)
);
COMMENT ON TABLE vocab.mesh_term IS 'every entry term of a MeSH descriptor (its name and synonyms), keyed by geo.name_key';
CREATE INDEX mesh_term_words_idx ON vocab.mesh_term (words_key);
CREATE INDEX mesh_term_trgm_idx ON vocab.mesh_term USING gin (name_key gin_trgm_ops);

CREATE TABLE vocab.mesh_tree (
    tree_number text PRIMARY KEY,
    ui text NOT NULL REFERENCES vocab.mesh_descriptor (ui) ON DELETE CASCADE
);
COMMENT ON TABLE vocab.mesh_tree IS 'where each MeSH descriptor sits in the hierarchy (C18.452.394.750 is under C18.452.394)';
CREATE INDEX mesh_tree_ui_idx ON vocab.mesh_tree (ui);

-- === the search index and AI tags (ADR-008, patient search) =====================================
--
-- One search document per trial, built from its profile (core.trial_profile) at every transfer and by
-- `trialwatch search reindex`, so a trial is findable the moment it arrives. Its words carry a weight
-- for where they came from:
--   A  conditions, their translations, the MedDRA terms CTIS publishes, the trial's MeSH concepts
--   B  titles and their translations, lay categories, the concepts above the trial's (MeSH hierarchy)
--   C  what is tested (products, substances, ATC names) and the main objective
--   D  inclusion criteria
-- Exclusion criteria are never indexed: "patients with diabetes are excluded" must not match
-- "diabetes". Words are compared without accents (search.trialwatch: simple + unaccent), so "diabète"
-- and "diabete" meet; no stemming, which differs by language: a search matches word beginnings.
--
-- search.trial_concept: which MeSH descriptors a trial's conditions name, how that was found, and the
-- words it was found in (field and quote), so every concept shown can be checked against the registry.
-- search.term: the words of recruiting trials' conditions and titles, for "did you mean".
-- All three are derived and rebuilt from stored data: not audited.
--
-- AI tags with checked quotes: a model reads a recruiting trial's registry fields (conditions and their
-- translations, titles, objective, inclusion criteria, products) and tags it: plain-language
-- categories, the kind of medicine tested, the trial's purpose, synonyms of its conditions in English
-- and its countries' languages, MeSH concepts from a shortlist, and the exact age limits its inclusion
-- criteria give. Every tag names the field it comes from and quotes it; a tag whose quote is not in
-- that field (as whole words) is dropped, and kept as dropped with why. Tags are shown as AI-made, with
-- the model, the quote and the prompt. search.annotation_run is one run of the annotate step (like
-- checks.run) with the exact prompt text and sha256 it sent; search.annotation is one trial's answer
-- in a run, append-only: the newest for the trial's current profile is what its search document uses.
-- Billed calls are core.api_call rows (operation 'annotate'), and automatic runs are held by the
-- monthly spending cap.
CREATE SCHEMA search;

CREATE TEXT SEARCH CONFIGURATION search.trialwatch (COPY = pg_catalog.simple);
ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ALTER MAPPING FOR asciiword, asciihword, hword_asciipart, word, hword, hword_part WITH public.unaccent, simple;

CREATE TABLE search.annotation_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model text NOT NULL,
    effort text NOT NULL,
    prompt text NOT NULL,
    prompt_sha256 bytea NOT NULL CHECK (length(prompt_sha256) = 32),
    prompt_text text NOT NULL,
    params jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed', 'stopped')),
    error text,
    trials integer NOT NULL DEFAULT 0,
    input_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    cost_usd numeric(14, 6) NOT NULL DEFAULT 0,
    -- the job that ran it (jobs.job), when a worker did
    job_id bigint DEFAULT nullif(current_setting('trialwatch.job', true), '')::bigint REFERENCES jobs.job (id),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    created_by bigint NOT NULL REFERENCES core.actor (id)
);
COMMENT ON TABLE search.annotation_run IS 'a run of AI tagging of recruiting trials, with the exact prompt it sent';
CREATE TRIGGER annotation_run_audit AFTER INSERT OR DELETE OR UPDATE ON search.annotation_run
    FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'prompt_text');
ALTER TABLE core.api_call ADD FOREIGN KEY (annotation_run_id) REFERENCES search.annotation_run (id);

CREATE TABLE search.annotation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES search.annotation_run (id),
    trial_id bigint NOT NULL REFERENCES core.trial (id),
    -- the profile it read: an annotation of another profile is not used
    profile_hash bytea NOT NULL CHECK (length(profile_hash) = 32),
    served_by text,
    -- the tags whose quotes were found in their field, and the ones dropped, each with why
    facts jsonb NOT NULL DEFAULT '{}',
    dropped jsonb NOT NULL DEFAULT '[]',
    -- the request as sent, without the system prompt (kept once with the run), and the answer as given
    request jsonb COMPRESSION lz4,
    raw_answer text,
    error text,
    input_tokens integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    cost_usd numeric(14, 6),
    duration_ms integer,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, trial_id)
);
COMMENT ON TABLE search.annotation IS 'one trial''s AI tags in a run: kept facts with their quotes, dropped ones with why';
CREATE INDEX annotation_trial_idx ON search.annotation (trial_id, profile_hash, id DESC);

CREATE TABLE search.document (
    trial_id bigint PRIMARY KEY REFERENCES core.trial (id),
    source_id smallint NOT NULL REFERENCES core.source (id),
    -- the profile it was built from: the same hash again needs no rebuilding
    profile_hash bytea NOT NULL,
    recruiting boolean NOT NULL,
    -- countries where it recruits now, as the source names them (core.trial_location.country)
    recruiting_countries text[] NOT NULL DEFAULT '{}',
    title text,
    conditions text[] NOT NULL DEFAULT '{}',
    -- lay categories (src/trialwatch/search/categories.json)
    categories text[] NOT NULL DEFAULT '{}',
    phase text,
    age_groups text[] NOT NULL DEFAULT '{}',
    female boolean,
    male boolean,
    healthy_volunteers boolean,
    patients boolean,
    rare boolean NOT NULL DEFAULT false,
    tsv tsvector NOT NULL,
    built_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- the AI tags it used, if any
    annotation_id bigint REFERENCES search.annotation (id),
    -- the exact age limits the inclusion criteria give (AI, with a checked quote), in years
    age_min numeric,
    age_max numeric,
    -- the kind of medicine tested and the trial's purpose (AI, with a checked quote)
    interventions text[] NOT NULL DEFAULT '{}',
    purposes text[] NOT NULL DEFAULT '{}'
);
COMMENT ON TABLE search.document IS 'one per trial, built from its profile: what patient search matches (ADR-008)';
CREATE INDEX document_tsv_idx ON search.document USING gin (tsv);
CREATE INDEX document_recruiting_idx ON search.document (trial_id) WHERE recruiting;

CREATE TABLE search.trial_concept (
    trial_id bigint NOT NULL REFERENCES core.trial (id),
    ui text NOT NULL,
    -- exact-name | same-words | phrase (found in the text without AI) | ai-quote
    method text NOT NULL CHECK (method IN ('exact-name', 'same-words', 'phrase', 'ai-quote')),
    -- which text of the profile it was found in (condition-1, meddra-2) and the words found
    field text NOT NULL,
    quote text NOT NULL,
    PRIMARY KEY (trial_id, ui, field)
);
COMMENT ON TABLE search.trial_concept IS 'the MeSH descriptors a trial''s conditions name, with the words they were found in';

CREATE TABLE search.term (
    term text PRIMARY KEY,
    documents integer NOT NULL
);
COMMENT ON TABLE search.term IS 'words of recruiting trials'' conditions and titles, for "did you mean"';
CREATE INDEX term_trgm_idx ON search.term USING gin (term gin_trgm_ops);


-- migrate:down
DROP SCHEMA search CASCADE;
DROP SCHEMA vocab CASCADE;
DROP SCHEMA geo CASCADE;
DROP SCHEMA jobs CASCADE;
DROP SCHEMA auth CASCADE;
DROP SCHEMA checks CASCADE;
DROP SCHEMA core CASCADE;
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I RESET TimeZone', current_database());
END
$$;
