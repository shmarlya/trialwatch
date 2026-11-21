\restrict dbmate

-- Dumped from database version 18.6
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: auth; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA auth;


--
-- Name: checks; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA checks;


--
-- Name: core; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA core;


--
-- Name: publishable(); Type: FUNCTION; Schema: checks; Owner: -
--

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


--
-- Name: audit(); Type: FUNCTION; Schema: core; Owner: -
--

CREATE FUNCTION core.audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: check_identifier(); Type: FUNCTION; Schema: core; Owner: -
--

CREATE FUNCTION core.check_identifier() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: refuse_change(); Type: FUNCTION; Schema: core; Owner: -
--

CREATE FUNCTION core.refuse_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: app_user; Type: TABLE; Schema: auth; Owner: -
--

CREATE TABLE auth.app_user (
    id bigint NOT NULL,
    email text NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL,
    actor_id bigint NOT NULL,
    disabled_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT app_user_email_check CHECK (((email = lower(email)) AND (length(email) <= 254) AND (email ~ '^[^@/[:space:]]+@[^@/[:space:]]+\.[^@/[:space:]]+$'::text))),
    CONSTRAINT app_user_role_check CHECK ((role = ANY (ARRAY['admin'::text, 'moderator'::text])))
);


--
-- Name: TABLE app_user; Type: COMMENT; Schema: auth; Owner: -
--

COMMENT ON TABLE auth.app_user IS 'people who can sign in to the web interface; their role picks their actions (auth/actions.py)';


--
-- Name: app_user_id_seq; Type: SEQUENCE; Schema: auth; Owner: -
--

ALTER TABLE auth.app_user ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME auth.app_user_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: session; Type: TABLE; Schema: auth; Owner: -
--

CREATE TABLE auth.session (
    token_sha256 bytea NOT NULL,
    user_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT session_token_sha256_check CHECK ((length(token_sha256) = 32))
);


--
-- Name: evidence; Type: TABLE; Schema: checks; Owner: -
--

CREATE TABLE checks.evidence (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    trial_id bigint NOT NULL,
    key text NOT NULL,
    found boolean,
    kind text,
    quote text,
    document_version_id bigint,
    file_sha256 bytea,
    page_no integer,
    note text,
    verified boolean NOT NULL
);


--
-- Name: evidence_id_seq; Type: SEQUENCE; Schema: checks; Owner: -
--

ALTER TABLE checks.evidence ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME checks.evidence_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: judgement; Type: TABLE; Schema: checks; Owner: -
--

CREATE TABLE checks.judgement (
    run_id bigint NOT NULL,
    trial_id bigint NOT NULL,
    label text,
    parts jsonb DEFAULT '{}'::jsonb NOT NULL,
    trace jsonb,
    trial_record_id bigint,
    document_versions bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    served_by text[],
    input_tokens integer,
    output_tokens integer,
    duration_ms integer,
    error text,
    CONSTRAINT judgement_check CHECK (((label IS NULL) = (error IS NOT NULL))),
    CONSTRAINT judgement_label_check CHECK ((label = ANY (ARRAY['full'::text, 'not full'::text, 'undecided'::text])))
);


--
-- Name: run; Type: TABLE; Schema: checks; Owner: -
--

CREATE TABLE checks.run (
    id bigint NOT NULL,
    check_name text NOT NULL,
    actor_id bigint NOT NULL,
    model text NOT NULL,
    effort text NOT NULL,
    prompt text NOT NULL,
    prompt_sha256 bytea NOT NULL,
    prompt_text text NOT NULL,
    requirements text NOT NULL,
    requirements_sha256 bytea NOT NULL,
    requirements_text text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    error text,
    started_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    finished_at timestamp with time zone,
    trials integer DEFAULT 0 NOT NULL,
    errors integer DEFAULT 0 NOT NULL,
    input_tokens bigint DEFAULT 0 NOT NULL,
    output_tokens bigint DEFAULT 0 NOT NULL,
    CONSTRAINT run_check CHECK (((status = 'running'::text) = (finished_at IS NULL))),
    CONSTRAINT run_prompt_sha256_check CHECK ((length(prompt_sha256) = 32)),
    CONSTRAINT run_requirements_sha256_check CHECK ((length(requirements_sha256) = 32)),
    CONSTRAINT run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text, 'stopped'::text])))
);


--
-- Name: document; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.document (
    id bigint NOT NULL,
    source_id smallint NOT NULL,
    source_key text NOT NULL,
    kind text NOT NULL,
    title text,
    language text,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL
);


--
-- Name: document_version; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.document_version (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    version text NOT NULL,
    file_sha256 bytea NOT NULL,
    published_at timestamp with time zone,
    fetched_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    replaced_at timestamp with time zone,
    created_by bigint NOT NULL
);


--
-- Name: COLUMN document_version.version; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON COLUMN core.document_version.version IS 'the source''s version label (CTIS systemVersion), or 1 for an upload; a source may replace a file without changing it, so a label can repeat: file_sha256 tells the versions apart';


--
-- Name: document_current; Type: VIEW; Schema: core; Owner: -
--

CREATE VIEW core.document_current AS
 SELECT d.id,
    d.source_id,
    d.source_key,
    d.kind,
    d.title,
    d.language,
    d.created_at,
    d.created_by,
    v.id AS version_id,
    v.version,
    v.file_sha256,
    v.published_at,
    v.fetched_at
   FROM (core.document d
     JOIN core.document_version v ON (((v.document_id = d.id) AND (v.replaced_at IS NULL))));


