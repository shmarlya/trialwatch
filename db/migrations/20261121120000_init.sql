-- migrate:up

-- TrialWatch's database. Nothing is in production yet, so everything up to 2026-11-21 is in this one
-- migration, under a version of its own: a database made with the earlier migrations is reset
-- (docker compose down -v), never migrated in place. New changes get new files.
--
-- Two schemas (ADR-003, ADR-004, ADR-005):
-- - core: the central model, the same whatever a trial or document came from: who changed what
--   (actor, audit_log), where data comes from (source, ingest_run, source_listing,
--   evidence_transfer), trials (identifiers, records, sponsors) and documents (file → document →
--   version → page).
-- - checks: what our checks answered: runs, questions, evidence, judgements and their traces.
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
-- Who made the rows this migration adds
INSERT INTO core.actor (kind, name) VALUES ('process', 'migration');

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
    PRIMARY KEY (source_id, source_key),
    FOREIGN KEY (source_id, source_key) REFERENCES core.trial_source
);

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
    -- the listing's source_updated_on when the transfer ran: a later one means the evidence is stale
    source_updated_on date,
    documents         integer NOT NULL DEFAULT 0,
    pages             integer NOT NULL DEFAULT 0,
    -- what failed: "record", a document's key, or a file -> the error
    errors            jsonb NOT NULL DEFAULT '{}',
    CHECK ((status = 'complete') = (errors = '{}'::jsonb)),
    PRIMARY KEY (source_id, source_key),
    FOREIGN KEY (source_id, source_key) REFERENCES core.trial_source
);

-- Each trial's state at each source: listed or not, and its evidence 'not transferred', 'failed',
-- 'stale' (complete, but the source changed the trial since) or 'complete'
CREATE VIEW core.evidence_status AS
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
-- gets replaced_at
CREATE TABLE core.document_version (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  bigint NOT NULL REFERENCES core.document,
    -- the source's version label (CTIS systemVersion), or 1 for an upload
    version      text NOT NULL,
    file_sha256  bytea NOT NULL REFERENCES core.file,
    -- when the source says this version was submitted or published, if it says (CTIS: the
    -- submission date of the results it belongs to)
    published_at timestamptz,
    fetched_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    replaced_at  timestamptz,
    created_by   bigint NOT NULL REFERENCES core.actor,
    UNIQUE (document_id, version)
);
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
    input_tokens        bigint NOT NULL DEFAULT 0,
    output_tokens       bigint NOT NULL DEFAULT 0,
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
    run_id            bigint NOT NULL REFERENCES checks.run,
    trial_id          bigint NOT NULL REFERENCES core.trial,
    -- full | not full | undecided; NULL only with an error
    label             text CHECK (label IN ('full', 'not full', 'undecided')),
    -- the parts by name, as the requirement set names them, e.g. {"participants": true, ...}
    parts             jsonb NOT NULL DEFAULT '{}',
    -- per question and part: the value, the rule that set it and the evidence id it rests on
    trace             jsonb,
    -- the inputs read at that moment: the trial record and the document versions
    trial_record_id   bigint REFERENCES core.trial_record,
    document_versions bigint[] NOT NULL DEFAULT '{}',
    -- the model that actually answered (another one when a refusal fell back), per call
    served_by         text[],
    input_tokens      integer,
    output_tokens     integer,
    duration_ms       integer,
    error             text,
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


-- migrate:down
DROP SCHEMA checks CASCADE;
DROP SCHEMA core CASCADE;
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I RESET TimeZone', current_database());
END
$$;
