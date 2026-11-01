-- migrate:up

-- The whole schema in one file: the migrations written up to 2026-11-01 (init, reference data,
-- ctis trials, UTC time zone, search export, gold labels), squashed. New changes go in new files.

-- Every session on this database shows and parses timestamps in UTC, whatever the server's
-- or client's own setting. Columns are timestamptz (stored as UTC instants either way); this
-- only fixes how they are displayed and how zone-less input like '2026-10-31 12:00' is read.
-- A client can still override it per session (e.g. a GUI sending its own TimeZone).
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET TimeZone TO %L', current_database(), 'UTC');
END
$$;

-- Everything fetched from CTIS lives in its own schema.
CREATE SCHEMA ctis;

-- One row per ingestion run (single fetch, seed list, or full sync), so every snapshot
-- can be traced back to the run that produced it.
CREATE TABLE ctis.fetch_run (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind           text NOT NULL CHECK (kind IN ('fetch', 'seed', 'sync')),
    args           jsonb NOT NULL DEFAULT '{}',
    status         text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    trials_fetched integer NOT NULL DEFAULT 0,
    trials_changed integer NOT NULL DEFAULT 0,
    trials_failed  integer NOT NULL DEFAULT 0,
    error          text,
    CHECK ((status = 'running') = (finished_at IS NULL))
);

-- One row per trial we have ever seen.
CREATE TABLE ctis.trial (
    -- EU CT number, e.g. 2025-522750-39-00
    ct_number     text PRIMARY KEY CHECK (ct_number ~ '^\d{4}-\d{6}-\d{2}-\d{2}$'),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now()
);

-- Untouched /retrieve responses. A row is added only when the content hash differs from the
-- trial's latest snapshot, so this is the full change history. Never updated or deleted.
CREATE TABLE ctis.trial_snapshot (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ct_number    text NOT NULL REFERENCES ctis.trial,
    fetch_run_id bigint NOT NULL REFERENCES ctis.fetch_run,
    -- clock_timestamp(), not now(): now() is the transaction start, identical for every row in it
    fetched_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- sha256 of the canonical JSON (computed by the app, which leaves out fields that change on
    -- every fetch without the trial changing)
    content_hash bytea NOT NULL CHECK (length(content_hash) = 32),
    payload      jsonb COMPRESSION lz4 NOT NULL,
    UNIQUE (ct_number, fetched_at)
);
CREATE INDEX trial_snapshot_latest_idx ON ctis.trial_snapshot (ct_number, fetched_at DESC);
CREATE INDEX trial_snapshot_fetch_run_idx ON ctis.trial_snapshot (fetch_run_id);

-- Latest snapshot per trial: what most queries should read.
CREATE VIEW ctis.trial_latest AS
SELECT DISTINCT ON (ct_number) *
FROM ctis.trial_snapshot
ORDER BY ct_number, fetched_at DESC;

