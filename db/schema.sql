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
-- Name: geo; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA geo;


--
-- Name: jobs; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA jobs;


--
-- Name: report; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA report;


--
-- Name: SCHEMA report; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA report IS 'what Grafana reads (role grafana_ro): queue, spend, checks, sources, backups';


--
-- Name: search; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA search;


--
-- Name: vocab; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA vocab;


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: unaccent; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;


--
-- Name: EXTENSION unaccent; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION unaccent IS 'text search dictionary that removes accents';


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


--
-- Name: distance_km(double precision, double precision, double precision, double precision); Type: FUNCTION; Schema: geo; Owner: -
--

CREATE FUNCTION geo.distance_km(lat1 double precision, lon1 double precision, lat2 double precision, lon2 double precision) RETURNS double precision
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN (((2 * 6371))::double precision * asin(LEAST((1)::double precision, sqrt((power(sin((radians((lat2 - lat1)) / (2)::double precision)), (2)::double precision) + ((cos(radians(lat1)) * cos(radians(lat2))) * power(sin((radians((lon2 - lon1)) / (2)::double precision)), (2)::double precision)))))));


--
-- Name: name_key(text); Type: FUNCTION; Schema: geo; Owner: -
--

CREATE FUNCTION geo.name_key(name text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN btrim(regexp_replace(lower(public.unaccent('public.unaccent'::regdictionary, COALESCE(name, ''::text))), '[^[:alnum:]]+'::text, ' '::text, 'g'::text));


--
-- Name: postcode_key(text); Type: FUNCTION; Schema: geo; Owner: -
--

CREATE FUNCTION geo.postcode_key(postcode text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN regexp_replace(upper(COALESCE(postcode, ''::text)), '[^[:alnum:]]'::text, ''::text, 'g'::text);


--
-- Name: words_key(text); Type: FUNCTION; Schema: vocab; Owner: -
--

CREATE FUNCTION vocab.words_key(name text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN array_to_string(ARRAY(SELECT DISTINCT word.word FROM unnest(string_to_array(geo.name_key(words_key.name), ' '::text)) word(word) WHERE (word.word <> ''::text) ORDER BY word.word), ' '::text);


--
-- Name: trialwatch; Type: TEXT SEARCH CONFIGURATION; Schema: search; Owner: -
--

CREATE TEXT SEARCH CONFIGURATION search.trialwatch (
    PARSER = pg_catalog."default" );

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR asciiword WITH public.unaccent, simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR word WITH public.unaccent, simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR numword WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR email WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR url WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR host WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR sfloat WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR version WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR hword_numpart WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR hword_part WITH public.unaccent, simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR hword_asciipart WITH public.unaccent, simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR numhword WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR asciihword WITH public.unaccent, simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR hword WITH public.unaccent, simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR url_path WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR file WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR "float" WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR "int" WITH simple;

ALTER TEXT SEARCH CONFIGURATION search.trialwatch
    ADD MAPPING FOR uint WITH simple;


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
-- Name: answer; Type: TABLE; Schema: checks; Owner: -
--

CREATE TABLE checks.answer (
    run_id bigint NOT NULL,
    trial_id bigint NOT NULL,
    key text NOT NULL,
    item_text text,
    pages jsonb NOT NULL,
    widened boolean DEFAULT false NOT NULL,
    evidence_id bigint,
    found boolean,
    kind text,
    quote text,
    document_version_id bigint,
    file_sha256 bytea,
    page_no integer,
    note text,
    verified boolean,
    CONSTRAINT answer_check CHECK (((evidence_id IS NULL) = (verified IS NULL)))
);


--
-- Name: TABLE answer; Type: COMMENT; Schema: checks; Owner: -
--

COMMENT ON TABLE checks.answer IS 'each question a run asked about a trial, with the answer the model gave (if any)';


--
-- Name: answer_evidence_id_seq; Type: SEQUENCE; Schema: checks; Owner: -
--

CREATE SEQUENCE checks.answer_evidence_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: answer_evidence_id_seq; Type: SEQUENCE OWNED BY; Schema: checks; Owner: -
--

ALTER SEQUENCE checks.answer_evidence_id_seq OWNED BY checks.answer.evidence_id;


--
-- Name: judgement; Type: TABLE; Schema: checks; Owner: -
--

CREATE TABLE checks.judgement (
    run_id bigint NOT NULL,
    trial_id bigint NOT NULL,
    label text,
    parts jsonb DEFAULT '{}'::jsonb NOT NULL,
    trial_record_id bigint,
    document_versions bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    served_by text[],
    duration_ms integer,
    error text,
    agreement jsonb,
    withdrawn_at timestamp with time zone,
    withdrawn_by bigint,
    withdrawn_reason text,
    CONSTRAINT judgement_check CHECK (((label IS NULL) = (error IS NOT NULL))),
    CONSTRAINT judgement_withdrawn_check CHECK ((((withdrawn_at IS NULL) = (withdrawn_by IS NULL)) AND ((withdrawn_at IS NULL) = (withdrawn_reason IS NULL))))
);


--
-- Name: COLUMN judgement.label; Type: COMMENT; Schema: checks; Owner: -
--

COMMENT ON COLUMN checks.judgement.label IS 'one of its requirement set''s labels; NULL only with an error';


--
-- Name: COLUMN judgement.agreement; Type: COMMENT; Schema: checks; Owner: -
--

COMMENT ON COLUMN checks.judgement.agreement IS '{"confidence": share or null, "basis": in words}: how judgements of its kind agreed with gold labels when its run finished';


--
-- Name: COLUMN judgement.withdrawn_reason; Type: COMMENT; Schema: checks; Owner: -
--

COMMENT ON COLUMN checks.judgement.withdrawn_reason IS 'why staff withdrew it: shown to the public, with the judgement';


--
-- Name: run; Type: TABLE; Schema: checks; Owner: -
--

CREATE TABLE checks.run (
    id bigint NOT NULL,
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
    job_id bigint DEFAULT (NULLIF(current_setting('trialwatch.job'::text, true), ''::text))::bigint,
    CONSTRAINT run_check CHECK (((status = ANY (ARRAY['running'::text, 'waiting'::text])) = (finished_at IS NULL))),
    CONSTRAINT run_prompt_sha256_check CHECK ((length(prompt_sha256) = 32)),
    CONSTRAINT run_requirements_sha256_check CHECK ((length(requirements_sha256) = 32)),
    CONSTRAINT run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'waiting'::text, 'succeeded'::text, 'failed'::text, 'stopped'::text])))
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
    created_by bigint NOT NULL,
    suspect text
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
-- Name: page_ocr; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.page_ocr (
    file_sha256 bytea NOT NULL,
    page_no integer NOT NULL,
    is_picture boolean NOT NULL,
    text text,
    extractor text,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED,
    letters integer GENERATED ALWAYS AS (length(regexp_replace(COALESCE(text, ''::text), '[^[:alpha:]]'::text, ''::text, 'g'::text))) STORED,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT page_ocr_check CHECK (((text IS NULL) = (extractor IS NULL))),
    CONSTRAINT page_ocr_check1 CHECK (((text IS NULL) OR is_picture))
);


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
    j.trial_record_id,
    j.document_versions,
    j.served_by,
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
          WHERE ((c.trial_id = j.trial_id) AND (c.id IS DISTINCT FROM j.trial_record_id) AND (c.fetched_at > r.started_at)))) OR (EXISTS ( SELECT
           FROM (core.document_version v
             JOIN core.page_ocr o ON ((o.file_sha256 = v.file_sha256)))
          WHERE ((v.id = ANY (j.document_versions)) AND (o.text IS NOT NULL) AND (o.created_at > r.started_at))))) AS outdated,
    r.finished_at AS published_at,
    j.agreement,
    j.withdrawn_at,
    j.withdrawn_reason
   FROM (checks.judgement j
     JOIN checks.run r ON ((r.id = j.run_id)))
  WHERE ((j.error IS NULL) AND (r.status <> 'running'::text))
  ORDER BY j.trial_id, r.requirements, j.run_id DESC;


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
-- Name: TABLE actor; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON TABLE core.actor IS 'a person, or a process; actor 1 (process trialwatch) is TrialWatch itself: migrations, scheduled jobs, automatic checks and publishing';


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
-- Name: app_setting; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.app_setting (
    key text NOT NULL,
    value jsonb,
    updated_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
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
-- Name: backup; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.backup (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    file text NOT NULL,
    bytes bigint NOT NULL
);


--
-- Name: TABLE backup; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON TABLE core.backup IS 'each nightly pg_dump (compose service backup), for the backup alert';


--
-- Name: backup_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.backup ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.backup_id_seq
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
    failures integer DEFAULT 0 NOT NULL,
    failed_on date,
    CONSTRAINT evidence_transfer_check CHECK (((status = 'complete'::text) = (errors = '{}'::jsonb))),
    CONSTRAINT evidence_transfer_failures_check CHECK ((failures >= 0)),
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
    delisted_at timestamp with time zone,
    recruiting boolean
);