--
-- Name: trial_document; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.trial_document (
    trial_id bigint NOT NULL,
    document_id bigint NOT NULL,
    basis text NOT NULL,
    source_id smallint NOT NULL,
    confidence real DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    removed_at timestamp with time zone,
    removed_by bigint,
    removal text,
    CONSTRAINT trial_document_basis_check CHECK ((basis = ANY (ARRAY['listed-by-source'::text, 'identifier-in-text'::text, 'manual'::text]))),
    CONSTRAINT trial_document_check CHECK ((((removed_at IS NULL) = (removed_by IS NULL)) AND ((removed_at IS NULL) = (removal IS NULL)))),
    CONSTRAINT trial_document_confidence_check CHECK (((confidence > (0)::double precision) AND (confidence <= (1)::double precision))),
    CONSTRAINT trial_document_removal_check CHECK ((removal = ANY (ARRAY['source'::text, 'person'::text])))
);


--
-- Name: trial_record; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.trial_record (
    id bigint NOT NULL,
    source_id smallint NOT NULL,
    source_key text NOT NULL,
    content_hash bytea NOT NULL,
    payload jsonb NOT NULL,
    registered jsonb DEFAULT '{}'::jsonb NOT NULL,
    fetched_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    CONSTRAINT trial_record_content_hash_check CHECK ((length(content_hash) = 32))
);
ALTER TABLE ONLY core.trial_record ALTER COLUMN payload SET COMPRESSION lz4;


--
-- Name: trial_source; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.trial_source (
    source_id smallint NOT NULL,
    source_key text NOT NULL,
    trial_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL
);


--
-- Name: trial_record_current; Type: VIEW; Schema: core; Owner: -
--

CREATE VIEW core.trial_record_current AS
 SELECT s.trial_id,
    r.id,
    r.source_id,
    r.source_key,
    r.content_hash,
    r.payload,
    r.registered,
    r.fetched_at,
    r.created_by
   FROM (core.trial_source s
     CROSS JOIN LATERAL ( SELECT r_1.id,
            r_1.source_id,
            r_1.source_key,
            r_1.content_hash,
            r_1.payload,
            r_1.registered,
            r_1.fetched_at,
            r_1.created_by
           FROM core.trial_record r_1
          WHERE ((r_1.source_id = s.source_id) AND (r_1.source_key = s.source_key))
          ORDER BY r_1.fetched_at DESC, r_1.id DESC
         LIMIT 1) r);


--
-- Name: judgement_current; Type: VIEW; Schema: checks; Owner: -
--

CREATE VIEW checks.judgement_current AS
 SELECT DISTINCT ON (j.trial_id, r.requirements) j.run_id,
    j.trial_id,
    j.label,
    j.parts,
    j.trace,
    j.trial_record_id,
    j.document_versions,
    j.served_by,
    j.input_tokens,
    j.output_tokens,
    j.duration_ms,
    r.model,
    r.prompt,
    r.requirements,
    r.started_at,
    ((EXISTS ( SELECT
           FROM (core.document_version v
             JOIN core.trial_document l ON (((l.document_id = v.document_id) AND (l.trial_id = j.trial_id))))
          WHERE ((v.id = ANY (j.document_versions)) AND ((v.replaced_at IS NOT NULL) OR (l.removed_at IS NOT NULL))))) OR (EXISTS ( SELECT
           FROM (core.trial_document l
             JOIN core.document_current d ON ((d.id = l.document_id)))
          WHERE ((l.trial_id = j.trial_id) AND (l.removed_at IS NULL) AND (NOT (d.version_id = ANY (j.document_versions))) AND (d.kind IN ( SELECT jsonb_array_elements_text(((r.requirements_text)::jsonb -> 'document_kinds'::text)) AS jsonb_array_elements_text))))) OR (EXISTS ( SELECT
           FROM core.trial_record_current c
          WHERE ((c.trial_id = j.trial_id) AND (c.id IS DISTINCT FROM j.trial_record_id) AND (c.fetched_at > r.started_at))))) AS outdated
   FROM (checks.judgement j
     JOIN checks.run r ON ((r.id = j.run_id)))
  WHERE ((j.error IS NULL) AND (r.status <> 'running'::text))
  ORDER BY j.trial_id, r.requirements, j.run_id DESC;


--
-- Name: publication; Type: TABLE; Schema: checks; Owner: -
--

CREATE TABLE checks.publication (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    trial_id bigint NOT NULL,
    published_by bigint NOT NULL,
    published_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    withdrawn_by bigint,
    withdrawn_at timestamp with time zone,
    note text,
    confidence real,
    basis text,
    CONSTRAINT publication_check CHECK (((confidence IS NULL) = (basis IS NULL))),
    CONSTRAINT publication_check1 CHECK (((withdrawn_at IS NULL) = (withdrawn_by IS NULL))),
    CONSTRAINT publication_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))
);


--
-- Name: TABLE publication; Type: COMMENT; Schema: checks; Owner: -
--

COMMENT ON TABLE checks.publication IS 'judgements staff made public (ADR-007); withdrawn ones are kept';


--
-- Name: judgement_published; Type: VIEW; Schema: checks; Owner: -
--