-- Rows from a CTIS portal CSV export (the public search's download), e.g. the 2025-11-10 export
-- Bruckner et al. reviewed. Its columns differ from /retrieve, so it is kept apart from
-- ctis.trial_snapshot. Each row is stored as it was in the file (header -> text), never updated.
CREATE TABLE ctis.search_export_snapshot (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ct_number    text NOT NULL REFERENCES ctis.trial,
    fetch_run_id bigint NOT NULL REFERENCES ctis.fetch_run,
    -- the day CTIS produced the export, not the day we loaded it
    captured_on  date NOT NULL,
    source_file  text NOT NULL,
    -- sha256 of the canonical JSON of the row; loading the same file twice adds nothing
    content_hash bytea NOT NULL CHECK (length(content_hash) = 32),
    payload      jsonb COMPRESSION lz4 NOT NULL,
    UNIQUE (ct_number, captured_on, content_hash)
);
CREATE INDEX search_export_snapshot_fetch_run_idx ON ctis.search_export_snapshot (fetch_run_id);

-- Lookup lists for the numeric codes CTIS returns.
-- Source: the CTIS public portal's own option lists (euclinicaltrials.eu/ctis-public, JS bundle),
-- cross-checked against live API responses on 2026-10-30. Unofficial: CTIS may change them.
-- Document types are not in the portal bundle; the codes below are the ones seen in API data so far.
CREATE SCHEMA ref;

CREATE TABLE ref.trial_status (
    code  smallint PRIMARY KEY,
    label text NOT NULL UNIQUE
);
INSERT INTO ref.trial_status (code, label) VALUES
    (1,  'Under evaluation'),
    (2,  'Authorised, recruitment pending'),
    (3,  'Authorised, recruiting'),
    (4,  'Ongoing, recruiting'),
    (5,  'Ongoing, recruitment ended'),
    (6,  'Temporarily halted'),
    (7,  'Suspended'),
    (8,  'Ended'),
    (9,  'Expired'),
    (10, 'Revoked'),
    (11, 'Not authorised'),
    (12, 'Cancelled');

CREATE TABLE ref.trial_phase (
    code  smallint PRIMARY KEY,
    label text NOT NULL UNIQUE
);
-- Labels kept exactly as CTIS spells them (including the double space in code 5)
INSERT INTO ref.trial_phase (code, label) VALUES
    (1,  'Human Pharmacology (Phase I)- First administration to humans'),
    (2,  'Human Pharmacology (Phase I)- Bioequivalence Study'),
    (3,  'Human Pharmacology (Phase I)-  Other'),
    (4,  'Therapeutic exploratory (Phase II)'),
    (5,  'Therapeutic confirmatory  (Phase III)'),
    (6,  'Therapeutic use (Phase IV)'),
    (7,  'Phase I and Phase II (Integrated)- First administration to humans'),
    (8,  'Phase I and Phase II (Integrated)- Bioequivalence Study'),
    (9,  'Phase I and Phase II (Integrated)- Other'),
    (10, 'Phase II and Phase III (Integrated)'),
    (11, 'Phase III and phase IV (Integrated)');

CREATE TABLE ref.trial_region (
    code  smallint PRIMARY KEY,
    label text NOT NULL UNIQUE
);
INSERT INTO ref.trial_region (code, label) VALUES
    (1, 'EEA only'),
    (2, 'Non-EEA only'),
    (3, 'In both EEA and non-EEA');

CREATE TABLE ref.sponsor_type (
    code  smallint PRIMARY KEY,
    label text NOT NULL UNIQUE
);
INSERT INTO ref.sponsor_type (code, label) VALUES
    (1,  'Educational Institution'),
    (2,  'Health care'),
    (3,  'Industry'),
    (6,  'Patient organisation/association'),
    (8,  'Hospital/Clinic/Other health care facility'),
    (9,  'Laboratory/Research/Testing facility'),
    (10, 'Pharmaceutical company'),
    (11, 'Non-Pharmaceutical company'),
    (12, 'Pharmaceutical association/federation');

CREATE TABLE ref.age_group (
    code  smallint PRIMARY KEY,
    label text NOT NULL UNIQUE
);
INSERT INTO ref.age_group (code, label) VALUES
    (1, 'In utero'),
    (2, '0-17 years'),
    (3, '18-64 years'),
    (4, '65+ years');

CREATE TABLE ref.early_termination_reason (
    code  smallint PRIMARY KEY,
    label text NOT NULL UNIQUE
);
INSERT INTO ref.early_termination_reason (code, label) VALUES
    (1, 'Sponsor decision'),
    (2, 'Reprioritisation of trial'),
    (3, 'Feasibility (recruitment issues etc.)'),
    (4, 'Safety related (clinical or pre-clinical results)'),
    (5, 'Investigator/Site related'),
    (6, 'Medicinal Product related'),
    (7, 'Study management related');

CREATE TABLE ref.document_type (
    code  smallint PRIMARY KEY,
    label text NOT NULL UNIQUE,
    -- true for the result documents our checks read
    is_result boolean NOT NULL DEFAULT false
);
INSERT INTO ref.document_type (code, label, is_result) VALUES
    (7,   'Synopsis of the protocol (for publication)', false),
    (14,  'Recruitment arrangements (for publication)', false),
    (15,  'Subject information and informed consent form (for publication)', false),
    (19,  'Summary of Product Characteristics (SmPC) (for publication)', false),
    (46,  'Laypersons summary of results (for publication)', true),
    (103, 'Summary of results (for publication)', true),
    (104, 'Protocol (for publication)', false);

-- EEA countries; CTIS identifies them by name (e.g. mscName "Czechia"), so name must match CTIS spelling
CREATE TABLE ref.member_state (
    iso2  char(2) PRIMARY KEY,
    name  text NOT NULL UNIQUE,
    is_eu boolean NOT NULL
);
INSERT INTO ref.member_state (iso2, name, is_eu) VALUES
    ('AT', 'Austria', true),
    ('BE', 'Belgium', true),
    ('BG', 'Bulgaria', true),
    ('HR', 'Croatia', true),
    ('CY', 'Cyprus', true),
    ('CZ', 'Czechia', true),
    ('DK', 'Denmark', true),
    ('EE', 'Estonia', true),
    ('FI', 'Finland', true),
    ('FR', 'France', true),
    ('DE', 'Germany', true),
    ('GR', 'Greece', true),
    ('HU', 'Hungary', true),
    ('IE', 'Ireland', true),
    ('IT', 'Italy', true),
    ('LV', 'Latvia', true),
    ('LT', 'Lithuania', true),
    ('LU', 'Luxembourg', true),
    ('MT', 'Malta', true),
    ('NL', 'Netherlands', true),
    ('PL', 'Poland', true),
    ('PT', 'Portugal', true),
    ('RO', 'Romania', true),
    ('SK', 'Slovakia', true),
    ('SI', 'Slovenia', true),
    ('ES', 'Spain', true),
    ('SE', 'Sweden', true),
    ('IS', 'Iceland', false),
    ('LI', 'Liechtenstein', false),
    ('NO', 'Norway', false);

-- Hand-checked answers we compare our own checks against ("gold labels").
CREATE SCHEMA gold;

-- Bruckner et al.'s final verdict per trial: one row per study trial (277), loaded from their
-- <date>_ctis_study_final_dataset.csv (on OSF, no licence) by `trialwatch gold bruckner` or the dev seed
-- in db/dev/. The answer key for ADR-001: our due-date and
-- results-document checks must reach the same verdicts. Never updated; a later release of the
-- file with different content is reported by the loader, not written over.
CREATE TABLE gold.bruckner_verdict (
    ct_number        text PRIMARY KEY REFERENCES ctis.trial,
    fetch_run_id     bigint NOT NULL REFERENCES ctis.fetch_run,
    source_file      text NOT NULL,
    -- their legal_compliance_assessment, spelled as in the file; "reported fully" means a full
    -- scientific results document AND a layperson summary
    verdict          text NOT NULL CHECK (verdict IN (
        'reported fully on time', 'reported fully late', 'not fully reported', 'extended', 'cancelled'
    )),
    -- what the scientific results document in CTIS contained when they checked it
    results_document text NOT NULL CHECK (results_document IN ('full', 'incomplete', 'none')),
    lay_summary      boolean NOT NULL,
    -- results publication date minus completion date; only for trials reported fully
    days_to_results  integer CHECK ((days_to_results IS NULL) = (verdict NOT LIKE 'reported fully%')),
    comment          text,
    -- sha256 of the canonical JSON of the row, to spot a changed release of the file
    content_hash     bytea NOT NULL CHECK (length(content_hash) = 32),
    -- the row exactly as in the file ('NA' included)
    payload          jsonb NOT NULL
);
CREATE INDEX bruckner_verdict_fetch_run_idx ON gold.bruckner_verdict (fetch_run_id);


-- migrate:down
DROP SCHEMA gold CASCADE;
DROP SCHEMA ref CASCADE;
DROP SCHEMA ctis CASCADE;
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I RESET TimeZone', current_database());
END
$$;