--
-- Name: COLUMN source_listing.recruiting; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON COLUMN core.source_listing.recruiting IS 'the source''s list says the trial recruits now (CTIS: overall status 3 or 4); NULL: it doesn''t say';


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
    e.errors,
    l.recruiting,
    COALESCE(e.failures, 0) AS failures,
    COALESCE(((e.status = 'failed'::text) AND (e.failures >= 3) AND (NOT COALESCE((l.source_updated_on >= e.failed_on), false))), false) AS stuck
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
    ocr_at timestamp with time zone,
    ocr_error text,
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
    job_id bigint DEFAULT (NULLIF(current_setting('trialwatch.job'::text, true), ''::text))::bigint,
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
-- Name: llm_batch; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.llm_batch (
    id bigint NOT NULL,
    provider_batch_id text,
    provider text DEFAULT 'anthropic'::text NOT NULL,
    operation text NOT NULL,
    job_id bigint DEFAULT (NULLIF(current_setting('trialwatch.job'::text, true), ''::text))::bigint,
    check_run_id bigint,
    annotation_run_id bigint,
    round integer NOT NULL,
    status text DEFAULT 'submitting'::text NOT NULL,
    request_count integer NOT NULL,
    estimated_usd numeric(14,6),
    counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    submitted_at timestamp with time zone,
    ended_at timestamp with time zone,
    collected_at timestamp with time zone,
    CONSTRAINT llm_batch_check CHECK (((status = ANY (ARRAY['submitting'::text, 'failed'::text])) OR (provider_batch_id IS NOT NULL))),
    CONSTRAINT llm_batch_request_count_check CHECK ((request_count >= 0)),
    CONSTRAINT llm_batch_round_check CHECK ((round >= 1)),
    CONSTRAINT llm_batch_status_check CHECK ((status = ANY (ARRAY['submitting'::text, 'submitted'::text, 'collected'::text, 'failed'::text])))
);


--
-- Name: TABLE llm_batch; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON TABLE core.llm_batch IS 'a Message Batch: one round of a run''s model requests, sent together at batch prices';


--
-- Name: llm_batch_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.llm_batch ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.llm_batch_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: llm_batch_item; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.llm_batch_item (
    llm_batch_id bigint NOT NULL,
    custom_id text NOT NULL,
    trial_id bigint NOT NULL,
    call_number integer NOT NULL,
    model text NOT NULL,
    llm_call_id bigint
);


--
-- Name: TABLE llm_batch_item; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON TABLE core.llm_batch_item IS 'one request of a Message Batch, and its call once answered';


--
-- Name: llm_call; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.llm_call (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    actor_id bigint NOT NULL,
    provider text DEFAULT 'anthropic'::text NOT NULL,
    operation text NOT NULL,
    model text NOT NULL,
    served_by text,
    check_run_id bigint,
    trial_id bigint,
    call_number integer,
    annotation_run_id bigint,
    job_id bigint DEFAULT (NULLIF(current_setting('trialwatch.job'::text, true), ''::text))::bigint,
    request jsonb DEFAULT '{}'::jsonb NOT NULL,
    response text,
    stop_reason text,
    latency_ms integer,
    error text,
    request_id text,
    attempts jsonb DEFAULT '[]'::jsonb NOT NULL,
    input_tokens bigint DEFAULT 0 NOT NULL,
    output_tokens bigint DEFAULT 0 NOT NULL,
    cache_write_tokens bigint DEFAULT 0 NOT NULL,
    cache_read_tokens bigint DEFAULT 0 NOT NULL,
    cost_usd numeric(14,6),
    unpriced_attempts integer DEFAULT 0 NOT NULL,
    tier text DEFAULT 'direct'::text NOT NULL,
    llm_batch_id bigint,
    custom_id text,
    CONSTRAINT llm_call_tier_check CHECK ((tier = ANY (ARRAY['direct'::text, 'batch'::text])))
);


--
-- Name: TABLE llm_call; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON TABLE core.llm_call IS 'every model call: the request as sent, the answer, how it ended, its billed attempts priced; the source of all spend totals';


--
-- Name: COLUMN llm_call.tier; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON COLUMN core.llm_call.tier IS 'direct: sent alone, at list prices; batch: answered in a Message Batch, at batch prices';


--
-- Name: llm_call_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.llm_call ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.llm_call_id_seq
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
    letters integer GENERATED ALWAYS AS (length(regexp_replace(text, '[^[:alpha:]]'::text, ''::text, 'g'::text))) STORED,
    CONSTRAINT page_page_no_check CHECK ((page_no >= 1))
);


--
-- Name: page_text; Type: VIEW; Schema: core; Owner: -
--

CREATE VIEW core.page_text AS
 SELECT p.file_sha256,
    p.page_no,
    COALESCE(o.text, p.text) AS text,
    COALESCE(o.extractor, p.extractor) AS extractor,
        CASE
            WHEN (o.text IS NOT NULL) THEN o.tsv
            ELSE p.tsv
        END AS tsv,
        CASE
            WHEN (o.text IS NOT NULL) THEN o.letters
            ELSE p.letters
        END AS letters,
    o.is_picture
   FROM (core.page p
     LEFT JOIN core.page_ocr o ON (((o.file_sha256 = p.file_sha256) AND (o.page_no = p.page_no))));


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
-- Name: reference_load; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.reference_load (
    id bigint NOT NULL,
    name text NOT NULL,
    version text,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    files jsonb DEFAULT '[]'::jsonb NOT NULL,
    counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    license text NOT NULL,
    started_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    finished_at timestamp with time zone,
    created_by bigint NOT NULL,
    CONSTRAINT reference_load_name_check CHECK ((name = ANY (ARRAY['geonames'::text, 'mesh'::text])))
);