CREATE VIEW checks.judgement_published AS
 SELECT DISTINCT ON (j.trial_id, r.requirements) j.run_id,
    j.trial_id,
    j.label,
    j.parts,
    j.trace,
    j.trial_record_id,
    j.document_versions,
    r.model,
    r.prompt,
    r.requirements,
    r.started_at,
    p.published_at,
    p.confidence,
    p.basis,
    ((EXISTS ( SELECT
           FROM (core.document_version v
             JOIN core.trial_document l ON (((l.document_id = v.document_id) AND (l.trial_id = j.trial_id))))
          WHERE ((v.id = ANY (j.document_versions)) AND ((v.replaced_at IS NOT NULL) OR (l.removed_at IS NOT NULL))))) OR (EXISTS ( SELECT
           FROM (core.trial_document l
             JOIN core.document_current d ON ((d.id = l.document_id)))
          WHERE ((l.trial_id = j.trial_id) AND (l.removed_at IS NULL) AND (NOT (d.version_id = ANY (j.document_versions))) AND (d.kind IN ( SELECT jsonb_array_elements_text(((r.requirements_text)::jsonb -> 'document_kinds'::text)) AS jsonb_array_elements_text))))) OR (EXISTS ( SELECT
           FROM core.trial_record_current c
          WHERE ((c.trial_id = j.trial_id) AND (c.id IS DISTINCT FROM j.trial_record_id) AND (c.fetched_at > r.started_at))))) AS outdated
   FROM ((checks.publication p
     JOIN checks.judgement j ON (((j.run_id = p.run_id) AND (j.trial_id = p.trial_id))))
     JOIN checks.run r ON ((r.id = j.run_id)))
  WHERE (p.withdrawn_at IS NULL)
  ORDER BY j.trial_id, r.requirements, j.run_id DESC;


--
-- Name: publication_id_seq; Type: SEQUENCE; Schema: checks; Owner: -
--

ALTER TABLE checks.publication ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME checks.publication_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: question; Type: TABLE; Schema: checks; Owner: -
--

CREATE TABLE checks.question (
    run_id bigint NOT NULL,
    trial_id bigint NOT NULL,
    key text NOT NULL,
    item_text text,
    pages jsonb NOT NULL,
    widened boolean DEFAULT false NOT NULL
);


--
-- Name: run_id_seq; Type: SEQUENCE; Schema: checks; Owner: -
--

ALTER TABLE checks.run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME checks.run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: step; Type: TABLE; Schema: checks; Owner: -
--

CREATE TABLE checks.step (
    run_id bigint NOT NULL,
    trial_id bigint NOT NULL,
    seq integer NOT NULL,
    name text NOT NULL,
    at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    duration_ms integer,
    data jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: actor; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.actor (
    id bigint NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT actor_kind_check CHECK ((kind = ANY (ARRAY['person'::text, 'process'::text])))
);


--
-- Name: actor_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.actor ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.actor_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: audit_log; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.audit_log (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    actor_id bigint NOT NULL,
    action text NOT NULL,
    table_name text NOT NULL,
    row_key jsonb NOT NULL,
    old_row jsonb,
    new_row jsonb,
    note text,
    CONSTRAINT audit_log_action_check CHECK ((action = ANY (ARRAY['insert'::text, 'update'::text, 'delete'::text])))
);


--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.audit_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: document_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.document ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.document_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: document_kind; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.document_kind (
    code text NOT NULL,
    label text NOT NULL,
    has_results boolean DEFAULT false NOT NULL,
    CONSTRAINT document_kind_code_check CHECK ((code ~ '^[a-z][a-z0-9-]*$'::text))
);


--
-- Name: document_version_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.document_version ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.document_version_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: evidence_transfer; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.evidence_transfer (
    source_id smallint NOT NULL,
    source_key text NOT NULL,
    status text NOT NULL,
    transferred_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    transferred_by bigint NOT NULL,
    ingest_run_id bigint,
    document_kinds text[],
    source_updated_on date,
    documents integer DEFAULT 0 NOT NULL,
    pages integer DEFAULT 0 NOT NULL,
    errors jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT evidence_transfer_check CHECK (((status = 'complete'::text) = (errors = '{}'::jsonb))),
    CONSTRAINT evidence_transfer_status_check CHECK ((status = ANY (ARRAY['complete'::text, 'failed'::text])))
);


--
-- Name: source_listing; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.source_listing (
    source_id smallint NOT NULL,
    source_key text NOT NULL,
    title text,
    source_updated_on date,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    first_listed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    last_listed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    last_run bigint NOT NULL,
    delisted_at timestamp with time zone
);


--
-- Name: evidence_status; Type: VIEW; Schema: core; Owner: -
--

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
    e.errors
   FROM ((core.trial_source ts
     LEFT JOIN core.source_listing l USING (source_id, source_key))
     LEFT JOIN core.evidence_transfer e USING (source_id, source_key));


--
-- Name: file; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.file (
    sha256 bytea NOT NULL,
    size_bytes bigint NOT NULL,
    media_type text,
    content bytea NOT NULL,
    text_fingerprint bytea,
    pages_extracted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    CONSTRAINT file_check CHECK ((size_bytes = length(content))),
    CONSTRAINT file_sha256_check CHECK ((length(sha256) = 32)),
    CONSTRAINT file_text_fingerprint_check CHECK ((length(text_fingerprint) = 32))
);
ALTER TABLE ONLY core.file ALTER COLUMN content SET STORAGE EXTERNAL;


--
-- Name: identifier_scheme; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.identifier_scheme (
    code text NOT NULL,
    label text NOT NULL,
    pattern text NOT NULL,
    identifies text DEFAULT 'trial'::text NOT NULL,
    CONSTRAINT identifier_scheme_code_check CHECK ((code ~ '^[a-z][a-z0-9-]*$'::text)),
    CONSTRAINT identifier_scheme_identifies_check CHECK ((identifies = ANY (ARRAY['trial'::text, 'organisation'::text])))
);


--
-- Name: ingest_run; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.ingest_run (
    id bigint NOT NULL,
    source_id smallint NOT NULL,
    kind text NOT NULL,
    actor_id bigint NOT NULL,
    args jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    started_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    finished_at timestamp with time zone,
    counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    errors jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ingest_run_check CHECK (((status = 'running'::text) = (finished_at IS NULL))),
    CONSTRAINT ingest_run_kind_check CHECK ((kind = ANY (ARRAY['list'::text, 'transfer'::text]))),
    CONSTRAINT ingest_run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text])))
);