--
-- Name: TABLE reference_load; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON TABLE core.reference_load IS 'each load of reference data: GeoNames (geo load), MeSH (vocab load)';


--
-- Name: reference_load_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.reference_load ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.reference_load_id_seq
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
-- Name: trial_location; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.trial_location (
    id bigint NOT NULL,
    trial_id bigint NOT NULL,
    source_id smallint NOT NULL,
    country text NOT NULL,
    city text,
    postcode text,
    site text,
    status text,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    country_key text GENERATED ALWAYS AS (geo.name_key(country)) STORED,
    postcode_key text GENERATED ALWAYS AS (geo.postcode_key(postcode)) STORED,
    city_key text GENERATED ALWAYS AS (geo.name_key(city)) STORED,
    country_iso2 character(2),
    latitude double precision,
    longitude double precision,
    CONSTRAINT trial_location_country_check CHECK ((btrim(country) <> ''::text)),
    CONSTRAINT trial_location_point_check CHECK (((latitude IS NULL) = (longitude IS NULL)))
);


--
-- Name: TABLE trial_location; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON TABLE core.trial_location IS 'where a trial runs, per source: its sites (city, postcode, country) and countries';


--
-- Name: COLUMN trial_location.country_iso2; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON COLUMN core.trial_location.country_iso2 IS 'the country''s ISO 3166 code (geo/countries.py); NULL: a name we don''t know (no part in finding trials by country)';


--
-- Name: COLUMN trial_location.latitude; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON COLUMN core.trial_location.latitude IS 'where the site is, when the source says so; else geo.address_point by its address';


--
-- Name: trial_location_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.trial_location ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.trial_location_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: trial_profile; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.trial_profile (
    trial_id bigint NOT NULL,
    source_id smallint NOT NULL,
    trial_record_id bigint,
    profile jsonb NOT NULL,
    profile_hash bytea NOT NULL,
    recruiting boolean NOT NULL,
    read_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    created_by bigint NOT NULL,
    CONSTRAINT trial_profile_profile_hash_check CHECK ((length(profile_hash) = 32))
);
ALTER TABLE ONLY core.trial_profile ALTER COLUMN profile SET COMPRESSION lz4;


--
-- Name: TABLE trial_profile; Type: COMMENT; Schema: core; Owner: -
--

COMMENT ON TABLE core.trial_profile IS 'what a person looking for a trial needs, per trial and source, read from its record (sources/profile.py)';


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
-- Name: address_point; Type: TABLE; Schema: geo; Owner: -
--

CREATE TABLE geo.address_point (
    country_key text NOT NULL,
    postcode_key text NOT NULL,
    city_key text NOT NULL,
    iso2 character(2),
    latitude double precision,
    longitude double precision,
    "precision" text NOT NULL,
    matched text,
    geocoded_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT address_point_check CHECK ((("precision" = 'none'::text) = (latitude IS NULL))),
    CONSTRAINT address_point_precision_check CHECK (("precision" = ANY (ARRAY['postcode'::text, 'city'::text, 'city-fuzzy'::text, 'none'::text])))
);


--
-- Name: TABLE address_point; Type: COMMENT; Schema: geo; Owner: -
--

COMMENT ON TABLE geo.address_point IS 'where each distinct site address is (derived from geo.postcode and geo.place; trialwatch geo geocode)';


--
-- Name: country; Type: TABLE; Schema: geo; Owner: -
--

CREATE TABLE geo.country (
    iso2 character(2) NOT NULL,
    iso3 character(3),
    name text NOT NULL,
    languages text[] DEFAULT '{}'::text[] NOT NULL,
    in_gazetteer boolean DEFAULT false CONSTRAINT country_eea_not_null NOT NULL
);


--
-- Name: TABLE country; Type: COMMENT; Schema: geo; Owner: -
--

COMMENT ON TABLE geo.country IS 'countries (GeoNames countryInfo), reference data replaced by each geo load';


--
-- Name: COLUMN country.in_gazetteer; Type: COMMENT; Schema: geo; Owner: -
--

COMMENT ON COLUMN geo.country.in_gazetteer IS 'its places and postal codes are loaded: a query''s place is looked for here';


--
-- Name: country_name; Type: TABLE; Schema: geo; Owner: -
--

CREATE TABLE geo.country_name (
    name_key text NOT NULL,
    iso2 character(2) NOT NULL,
    name text NOT NULL
);


--
-- Name: TABLE country_name; Type: COMMENT; Schema: geo; Owner: -
--

COMMENT ON TABLE geo.country_name IS 'every name we know a country by (English, its own languages, CTIS''s spellings), keyed by geo.name_key';


--
-- Name: place; Type: TABLE; Schema: geo; Owner: -
--

CREATE TABLE geo.place (
    geonameid bigint NOT NULL,
    name text NOT NULL,
    country_code character(2) NOT NULL,
    population bigint DEFAULT 0 NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL
);


--
-- Name: TABLE place; Type: COMMENT; Schema: geo; Owner: -
--

COMMENT ON TABLE geo.place IS 'populated places of the EU/EEA with 1000+ people (GeoNames cities1000)';


--
-- Name: place_name; Type: TABLE; Schema: geo; Owner: -
--

CREATE TABLE geo.place_name (
    geonameid bigint NOT NULL,
    name_key text NOT NULL
);


--
-- Name: TABLE place_name; Type: COMMENT; Schema: geo; Owner: -
--

COMMENT ON TABLE geo.place_name IS 'every name of a place (its own, ASCII, and alternate names in other languages: Wien, Vienna, Vienne)';


--
-- Name: postcode; Type: TABLE; Schema: geo; Owner: -
--

CREATE TABLE geo.postcode (
    country_code character(2) NOT NULL,
    postcode_key text NOT NULL,
    postcode text NOT NULL,
    place_name text NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL
);


--
-- Name: TABLE postcode; Type: COMMENT; Schema: geo; Owner: -
--

COMMENT ON TABLE geo.postcode IS 'postal codes with a point (GeoNames postal code files), EU/EEA';


--
-- Name: job; Type: TABLE; Schema: jobs; Owner: -
--

CREATE TABLE jobs.job (
    id bigint NOT NULL,
    kind text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    automatic boolean DEFAULT false NOT NULL,
    schedule_id integer,
    priority smallint DEFAULT 100 NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer NOT NULL,
    run_after timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    cancel_requested boolean DEFAULT false NOT NULL,
    last_error text,
    result jsonb DEFAULT '{}'::jsonb NOT NULL,
    locked_by text,
    heartbeat_at timestamp with time zone,
    created_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    trace_id text,
    progress_done integer,
    progress_total integer,
    attempt_started_at timestamp with time zone,
    failures jsonb DEFAULT '{}'::jsonb NOT NULL,
    waiting_on bigint,
    wait_until timestamp with time zone,
    CONSTRAINT job_check CHECK (((status = ANY (ARRAY['succeeded'::text, 'partial'::text, 'failed'::text, 'cancelled'::text])) = (finished_at IS NOT NULL))),
    CONSTRAINT job_kind_check CHECK ((kind = ANY (ARRAY['sync'::text, 'transfer'::text, 'check'::text, 'refresh'::text, 'annotate'::text]))),
    CONSTRAINT job_max_attempts_check CHECK ((max_attempts >= 1)),
    CONSTRAINT job_progress_done_check CHECK ((progress_done >= 0)),
    CONSTRAINT job_progress_total_check CHECK ((progress_total >= 0)),
    CONSTRAINT job_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'waiting'::text, 'held'::text, 'succeeded'::text, 'partial'::text, 'failed'::text, 'cancelled'::text]))),
    CONSTRAINT job_waiting_check CHECK (((status = 'waiting'::text) = (waiting_on IS NOT NULL)))
);


--
-- Name: COLUMN job.trace_id; Type: COMMENT; Schema: jobs; Owner: -
--

COMMENT ON COLUMN jobs.job.trace_id IS 'the trace of its latest attempt (32 hex digits), when tracing is on';


--
-- Name: job_id_seq; Type: SEQUENCE; Schema: jobs; Owner: -
--

ALTER TABLE jobs.job ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME jobs.job_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: job_line; Type: TABLE; Schema: jobs; Owner: -
--

CREATE TABLE jobs.job_line (
    id bigint NOT NULL,
    job_id bigint NOT NULL,
    attempt integer NOT NULL,
    at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    line text NOT NULL
);


--
-- Name: job_line_id_seq; Type: SEQUENCE; Schema: jobs; Owner: -
--

ALTER TABLE jobs.job_line ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME jobs.job_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: schedule; Type: TABLE; Schema: jobs; Owner: -
--

CREATE TABLE jobs.schedule (
    id integer NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    weekday smallint NOT NULL,
    hour_utc smallint NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    next_run_at timestamp with time zone NOT NULL,
    frequency text DEFAULT 'weekly'::text NOT NULL,
    CONSTRAINT schedule_frequency_check CHECK ((frequency = ANY (ARRAY['daily'::text, 'weekly'::text, 'monthly'::text]))),
    CONSTRAINT schedule_hour_utc_check CHECK (((hour_utc >= 0) AND (hour_utc <= 23))),
    CONSTRAINT schedule_kind_check CHECK ((kind = ANY (ARRAY['sync'::text, 'transfer'::text, 'check'::text, 'refresh'::text, 'annotate'::text]))),
    CONSTRAINT schedule_weekday_check CHECK (((weekday >= 1) AND (weekday <= 7)))
);


--
-- Name: COLUMN schedule.weekday; Type: COMMENT; Schema: jobs; Owner: -
--

COMMENT ON COLUMN jobs.schedule.weekday IS 'ISO day of the week (1 Monday .. 7 Sunday); used when weekly';


--
-- Name: COLUMN schedule.frequency; Type: COMMENT; Schema: jobs; Owner: -
--

COMMENT ON COLUMN jobs.schedule.frequency IS 'daily | weekly | monthly (on the 1st)';


--
-- Name: schedule_id_seq; Type: SEQUENCE; Schema: jobs; Owner: -
--

ALTER TABLE jobs.schedule ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME jobs.schedule_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying NOT NULL
);


--
-- Name: backups; Type: VIEW; Schema: report; Owner: -
--

CREATE VIEW report.backups AS
 SELECT at,
    file,
    bytes
   FROM core.backup;


--
-- Name: check_runs; Type: VIEW; Schema: report; Owner: -
--

CREATE VIEW report.check_runs AS
 SELECT id,
    started_at,
    finished_at,
    status,
    model,
    effort,
    prompt,
    requirements,
    trials,
    errors,
    job_id,
    ( SELECT COALESCE(sum(c.cost_usd), (0)::numeric) AS "coalesce"
           FROM core.llm_call c
          WHERE (c.check_run_id = r.id)) AS cost_usd
   FROM checks.run r;


--
-- Name: ingest_runs; Type: VIEW; Schema: report; Owner: -
--

CREATE VIEW report.ingest_runs AS
 SELECT i.id,
    s.code AS source,
    i.kind,
    i.status,
    i.started_at,
    i.finished_at,
    i.counts,
    ( SELECT count(*) AS count
           FROM jsonb_object_keys(COALESCE(i.errors, '{}'::jsonb)) jsonb_object_keys(jsonb_object_keys)) AS errors
   FROM (core.ingest_run i
     JOIN core.source s ON ((s.id = i.source_id)));


--
-- Name: jobs; Type: VIEW; Schema: report; Owner: -
--

CREATE VIEW report.jobs AS
 SELECT id,
    kind,
    status,
    automatic,
    attempts,
    max_attempts,
    created_at,
    started_at,
    finished_at,
    run_after,
    last_error,
    trace_id,
    EXTRACT(epoch FROM (finished_at - started_at)) AS seconds
   FROM jobs.job j;


--
-- Name: judgements; Type: VIEW; Schema: report; Owner: -
--

CREATE VIEW report.judgements AS
 SELECT j.run_id,
    j.trial_id,
    r.finished_at AS at,
    r.requirements,
    r.model,
    j.label,
    (j.error IS NOT NULL) AS failed,
    ((j.error IS NULL) AND (r.status <> 'running'::text)) AS published,
    ((j.agreement ->> 'confidence'::text))::real AS confidence,
    (j.withdrawn_at IS NOT NULL) AS withdrawn
   FROM (checks.judgement j
     JOIN checks.run r ON ((r.id = j.run_id)));


--
-- Name: llm_calls; Type: VIEW; Schema: report; Owner: -
--

CREATE VIEW report.llm_calls AS
 SELECT id,
    at,
    operation,
    model,
    served_by,
    check_run_id,
    annotation_run_id,
    job_id,
    input_tokens,
    output_tokens,
    cache_write_tokens,
    cache_read_tokens,
    cost_usd,
    unpriced_attempts,
    latency_ms,
    stop_reason,
    (error IS NOT NULL) AS failed,
    split_part(error, ':'::text, 1) AS error_type
   FROM core.llm_call c;


--
-- Name: sources; Type: VIEW; Schema: report; Owner: -
--

CREATE VIEW report.sources AS
 SELECT code,
    enabled,
    ( SELECT max(i.finished_at) AS max
           FROM core.ingest_run i
          WHERE ((i.source_id = s.id) AND (i.kind = 'list'::text) AND (i.status = 'succeeded'::text))) AS last_sync,
    ( SELECT max(i.finished_at) AS max
           FROM core.ingest_run i
          WHERE ((i.source_id = s.id) AND (i.kind = 'transfer'::text))) AS last_transfer,
    ( SELECT count(*) AS count
           FROM core.evidence_transfer e
          WHERE ((e.source_id = s.id) AND (e.status = 'failed'::text))) AS failed_transfers
   FROM core.source s;


--
-- Name: spend; Type: VIEW; Schema: report; Owner: -
--

CREATE VIEW report.spend AS
 SELECT c.at,
    c.operation,
    a.model,
    a.input_tokens,
    a.output_tokens,
    a.cost_usd,
        CASE
            WHEN (p.kind = 'process'::text) THEN p.name
            ELSE 'a person'::text
        END AS made_for
   FROM ((core.llm_call c
     JOIN core.actor p ON ((p.id = c.actor_id)))
     CROSS JOIN LATERAL jsonb_to_recordset(c.attempts) a(model text, input_tokens bigint, output_tokens bigint, cost_usd numeric(14,6)));


--
-- Name: spend_cap; Type: VIEW; Schema: report; Owner: -
--