--
-- Name: ingest_run_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.ingest_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.ingest_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: organisation; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.organisation (
    id bigint NOT NULL,
    name text NOT NULL,
    name_key text GENERATED ALWAYS AS (lower(regexp_replace(btrim(name), '\s+'::text, ' '::text, 'g'::text))) STORED,
    kind text,
    country text,
    commercial boolean,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    first_source smallint NOT NULL,
    merged_into bigint,
    CONSTRAINT organisation_check CHECK ((merged_into IS DISTINCT FROM id)),
    CONSTRAINT organisation_name_check CHECK ((btrim(name) <> ''::text))
);


--
-- Name: organisation_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.organisation ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.organisation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: organisation_identifier; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.organisation_identifier (
    scheme text NOT NULL,
    value text NOT NULL,
    organisation_id bigint NOT NULL,
    source_id smallint NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL
);


--
-- Name: page; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.page (
    file_sha256 bytea NOT NULL,
    page_no integer NOT NULL,
    text text NOT NULL,
    extractor text NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, text)) STORED,
    CONSTRAINT page_page_no_check CHECK ((page_no >= 1))
);


--
-- Name: possible_duplicate; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.possible_duplicate (
    id bigint NOT NULL,
    trial_a bigint NOT NULL,
    trial_b bigint NOT NULL,
    evidence jsonb NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    decided_by bigint,
    decided_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    CONSTRAINT possible_duplicate_check CHECK ((trial_a < trial_b)),
    CONSTRAINT possible_duplicate_check1 CHECK (((status = 'open'::text) = (decided_at IS NULL))),
    CONSTRAINT possible_duplicate_status_check CHECK ((status = ANY (ARRAY['open'::text, 'merged'::text, 'distinct'::text])))
);


--
-- Name: possible_duplicate_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.possible_duplicate ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.possible_duplicate_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: source; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.source (
    id smallint NOT NULL,
    code text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    url text,
    adapter text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    CONSTRAINT source_code_check CHECK ((code ~ '^[a-z][a-z0-9-]*$'::text)),
    CONSTRAINT source_kind_check CHECK ((kind = ANY (ARRAY['registry'::text, 'publications'::text, 'manual'::text])))
);


--
-- Name: source_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.source ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.source_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: trial; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.trial (
    id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    first_source smallint NOT NULL,
    merged_into bigint,
    CONSTRAINT trial_check CHECK ((merged_into IS DISTINCT FROM id))
);


--
-- Name: trial_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.trial ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.trial_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: trial_identifier; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.trial_identifier (
    scheme text NOT NULL,
    value text NOT NULL,
    trial_id bigint NOT NULL,
    source_id smallint NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL
);


--
-- Name: trial_record_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.trial_record ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.trial_record_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: trial_sponsor; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.trial_sponsor (
    trial_id bigint NOT NULL,
    organisation_id bigint NOT NULL,
    is_primary boolean NOT NULL,
    source_id smallint NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    removed_at timestamp with time zone,
    removed_by bigint,
    removal text,
    CONSTRAINT trial_sponsor_check CHECK ((((removed_at IS NULL) = (removed_by IS NULL)) AND ((removed_at IS NULL) = (removal IS NULL)))),
    CONSTRAINT trial_sponsor_removal_check CHECK ((removal = ANY (ARRAY['source'::text, 'person'::text])))
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying NOT NULL
);


--
-- Name: app_user app_user_actor_id_key; Type: CONSTRAINT; Schema: auth; Owner: -
--

ALTER TABLE ONLY auth.app_user
    ADD CONSTRAINT app_user_actor_id_key UNIQUE (actor_id);


--
-- Name: app_user app_user_email_key; Type: CONSTRAINT; Schema: auth; Owner: -
--

ALTER TABLE ONLY auth.app_user
    ADD CONSTRAINT app_user_email_key UNIQUE (email);


--
-- Name: app_user app_user_pkey; Type: CONSTRAINT; Schema: auth; Owner: -
--

ALTER TABLE ONLY auth.app_user
    ADD CONSTRAINT app_user_pkey PRIMARY KEY (id);


--
-- Name: session session_pkey; Type: CONSTRAINT; Schema: auth; Owner: -
--

ALTER TABLE ONLY auth.session
    ADD CONSTRAINT session_pkey PRIMARY KEY (token_sha256);


--
-- Name: evidence evidence_pkey; Type: CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.evidence
    ADD CONSTRAINT evidence_pkey PRIMARY KEY (id);