CREATE VIEW report.spend_cap AS
 SELECT ( SELECT ((app_setting.value #>> '{}'::text[]))::numeric AS "numeric"
           FROM core.app_setting
          WHERE (app_setting.key = 'spend.monthly_cap_usd'::text)) AS cap_usd,
    ( SELECT COALESCE(sum(llm_call.cost_usd), (0)::numeric) AS "coalesce"
           FROM core.llm_call
          WHERE (llm_call.at >= date_trunc('month'::text, now()))) AS this_month_usd;


--
-- Name: annotation; Type: TABLE; Schema: search; Owner: -
--

CREATE TABLE search.annotation (
    id bigint NOT NULL,
    run_id bigint CONSTRAINT annotation_run_id_not_null1 NOT NULL,
    trial_id bigint NOT NULL,
    profile_hash bytea NOT NULL,
    facts jsonb DEFAULT '{}'::jsonb NOT NULL,
    dropped jsonb DEFAULT '[]'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT annotation_profile_hash_check CHECK ((length(profile_hash) = 32))
);


--
-- Name: TABLE annotation; Type: COMMENT; Schema: search; Owner: -
--

COMMENT ON TABLE search.annotation IS 'one trial''s AI tags in a run: kept facts with their quotes, dropped ones with why';


--
-- Name: annotation_id_seq; Type: SEQUENCE; Schema: search; Owner: -
--

ALTER TABLE search.annotation ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME search.annotation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: annotation_run; Type: TABLE; Schema: search; Owner: -
--

CREATE TABLE search.annotation_run (
    id bigint NOT NULL,
    model text NOT NULL,
    effort text NOT NULL,
    prompt text NOT NULL,
    prompt_sha256 bytea NOT NULL,
    prompt_text text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    error text,
    trials integer DEFAULT 0 NOT NULL,
    job_id bigint DEFAULT (NULLIF(current_setting('trialwatch.job'::text, true), ''::text))::bigint,
    started_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    finished_at timestamp with time zone,
    created_by bigint NOT NULL,
    CONSTRAINT annotation_run_prompt_sha256_check CHECK ((length(prompt_sha256) = 32)),
    CONSTRAINT annotation_run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'waiting'::text, 'succeeded'::text, 'failed'::text, 'stopped'::text])))
);


--
-- Name: TABLE annotation_run; Type: COMMENT; Schema: search; Owner: -
--

COMMENT ON TABLE search.annotation_run IS 'a run of AI tagging of recruiting trials, with the exact prompt it sent';


--
-- Name: annotation_run_id_seq; Type: SEQUENCE; Schema: search; Owner: -
--

ALTER TABLE search.annotation_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME search.annotation_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: document; Type: TABLE; Schema: search; Owner: -
--

CREATE TABLE search.document (
    trial_id bigint NOT NULL,
    source_id smallint NOT NULL,
    profile_hash bytea NOT NULL,
    recruiting boolean NOT NULL,
    recruiting_countries text[] DEFAULT '{}'::text[] NOT NULL,
    title text,
    conditions text[] DEFAULT '{}'::text[] NOT NULL,
    categories text[] DEFAULT '{}'::text[] NOT NULL,
    phase text,
    age_groups text[] DEFAULT '{}'::text[] NOT NULL,
    female boolean,
    male boolean,
    healthy_volunteers boolean,
    patients boolean,
    rare boolean DEFAULT false NOT NULL,
    tsv tsvector NOT NULL,
    built_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    annotation_id bigint,
    age_min numeric,
    age_max numeric,
    interventions text[] DEFAULT '{}'::text[] NOT NULL,
    purposes text[] DEFAULT '{}'::text[] NOT NULL
);


--
-- Name: TABLE document; Type: COMMENT; Schema: search; Owner: -
--

COMMENT ON TABLE search.document IS 'one per trial, built from its profile: what patient search matches (ADR-008)';


--
-- Name: term; Type: TABLE; Schema: search; Owner: -
--

CREATE TABLE search.term (
    term text NOT NULL,
    documents integer NOT NULL
);


--
-- Name: TABLE term; Type: COMMENT; Schema: search; Owner: -
--

COMMENT ON TABLE search.term IS 'words of recruiting trials'' conditions and titles, for "did you mean"';


--
-- Name: trial_concept; Type: TABLE; Schema: search; Owner: -
--

CREATE TABLE search.trial_concept (
    trial_id bigint NOT NULL,
    ui text NOT NULL,
    method text NOT NULL,
    field text NOT NULL,
    quote text NOT NULL,
    CONSTRAINT trial_concept_method_check CHECK ((method = ANY (ARRAY['exact-name'::text, 'same-words'::text, 'phrase'::text, 'source-code'::text, 'ai-quote'::text])))
);


--
-- Name: TABLE trial_concept; Type: COMMENT; Schema: search; Owner: -
--

COMMENT ON TABLE search.trial_concept IS 'the MeSH descriptors a trial''s conditions name, with the words they were found in';


--
-- Name: mesh_descriptor; Type: TABLE; Schema: vocab; Owner: -
--

CREATE TABLE vocab.mesh_descriptor (
    ui text NOT NULL,
    name text NOT NULL,
    tree_numbers text[] NOT NULL,
    CONSTRAINT mesh_descriptor_ui_check CHECK ((ui ~ '^D[0-9]+$'::text))
);


--
-- Name: TABLE mesh_descriptor; Type: COMMENT; Schema: vocab; Owner: -
--

COMMENT ON TABLE vocab.mesh_descriptor IS 'MeSH descriptors of diseases (C) and mental disorders (F03)';


--
-- Name: mesh_term; Type: TABLE; Schema: vocab; Owner: -
--

CREATE TABLE vocab.mesh_term (
    name_key text NOT NULL,
    ui text NOT NULL,
    term text NOT NULL,
    words_key text NOT NULL
);


--
-- Name: TABLE mesh_term; Type: COMMENT; Schema: vocab; Owner: -
--

COMMENT ON TABLE vocab.mesh_term IS 'every entry term of a MeSH descriptor (its name and synonyms), keyed by geo.name_key';


--
-- Name: mesh_tree; Type: TABLE; Schema: vocab; Owner: -
--

CREATE TABLE vocab.mesh_tree (
    tree_number text NOT NULL,
    ui text NOT NULL
);


--
-- Name: TABLE mesh_tree; Type: COMMENT; Schema: vocab; Owner: -
--

COMMENT ON TABLE vocab.mesh_tree IS 'where each MeSH descriptor sits in the hierarchy (C18.452.394.750 is under C18.452.394)';


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
-- Name: answer answer_evidence_id_key; Type: CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.answer
    ADD CONSTRAINT answer_evidence_id_key UNIQUE (evidence_id);


--
-- Name: answer answer_pkey; Type: CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.answer
    ADD CONSTRAINT answer_pkey PRIMARY KEY (run_id, trial_id, key);


--
-- Name: judgement judgement_pkey; Type: CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.judgement
    ADD CONSTRAINT judgement_pkey PRIMARY KEY (run_id, trial_id);


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
-- Name: app_setting app_setting_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.app_setting
    ADD CONSTRAINT app_setting_pkey PRIMARY KEY (key);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: backup backup_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.backup
    ADD CONSTRAINT backup_pkey PRIMARY KEY (id);


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
-- Name: llm_batch_item llm_batch_item_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_batch_item
    ADD CONSTRAINT llm_batch_item_pkey PRIMARY KEY (llm_batch_id, custom_id);


--
-- Name: llm_batch llm_batch_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_batch
    ADD CONSTRAINT llm_batch_pkey PRIMARY KEY (id);


--
-- Name: llm_batch llm_batch_provider_batch_id_key; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_batch
    ADD CONSTRAINT llm_batch_provider_batch_id_key UNIQUE (provider_batch_id);


--
-- Name: llm_call llm_call_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_call
    ADD CONSTRAINT llm_call_pkey PRIMARY KEY (id);


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
-- Name: page_ocr page_ocr_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.page_ocr
    ADD CONSTRAINT page_ocr_pkey PRIMARY KEY (file_sha256, page_no);


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
-- Name: reference_load reference_load_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.reference_load
    ADD CONSTRAINT reference_load_pkey PRIMARY KEY (id);


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
-- Name: trial_location trial_location_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_location
    ADD CONSTRAINT trial_location_pkey PRIMARY KEY (id);


--
-- Name: trial trial_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial
    ADD CONSTRAINT trial_pkey PRIMARY KEY (id);


--
-- Name: trial_profile trial_profile_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_profile
    ADD CONSTRAINT trial_profile_pkey PRIMARY KEY (trial_id, source_id);


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
-- Name: address_point address_point_pkey; Type: CONSTRAINT; Schema: geo; Owner: -
--

ALTER TABLE ONLY geo.address_point
    ADD CONSTRAINT address_point_pkey PRIMARY KEY (country_key, postcode_key, city_key);


--
-- Name: country_name country_name_pkey; Type: CONSTRAINT; Schema: geo; Owner: -
--

ALTER TABLE ONLY geo.country_name
    ADD CONSTRAINT country_name_pkey PRIMARY KEY (name_key, iso2);


--
-- Name: country country_pkey; Type: CONSTRAINT; Schema: geo; Owner: -
--

ALTER TABLE ONLY geo.country
    ADD CONSTRAINT country_pkey PRIMARY KEY (iso2);


--
-- Name: place_name place_name_pkey; Type: CONSTRAINT; Schema: geo; Owner: -
--

ALTER TABLE ONLY geo.place_name
    ADD CONSTRAINT place_name_pkey PRIMARY KEY (name_key, geonameid);


--
-- Name: place place_pkey; Type: CONSTRAINT; Schema: geo; Owner: -
--

ALTER TABLE ONLY geo.place
    ADD CONSTRAINT place_pkey PRIMARY KEY (geonameid);


--
-- Name: job_line job_line_pkey; Type: CONSTRAINT; Schema: jobs; Owner: -
--

ALTER TABLE ONLY jobs.job_line
    ADD CONSTRAINT job_line_pkey PRIMARY KEY (id);


--
-- Name: job job_pkey; Type: CONSTRAINT; Schema: jobs; Owner: -
--

ALTER TABLE ONLY jobs.job
    ADD CONSTRAINT job_pkey PRIMARY KEY (id);


--
-- Name: schedule schedule_name_key; Type: CONSTRAINT; Schema: jobs; Owner: -
--

ALTER TABLE ONLY jobs.schedule
    ADD CONSTRAINT schedule_name_key UNIQUE (name);


--
-- Name: schedule schedule_pkey; Type: CONSTRAINT; Schema: jobs; Owner: -
--

ALTER TABLE ONLY jobs.schedule
    ADD CONSTRAINT schedule_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: annotation annotation_pkey; Type: CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.annotation
    ADD CONSTRAINT annotation_pkey PRIMARY KEY (id);


--
-- Name: annotation annotation_run_id_trial_id_key; Type: CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.annotation
    ADD CONSTRAINT annotation_run_id_trial_id_key UNIQUE (run_id, trial_id);


--
-- Name: annotation_run annotation_run_pkey; Type: CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.annotation_run
    ADD CONSTRAINT annotation_run_pkey PRIMARY KEY (id);


--
-- Name: document document_pkey; Type: CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.document
    ADD CONSTRAINT document_pkey PRIMARY KEY (trial_id);


--
-- Name: term term_pkey; Type: CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.term
    ADD CONSTRAINT term_pkey PRIMARY KEY (term);


--
-- Name: trial_concept trial_concept_pkey; Type: CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.trial_concept
    ADD CONSTRAINT trial_concept_pkey PRIMARY KEY (trial_id, ui, field);


--
-- Name: mesh_descriptor mesh_descriptor_pkey; Type: CONSTRAINT; Schema: vocab; Owner: -
--

ALTER TABLE ONLY vocab.mesh_descriptor
    ADD CONSTRAINT mesh_descriptor_pkey PRIMARY KEY (ui);


--
-- Name: mesh_term mesh_term_pkey; Type: CONSTRAINT; Schema: vocab; Owner: -
--

ALTER TABLE ONLY vocab.mesh_term
    ADD CONSTRAINT mesh_term_pkey PRIMARY KEY (name_key, ui);


--
-- Name: mesh_tree mesh_tree_pkey; Type: CONSTRAINT; Schema: vocab; Owner: -
--

ALTER TABLE ONLY vocab.mesh_tree
    ADD CONSTRAINT mesh_tree_pkey PRIMARY KEY (tree_number);


--
-- Name: session_user_idx; Type: INDEX; Schema: auth; Owner: -
--

CREATE INDEX session_user_idx ON auth.session USING btree (user_id);


--
-- Name: answer_file_idx; Type: INDEX; Schema: checks; Owner: -
--

CREATE INDEX answer_file_idx ON checks.answer USING btree (file_sha256) WHERE (file_sha256 IS NOT NULL);


--
-- Name: judgement_trial_idx; Type: INDEX; Schema: checks; Owner: -
--

CREATE INDEX judgement_trial_idx ON checks.judgement USING btree (trial_id, run_id DESC);


--
-- Name: run_job_idx; Type: INDEX; Schema: checks; Owner: -
--

CREATE INDEX run_job_idx ON checks.run USING btree (job_id);


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
-- Name: ingest_run_job_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX ingest_run_job_idx ON core.ingest_run USING btree (job_id);


--
-- Name: ingest_run_source_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX ingest_run_source_idx ON core.ingest_run USING btree (source_id, started_at DESC);


--
-- Name: llm_batch_job_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX llm_batch_job_idx ON core.llm_batch USING btree (job_id);


--
-- Name: llm_call_annotation_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX llm_call_annotation_idx ON core.llm_call USING btree (annotation_run_id, trial_id);


--
-- Name: llm_call_at_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX llm_call_at_idx ON core.llm_call USING btree (at);


--
-- Name: llm_call_job_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX llm_call_job_idx ON core.llm_call USING btree (job_id);


--
-- Name: llm_call_run_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX llm_call_run_idx ON core.llm_call USING btree (check_run_id, trial_id);


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
-- Name: trial_location_address_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_location_address_idx ON core.trial_location USING btree (country_key, postcode_key, city_key);


--
-- Name: trial_location_city_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_location_city_idx ON core.trial_location USING btree (lower(city));


--
-- Name: trial_location_country_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_location_country_idx ON core.trial_location USING btree (lower(country));


--
-- Name: trial_location_iso2_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_location_iso2_idx ON core.trial_location USING btree (trial_id, country_iso2);


--
-- Name: trial_location_trial_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_location_trial_idx ON core.trial_location USING btree (trial_id);


--
-- Name: trial_profile_recruiting_idx; Type: INDEX; Schema: core; Owner: -
--

CREATE INDEX trial_profile_recruiting_idx ON core.trial_profile USING btree (trial_id) WHERE recruiting;


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
-- Name: address_point_latitude_idx; Type: INDEX; Schema: geo; Owner: -
--