--
-- Name: judgement judgement_pkey; Type: CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.judgement
    ADD CONSTRAINT judgement_pkey PRIMARY KEY (run_id, trial_id);


--
-- Name: publication publication_pkey; Type: CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.publication
    ADD CONSTRAINT publication_pkey PRIMARY KEY (id);


--
-- Name: question question_pkey; Type: CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.question
    ADD CONSTRAINT question_pkey PRIMARY KEY (run_id, trial_id, key);


--
-- Name: run run_pkey; Type: CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.run
    ADD CONSTRAINT run_pkey PRIMARY KEY (id);


--
-- Name: step step_pkey; Type: CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.step
    ADD CONSTRAINT step_pkey PRIMARY KEY (run_id, trial_id, seq);


--
-- Name: actor actor_kind_name_key; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.actor
    ADD CONSTRAINT actor_kind_name_key UNIQUE (kind, name);


--
-- Name: actor actor_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.actor
    ADD CONSTRAINT actor_pkey PRIMARY KEY (id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: document_kind document_kind_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document_kind
    ADD CONSTRAINT document_kind_pkey PRIMARY KEY (code);


--
-- Name: document document_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document
    ADD CONSTRAINT document_pkey PRIMARY KEY (id);


--
-- Name: document document_source_id_source_key_key; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document
    ADD CONSTRAINT document_source_id_source_key_key UNIQUE (source_id, source_key);


--
-- Name: document_version document_version_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document_version
    ADD CONSTRAINT document_version_pkey PRIMARY KEY (id);


--
-- Name: evidence_transfer evidence_transfer_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.evidence_transfer
    ADD CONSTRAINT evidence_transfer_pkey PRIMARY KEY (source_id, source_key);


--
-- Name: file file_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.file
    ADD CONSTRAINT file_pkey PRIMARY KEY (sha256);


--
-- Name: identifier_scheme identifier_scheme_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.identifier_scheme
    ADD CONSTRAINT identifier_scheme_pkey PRIMARY KEY (code);


--
-- Name: ingest_run ingest_run_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.ingest_run
    ADD CONSTRAINT ingest_run_pkey PRIMARY KEY (id);


--
-- Name: organisation_identifier organisation_identifier_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.organisation_identifier
    ADD CONSTRAINT organisation_identifier_pkey PRIMARY KEY (scheme, value);


--
-- Name: organisation organisation_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.organisation
    ADD CONSTRAINT organisation_pkey PRIMARY KEY (id);


--
-- Name: page page_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.page
    ADD CONSTRAINT page_pkey PRIMARY KEY (file_sha256, page_no);


--
-- Name: possible_duplicate possible_duplicate_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.possible_duplicate
    ADD CONSTRAINT possible_duplicate_pkey PRIMARY KEY (id);


--
-- Name: possible_duplicate possible_duplicate_trial_a_trial_b_key; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.possible_duplicate
    ADD CONSTRAINT possible_duplicate_trial_a_trial_b_key UNIQUE (trial_a, trial_b);


--
-- Name: source source_adapter_key; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.source
    ADD CONSTRAINT source_adapter_key UNIQUE (adapter);


--
-- Name: source source_code_key; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.source
    ADD CONSTRAINT source_code_key UNIQUE (code);


--
-- Name: source_listing source_listing_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.source_listing
    ADD CONSTRAINT source_listing_pkey PRIMARY KEY (source_id, source_key);


--
-- Name: source source_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.source
    ADD CONSTRAINT source_pkey PRIMARY KEY (id);


--
-- Name: trial_document trial_document_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_document
    ADD CONSTRAINT trial_document_pkey PRIMARY KEY (trial_id, document_id);


--
-- Name: trial_identifier trial_identifier_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_identifier
    ADD CONSTRAINT trial_identifier_pkey PRIMARY KEY (scheme, value);


--
-- Name: trial trial_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial
    ADD CONSTRAINT trial_pkey PRIMARY KEY (id);


--
-- Name: trial_record trial_record_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_record
    ADD CONSTRAINT trial_record_pkey PRIMARY KEY (id);


--
-- Name: trial_source trial_source_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_source
    ADD CONSTRAINT trial_source_pkey PRIMARY KEY (source_id, source_key);


--
-- Name: trial_sponsor trial_sponsor_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_sponsor
    ADD CONSTRAINT trial_sponsor_pkey PRIMARY KEY (trial_id, organisation_id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: session_user_idx; Type: INDEX; Schema: auth; Owner: -
--

CREATE INDEX session_user_idx ON auth.session USING btree (user_id);


--
-- Name: evidence_question_idx; Type: INDEX; Schema: checks; Owner: -
--

CREATE INDEX evidence_question_idx ON checks.evidence USING btree (run_id, trial_id, key);


--
-- Name: judgement_trial_idx; Type: INDEX; Schema: checks; Owner: -
--

CREATE INDEX judgement_trial_idx ON checks.judgement USING btree (trial_id, run_id DESC);


--
-- Name: publication_live_idx; Type: INDEX; Schema: checks; Owner: -
--

CREATE UNIQUE INDEX publication_live_idx ON checks.publication USING btree (run_id, trial_id) WHERE (withdrawn_at IS NULL);


--
-- Name: publication_trial_idx; Type: INDEX; Schema: checks; Owner: -
--

CREATE INDEX publication_trial_idx ON checks.publication USING btree (trial_id);


--
-- Name: audit_log_actor_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX audit_log_actor_idx ON core.audit_log USING btree (actor_id, at);


--
-- Name: audit_log_row_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX audit_log_row_idx ON core.audit_log USING btree (table_name, row_key);


--
-- Name: document_version_current_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE UNIQUE INDEX document_version_current_idx ON core.document_version USING btree (document_id) WHERE (replaced_at IS NULL);


--
-- Name: document_version_file_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX document_version_file_idx ON core.document_version USING btree (file_sha256);


--
-- Name: file_text_fingerprint_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX file_text_fingerprint_idx ON core.file USING btree (text_fingerprint) WHERE (text_fingerprint IS NOT NULL);


--
-- Name: ingest_run_source_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX ingest_run_source_idx ON core.ingest_run USING btree (source_id, started_at DESC);


--
-- Name: organisation_identifier_organisation_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX organisation_identifier_organisation_idx ON core.organisation_identifier USING btree (organisation_id);


--
-- Name: organisation_name_key_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX organisation_name_key_idx ON core.organisation USING btree (name_key);


--
-- Name: page_tsv_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX page_tsv_idx ON core.page USING gin (tsv);


--
-- Name: trial_document_document_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_document_document_idx ON core.trial_document USING btree (document_id);


--
-- Name: trial_identifier_trial_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_identifier_trial_idx ON core.trial_identifier USING btree (trial_id);


--
-- Name: trial_record_latest_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_record_latest_idx ON core.trial_record USING btree (source_id, source_key, fetched_at DESC, id DESC);


--
-- Name: trial_source_trial_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_source_trial_idx ON core.trial_source USING btree (trial_id);


--
-- Name: trial_sponsor_organisation_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_sponsor_organisation_idx ON core.trial_sponsor USING btree (organisation_id);


--
-- Name: app_user app_user_audit; Type: TRIGGER; Schema: auth; Owner: -
--

CREATE TRIGGER app_user_audit AFTER INSERT OR DELETE OR UPDATE ON auth.app_user FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'password_hash');


--
-- Name: publication publication_audit; Type: TRIGGER; Schema: checks; Owner: -
--

CREATE TRIGGER publication_audit AFTER INSERT OR DELETE OR UPDATE ON checks.publication FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: publication publication_publishable; Type: TRIGGER; Schema: checks; Owner: -
--

CREATE TRIGGER publication_publishable BEFORE INSERT ON checks.publication FOR EACH ROW EXECUTE FUNCTION checks.publishable();


--
-- Name: actor actor_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER actor_audit AFTER DELETE OR UPDATE ON core.actor FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: audit_log audit_log_append_only; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER audit_log_append_only BEFORE DELETE OR UPDATE ON core.audit_log FOR EACH ROW EXECUTE FUNCTION core.refuse_change();


--
-- Name: audit_log audit_log_never_truncated; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER audit_log_never_truncated BEFORE TRUNCATE ON core.audit_log FOR EACH STATEMENT EXECUTE FUNCTION core.refuse_change();


--
-- Name: document document_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER document_audit AFTER INSERT OR DELETE OR UPDATE ON core.document FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: document_version document_version_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER document_version_audit AFTER INSERT OR DELETE OR UPDATE ON core.document_version FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: evidence_transfer evidence_transfer_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER evidence_transfer_audit AFTER INSERT OR DELETE OR UPDATE ON core.evidence_transfer FOR EACH ROW EXECUTE FUNCTION core.audit('source_id,source_key');


--
-- Name: file file_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER file_audit AFTER INSERT OR DELETE OR UPDATE ON core.file FOR EACH ROW EXECUTE FUNCTION core.audit('sha256', 'content');


--
-- Name: file file_never_changes; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER file_never_changes BEFORE DELETE OR UPDATE OF content, sha256, size_bytes ON core.file FOR EACH ROW EXECUTE FUNCTION core.refuse_change();


--
-- Name: organisation organisation_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER organisation_audit AFTER INSERT OR DELETE OR UPDATE ON core.organisation FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'name_key');