CREATE INDEX address_point_latitude_idx ON geo.address_point USING btree (latitude) WHERE (latitude IS NOT NULL);


--
-- Name: place_country_idx; Type: INDEX; Schema: geo; Owner: -
--

CREATE INDEX place_country_idx ON geo.place USING btree (country_code);


--
-- Name: place_name_trgm_idx; Type: INDEX; Schema: geo; Owner: -
--

CREATE INDEX place_name_trgm_idx ON geo.place_name USING gin (name_key public.gin_trgm_ops);


--
-- Name: postcode_idx; Type: INDEX; Schema: geo; Owner: -
--

CREATE INDEX postcode_idx ON geo.postcode USING btree (country_code, postcode_key);


--
-- Name: job_line_job_idx; Type: INDEX; Schema: jobs; Owner: -
--

CREATE INDEX job_line_job_idx ON jobs.job_line USING btree (job_id, id);


--
-- Name: job_queue_idx; Type: INDEX; Schema: jobs; Owner: -
--

CREATE INDEX job_queue_idx ON jobs.job USING btree (priority, id) WHERE (status = 'queued'::text);


--
-- Name: job_status_idx; Type: INDEX; Schema: jobs; Owner: -
--

CREATE INDEX job_status_idx ON jobs.job USING btree (status, id DESC);


--
-- Name: annotation_trial_idx; Type: INDEX; Schema: search; Owner: -
--

CREATE INDEX annotation_trial_idx ON search.annotation USING btree (trial_id, profile_hash, id DESC);


--
-- Name: document_recruiting_idx; Type: INDEX; Schema: search; Owner: -
--

CREATE INDEX document_recruiting_idx ON search.document USING btree (trial_id) WHERE recruiting;


--
-- Name: document_tsv_idx; Type: INDEX; Schema: search; Owner: -
--

CREATE INDEX document_tsv_idx ON search.document USING gin (tsv);


--
-- Name: term_trgm_idx; Type: INDEX; Schema: search; Owner: -
--

CREATE INDEX term_trgm_idx ON search.term USING gin (term public.gin_trgm_ops);


--
-- Name: mesh_term_trgm_idx; Type: INDEX; Schema: vocab; Owner: -
--

CREATE INDEX mesh_term_trgm_idx ON vocab.mesh_term USING gin (name_key public.gin_trgm_ops);


--
-- Name: mesh_term_words_idx; Type: INDEX; Schema: vocab; Owner: -
--

CREATE INDEX mesh_term_words_idx ON vocab.mesh_term USING btree (words_key);


--
-- Name: mesh_tree_ui_idx; Type: INDEX; Schema: vocab; Owner: -
--

CREATE INDEX mesh_tree_ui_idx ON vocab.mesh_tree USING btree (ui);


--
-- Name: app_user app_user_audit; Type: TRIGGER; Schema: auth; Owner: -
--

CREATE TRIGGER app_user_audit AFTER INSERT OR DELETE OR UPDATE ON auth.app_user FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'password_hash');


--
-- Name: judgement judgement_withdrawn_audit; Type: TRIGGER; Schema: checks; Owner: -
--

CREATE TRIGGER judgement_withdrawn_audit AFTER UPDATE OF withdrawn_at, withdrawn_by, withdrawn_reason ON checks.judgement FOR EACH ROW EXECUTE FUNCTION core.audit('run_id,trial_id', 'trace,parts,agreement');


--
-- Name: actor actor_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER actor_audit AFTER DELETE OR UPDATE ON core.actor FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: app_setting app_setting_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER app_setting_audit AFTER INSERT OR DELETE OR UPDATE ON core.app_setting FOR EACH ROW EXECUTE FUNCTION core.audit('key');


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
-- Name: page_ocr page_ocr_never_changes; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER page_ocr_never_changes BEFORE DELETE OR UPDATE ON core.page_ocr FOR EACH ROW EXECUTE FUNCTION core.refuse_change();


--
-- Name: possible_duplicate possible_duplicate_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER possible_duplicate_audit AFTER INSERT OR DELETE OR UPDATE ON core.possible_duplicate FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: reference_load reference_load_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER reference_load_audit AFTER INSERT OR DELETE OR UPDATE ON core.reference_load FOR EACH ROW EXECUTE FUNCTION core.audit('id');


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
-- Name: trial_location trial_location_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER trial_location_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_location FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: trial_profile trial_profile_audit; Type: TRIGGER; Schema: core; Owner: -
--

CREATE TRIGGER trial_profile_audit AFTER INSERT OR DELETE OR UPDATE ON core.trial_profile FOR EACH ROW EXECUTE FUNCTION core.audit('trial_id,source_id', 'profile,read_at');


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
-- Name: job job_audit; Type: TRIGGER; Schema: jobs; Owner: -
--

CREATE TRIGGER job_audit AFTER INSERT OR DELETE OR UPDATE ON jobs.job FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'heartbeat_at');


--
-- Name: schedule schedule_audit; Type: TRIGGER; Schema: jobs; Owner: -
--

CREATE TRIGGER schedule_audit AFTER INSERT OR DELETE OR UPDATE ON jobs.schedule FOR EACH ROW EXECUTE FUNCTION core.audit('id');


--
-- Name: annotation_run annotation_run_audit; Type: TRIGGER; Schema: search; Owner: -
--

CREATE TRIGGER annotation_run_audit AFTER INSERT OR DELETE OR UPDATE ON search.annotation_run FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'prompt_text');


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
-- Name: answer answer_document_version_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.answer
    ADD CONSTRAINT answer_document_version_id_fkey FOREIGN KEY (document_version_id) REFERENCES core.document_version(id);


--
-- Name: answer answer_file_sha256_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.answer
    ADD CONSTRAINT answer_file_sha256_fkey FOREIGN KEY (file_sha256) REFERENCES core.file(sha256);


--
-- Name: answer answer_run_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.answer
    ADD CONSTRAINT answer_run_id_fkey FOREIGN KEY (run_id) REFERENCES checks.run(id);


--
-- Name: answer answer_trial_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.answer
    ADD CONSTRAINT answer_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


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
-- Name: judgement judgement_withdrawn_by_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.judgement
    ADD CONSTRAINT judgement_withdrawn_by_fkey FOREIGN KEY (withdrawn_by) REFERENCES core.actor(id);


--
-- Name: run run_actor_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.run
    ADD CONSTRAINT run_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES core.actor(id);


--
-- Name: run run_job_id_fkey; Type: FK CONSTRAINT; Schema: checks; Owner: -
--

ALTER TABLE ONLY checks.run
    ADD CONSTRAINT run_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs.job(id);


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
-- Name: ingest_run ingest_run_job_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.ingest_run
    ADD CONSTRAINT ingest_run_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs.job(id);


--
-- Name: ingest_run ingest_run_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.ingest_run
    ADD CONSTRAINT ingest_run_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: llm_batch llm_batch_annotation_run_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_batch
    ADD CONSTRAINT llm_batch_annotation_run_id_fkey FOREIGN KEY (annotation_run_id) REFERENCES search.annotation_run(id);


--
-- Name: llm_batch llm_batch_check_run_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_batch
    ADD CONSTRAINT llm_batch_check_run_id_fkey FOREIGN KEY (check_run_id) REFERENCES checks.run(id);