--
-- Name: organisation_identifier organisation_identifier_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER organisation_identifier_audit AFTER INSERT OR DELETE OR UPDATE ON core.organisation_identifier FOR EACH ROW EXECUTE FUNCTION core.audit('scheme,value');


--
-- Name: organisation_identifier organisation_identifier_valid; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER organisation_identifier_valid BEFORE INSERT OR UPDATE ON core.organisation_identifier FOR EACH ROW EXECUTE FUNCTION core.check_identifier();


--
-- Name: page page_never_changes; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER page_never_changes BEFORE DELETE OR UPDATE ON core.page FOR EACH ROW EXECUTE FUNCTION core.refuse_change();


--
-- Name: possible_duplicate possible_duplicate_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER possible_duplicate_audit AFTER INSERT OR DELETE OR UPDATE ON core.possible_duplicate FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: source source_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER source_audit AFTER INSERT OR DELETE OR UPDATE ON core.source FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: trial trial_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER trial_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: trial_document trial_document_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER trial_document_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_document FOR EACH ROW EXECUTE FUNCTION core.audit('trial_id,document_id');


--
-- Name: trial_identifier trial_identifier_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER trial_identifier_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_identifier FOR EACH ROW EXECUTE FUNCTION core.audit('scheme,value');


--
-- Name: trial_identifier trial_identifier_valid; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER trial_identifier_valid BEFORE INSERT OR UPDATE ON core.trial_identifier FOR EACH ROW EXECUTE FUNCTION core.check_identifier();