--
-- Name: llm_batch_item llm_batch_item_llm_batch_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_batch_item
    ADD CONSTRAINT llm_batch_item_llm_batch_id_fkey FOREIGN KEY (llm_batch_id) REFERENCES core.llm_batch(id);


--
-- Name: llm_batch_item llm_batch_item_llm_call_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_batch_item
    ADD CONSTRAINT llm_batch_item_llm_call_id_fkey FOREIGN KEY (llm_call_id) REFERENCES core.llm_call(id);


--
-- Name: llm_batch_item llm_batch_item_trial_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_batch_item
    ADD CONSTRAINT llm_batch_item_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: llm_batch llm_batch_job_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_batch
    ADD CONSTRAINT llm_batch_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs.job(id);


--
-- Name: llm_call llm_call_actor_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_call
    ADD CONSTRAINT llm_call_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES core.actor(id);


--
-- Name: llm_call llm_call_annotation_run_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_call
    ADD CONSTRAINT llm_call_annotation_run_id_fkey FOREIGN KEY (annotation_run_id) REFERENCES search.annotation_run(id);


--
-- Name: llm_call llm_call_check_run_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_call
    ADD CONSTRAINT llm_call_check_run_id_fkey FOREIGN KEY (check_run_id) REFERENCES checks.run(id);


--
-- Name: llm_call llm_call_job_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_call
    ADD CONSTRAINT llm_call_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs.job(id);


--
-- Name: llm_call llm_call_llm_batch_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_call
    ADD CONSTRAINT llm_call_llm_batch_id_fkey FOREIGN KEY (llm_batch_id) REFERENCES core.llm_batch(id);


--
-- Name: llm_call llm_call_trial_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.llm_call
    ADD CONSTRAINT llm_call_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


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
-- Name: page_ocr page_ocr_file_sha256_page_no_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.page_ocr
    ADD CONSTRAINT page_ocr_file_sha256_page_no_fkey FOREIGN KEY (file_sha256, page_no) REFERENCES core.page(file_sha256, page_no);


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
-- Name: reference_load reference_load_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.reference_load
    ADD CONSTRAINT reference_load_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


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
-- Name: trial_location trial_location_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_location
    ADD CONSTRAINT trial_location_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: trial_location trial_location_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_location
    ADD CONSTRAINT trial_location_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: trial_location trial_location_trial_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_location
    ADD CONSTRAINT trial_location_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: trial trial_merged_into_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial
    ADD CONSTRAINT trial_merged_into_fkey FOREIGN KEY (merged_into) REFERENCES core.trial(id);


--
-- Name: trial_profile trial_profile_created_by_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_profile
    ADD CONSTRAINT trial_profile_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: trial_profile trial_profile_source_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_profile
    ADD CONSTRAINT trial_profile_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: trial_profile trial_profile_trial_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_profile
    ADD CONSTRAINT trial_profile_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: trial_profile trial_profile_trial_record_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.trial_profile
    ADD CONSTRAINT trial_profile_trial_record_id_fkey FOREIGN KEY (trial_record_id) REFERENCES core.trial_record(id);


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
-- Name: country_name country_name_iso2_fkey; Type: FK CONSTRAINT; Schema: geo; Owner: -
--

ALTER TABLE ONLY geo.country_name
    ADD CONSTRAINT country_name_iso2_fkey FOREIGN KEY (iso2) REFERENCES geo.country(iso2);


--
-- Name: place_name place_name_geonameid_fkey; Type: FK CONSTRAINT; Schema: geo; Owner: -
--

ALTER TABLE ONLY geo.place_name
    ADD CONSTRAINT place_name_geonameid_fkey FOREIGN KEY (geonameid) REFERENCES geo.place(geonameid) ON DELETE CASCADE;


--
-- Name: job job_created_by_fkey; Type: FK CONSTRAINT; Schema: jobs; Owner: -
--

ALTER TABLE ONLY jobs.job
    ADD CONSTRAINT job_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: job_line job_line_job_id_fkey; Type: FK CONSTRAINT; Schema: jobs; Owner: -
--

ALTER TABLE ONLY jobs.job_line
    ADD CONSTRAINT job_line_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs.job(id) ON DELETE CASCADE;


--
-- Name: job job_schedule_id_fkey; Type: FK CONSTRAINT; Schema: jobs; Owner: -
--

ALTER TABLE ONLY jobs.job
    ADD CONSTRAINT job_schedule_id_fkey FOREIGN KEY (schedule_id) REFERENCES jobs.schedule(id);


--
-- Name: job job_waiting_on_fkey; Type: FK CONSTRAINT; Schema: jobs; Owner: -
--

ALTER TABLE ONLY jobs.job
    ADD CONSTRAINT job_waiting_on_fkey FOREIGN KEY (waiting_on) REFERENCES core.llm_batch(id);


--
-- Name: annotation_run annotation_run_created_by_fkey; Type: FK CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.annotation_run
    ADD CONSTRAINT annotation_run_created_by_fkey FOREIGN KEY (created_by) REFERENCES core.actor(id);


--
-- Name: annotation annotation_run_id_fkey; Type: FK CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.annotation
    ADD CONSTRAINT annotation_run_id_fkey FOREIGN KEY (run_id) REFERENCES search.annotation_run(id);


--
-- Name: annotation_run annotation_run_job_id_fkey; Type: FK CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.annotation_run
    ADD CONSTRAINT annotation_run_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs.job(id);


--
-- Name: annotation annotation_trial_id_fkey; Type: FK CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.annotation
    ADD CONSTRAINT annotation_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: document document_annotation_id_fkey; Type: FK CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.document
    ADD CONSTRAINT document_annotation_id_fkey FOREIGN KEY (annotation_id) REFERENCES search.annotation(id);


--
-- Name: document document_source_id_fkey; Type: FK CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.document
    ADD CONSTRAINT document_source_id_fkey FOREIGN KEY (source_id) REFERENCES core.source(id);


--
-- Name: document document_trial_id_fkey; Type: FK CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.document
    ADD CONSTRAINT document_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: trial_concept trial_concept_trial_id_fkey; Type: FK CONSTRAINT; Schema: search; Owner: -
--

ALTER TABLE ONLY search.trial_concept
    ADD CONSTRAINT trial_concept_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES core.trial(id);


--
-- Name: mesh_term mesh_term_ui_fkey; Type: FK CONSTRAINT; Schema: vocab; Owner: -
--

ALTER TABLE ONLY vocab.mesh_term
    ADD CONSTRAINT mesh_term_ui_fkey FOREIGN KEY (ui) REFERENCES vocab.mesh_descriptor(ui) ON DELETE CASCADE;


--
-- Name: mesh_tree mesh_tree_ui_fkey; Type: FK CONSTRAINT; Schema: vocab; Owner: -
--

ALTER TABLE ONLY vocab.mesh_tree
    ADD CONSTRAINT mesh_tree_ui_fkey FOREIGN KEY (ui) REFERENCES vocab.mesh_descriptor(ui) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict dbmate


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20261121120000'),
    ('20261124100000'),
    ('20261124110000'),
    ('20261124120000'),
    ('20261124130000'),
    ('20261124140000'),
    ('20261124150000'),
    ('20261124160000'),
    ('20261124170000'),
    ('20261124180000'),
    ('20261124190000'),
    ('20261125100000'),
    ('20261125110000'),
    ('20261125120000'),
    ('20261126100000'),
    ('20261127100000'),
    ('20261127110000'),
    ('20261127120000');