--
-- Name: trial_record trial_record_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER trial_record_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_record FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'payload,registered');


--
-- Name: trial_source trial_source_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER trial_source_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_source FOR EACH ROW EXECUTE FUNCTION core.audit('source_id,source_key');


--
-- Name: trial_sponsor trial_sponsor_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER trial_sponsor_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_sponsor FOR EACH ROW EXECUTE FUNCTION core.audit('trial_id,organisation_id');


--
-- Name: app_user app_user_actor_id_fkey; Type: FK CONSTRAINT; Schema: auth; Owner: -
--

ALTER TABLE ONLY auth.app_user
    ADD CONSTRAINT app_user_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES core.actor(id);


--
-- Name: session session_user_id_fkey; Type: FK CONSTRAINT; Schema: auth; Owner: -
--

ALTER TABLE ONLY auth.session
    ADD CONSTRAINT session_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.app_user(id) ON DELETE CASCADE;


--
-- Name: evidence evidence_document_version_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.evidence
    ADD CONSTRAINT evidence_document_version_id_fkey FOREIGN KEY (document_version_id) REFERENCES core.document_version(id);


--
-- Name: evidence evidence_file_sha256_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.evidence
    ADD CONSTRAINT evidence_file_sha256_fkey FOREIGN KEY (file_sha256) REFERENCES core.file(sha256);


--
-- Name: evidence evidence_run_id_trial_id_key_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.evidence
    ADD CONSTRAINT evidence_run_id_trial_id_key_fkey FOREIGN KEY (run_id, trial_id, key) REFERENCES checks.question(run_id, trial_id, key);


--
-- Name: judgement judgement_run_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.judgement
    ADD CONSTRAINT judgement_run_id_fkey FOREIGN KEY (run_id) REFERENCES checks.run(id);


--
-- Name: judgement judgement_trial_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.judgement
    ADD CONSTRAINT judgement_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: judgement judgement_trial_record_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.judgement
    ADD CONSTRAINT judgement_trial_record_id_fkey FOREIGN KEY (trial_record_id) REFERENCES core.trial_record(id);


--
-- Name: publication publication_published_by_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.publication
    ADD CONSTRAINT publication_published_by_fkey FOREIGN KEY (published_by) REFERENCES core.actor(id);


--
-- Name: publication publication_run_id_trial_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.publication
    ADD CONSTRAINT publication_run_id_trial_id_fkey FOREIGN KEY (run_id, trial_id) REFERENCES checks.judgement(run_id, trial_id);


--
-- Name: publication publication_withdrawn_by_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.publication
    ADD CONSTRAINT publication_withdrawn_by_fkey FOREIGN KEY (withdrawn_by) REFERENCES core.actor(id);


--
-- Name: question question_run_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.question
    ADD CONSTRAINT question_run_id_fkey FOREIGN KEY (run_id) REFERENCES checks.run(id);


--
-- Name: question question_trial_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.question
    ADD CONSTRAINT question_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: run run_actor_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.run
    ADD CONSTRAINT run_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES core.actor(id);


--
-- Name: step step_run_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.step
    ADD CONSTRAINT step_run_id_fkey FOREIGN KEY (run_id) REFERENCES checks.run(id);


--
-- Name: step step_trial_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.step
    ADD CONSTRAINT step_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: audit_log audit_log_actor_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.audit_log
    ADD CONSTRAINT audit_log_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES core.actor(id);


--
-- Name: document document_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document
    ADD CONSTRAINT document_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: document document_kind_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document
    ADD CONSTRAINT document_kind_fkey FOREIGN KEY (kind) REFERENCES core.document_kind(code);


--
-- Name: document document_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document
    ADD CONSTRAINT document_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: document_version document_version_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document_version
    ADD CONSTRAINT document_version_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: document_version document_version_document_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document_version
    ADD CONSTRAINT document_version_document_id_fkey FOREIGN KEY (document_id) REFERENCES core.document(id);


--
-- Name: document_version document_version_file_sha256_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.document_version
    ADD CONSTRAINT document_version_file_sha256_fkey FOREIGN KEY (file_sha256) REFERENCES core.file(sha256);


--
-- Name: evidence_transfer evidence_transfer_ingest_run_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.evidence_transfer
    ADD CONSTRAINT evidence_transfer_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES core.ingest_run(id);


--
-- Name: evidence_transfer evidence_transfer_source_id_source_key_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.evidence_transfer
    ADD CONSTRAINT evidence_transfer_source_id_source_key_fkey FOREIGN KEY (source_id, source_key) REFERENCES core.trial_source(source_id, source_key);


--
-- Name: evidence_transfer evidence_transfer_transferred_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.evidence_transfer
    ADD CONSTRAINT evidence_transfer_transferred_by_fkey FOREIGN KEY (transferred_by) REFERENCES core.actor(id);


--
-- Name: file file_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.file
    ADD CONSTRAINT file_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: ingest_run ingest_run_actor_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.ingest_run
    ADD CONSTRAINT ingest_run_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES core.actor(id);


--
-- Name: ingest_run ingest_run_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.ingest_run
    ADD CONSTRAINT ingest_run_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: organisation organisation_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.organisation
    ADD CONSTRAINT organisation_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: organisation organisation_first_source_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.organisation
    ADD CONSTRAINT organisation_first_source_fkey FOREIGN KEY (first_source) REFERENCES core.source(id);


--
-- Name: organisation_identifier organisation_identifier_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.organisation_identifier
    ADD CONSTRAINT organisation_identifier_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: organisation_identifier organisation_identifier_organisation_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.organisation_identifier
    ADD CONSTRAINT organisation_identifier_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES core.organisation(id);


--
-- Name: organisation_identifier organisation_identifier_scheme_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.organisation_identifier
    ADD CONSTRAINT organisation_identifier_scheme_fkey FOREIGN KEY (scheme) REFERENCES core.identifier_scheme(code);


--
-- Name: organisation_identifier organisation_identifier_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.organisation_identifier
    ADD CONSTRAINT organisation_identifier_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: organisation organisation_merged_into_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.organisation
    ADD CONSTRAINT organisation_merged_into_fkey FOREIGN KEY (merged_into) REFERENCES core.organisation(id);


--
-- Name: page page_file_sha256_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.page
    ADD CONSTRAINT page_file_sha256_fkey FOREIGN KEY (file_sha256) REFERENCES core.file(sha256);


--
-- Name: possible_duplicate possible_duplicate_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.possible_duplicate
    ADD CONSTRAINT possible_duplicate_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: possible_duplicate possible_duplicate_decided_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.possible_duplicate
    ADD CONSTRAINT possible_duplicate_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES core.actor(id);


--
-- Name: possible_duplicate possible_duplicate_trial_a_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.possible_duplicate
    ADD CONSTRAINT possible_duplicate_trial_a_fkey FOREIGN KEY (trial_a) REFERENCES core.trial(id);


--
-- Name: possible_duplicate possible_duplicate_trial_b_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.possible_duplicate
    ADD CONSTRAINT possible_duplicate_trial_b_fkey FOREIGN KEY (trial_b) REFERENCES core.trial(id);


--
-- Name: source source_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.source
    ADD CONSTRAINT source_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: source_listing source_listing_last_run_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.source_listing
    ADD CONSTRAINT source_listing_last_run_fkey FOREIGN KEY (last_run) REFERENCES core.ingest_run(id);


--
-- Name: source_listing source_listing_source_id_source_key_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.source_listing
    ADD CONSTRAINT source_listing_source_id_source_key_fkey FOREIGN KEY (source_id, source_key) REFERENCES core.trial_source(source_id, source_key);


--
-- Name: trial trial_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial
    ADD CONSTRAINT trial_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: trial_document trial_document_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_document
    ADD CONSTRAINT trial_document_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: trial_document trial_document_document_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_document
    ADD CONSTRAINT trial_document_document_id_fkey FOREIGN KEY (document_id) REFERENCES core.document(id);


--
-- Name: trial_document trial_document_removed_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_document
    ADD CONSTRAINT trial_document_removed_by_fkey FOREIGN KEY (removed_by) REFERENCES core.actor(id);


--
-- Name: trial_document trial_document_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_document
    ADD CONSTRAINT trial_document_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: trial_document trial_document_trial_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_document
    ADD CONSTRAINT trial_document_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: trial trial_first_source_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial
    ADD CONSTRAINT trial_first_source_fkey FOREIGN KEY (first_source) REFERENCES core.source(id);


--
-- Name: trial_identifier trial_identifier_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_identifier
    ADD CONSTRAINT trial_identifier_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: trial_identifier trial_identifier_scheme_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_identifier
    ADD CONSTRAINT trial_identifier_scheme_fkey FOREIGN KEY (scheme) REFERENCES core.identifier_scheme(code);


--
-- Name: trial_identifier trial_identifier_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_identifier
    ADD CONSTRAINT trial_identifier_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: trial_identifier trial_identifier_trial_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_identifier
    ADD CONSTRAINT trial_identifier_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: trial trial_merged_into_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial
    ADD CONSTRAINT trial_merged_into_fkey FOREIGN KEY (merged_into) REFERENCES core.trial(id);


--
-- Name: trial_record trial_record_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_record
    ADD CONSTRAINT trial_record_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: trial_record trial_record_source_id_source_key_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_record
    ADD CONSTRAINT trial_record_source_id_source_key_fkey FOREIGN KEY (source_id, source_key) REFERENCES core.trial_source(source_id, source_key);


--
-- Name: trial_source trial_source_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_source
    ADD CONSTRAINT trial_source_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: trial_source trial_source_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_source
    ADD CONSTRAINT trial_source_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: trial_source trial_source_trial_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_source
    ADD CONSTRAINT trial_source_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: trial_sponsor trial_sponsor_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_sponsor
    ADD CONSTRAINT trial_sponsor_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: trial_sponsor trial_sponsor_organisation_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_sponsor
    ADD CONSTRAINT trial_sponsor_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES core.organisation(id);


--
-- Name: trial_sponsor trial_sponsor_removed_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_sponsor
    ADD CONSTRAINT trial_sponsor_removed_by_fkey FOREIGN KEY (removed_by) REFERENCES core.actor(id);


--
-- Name: trial_sponsor trial_sponsor_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_sponsor
    ADD CONSTRAINT trial_sponsor_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: trial_sponsor trial_sponsor_trial_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_sponsor
    ADD CONSTRAINT trial_sponsor_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- PostgreSQL database dump complete
--

\unrestrict dbmate


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20261121120000'),
    ('20261121180000'),
    ('20261121200000'),
    ('20261121220000');
