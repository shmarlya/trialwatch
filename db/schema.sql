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
-- Name: ctis; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA ctis;


--
-- Name: gold; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA gold;


--
-- Name: ref; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA ref;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: fetch_run; Type: TABLE; Schema: ctis; Owner: -
--

CREATE TABLE ctis.fetch_run (
    id bigint NOT NULL,
    kind text NOT NULL,
    args jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    trials_fetched integer DEFAULT 0 NOT NULL,
    trials_changed integer DEFAULT 0 NOT NULL,
    trials_failed integer DEFAULT 0 NOT NULL,
    error text,
    CONSTRAINT fetch_run_check CHECK (((status = 'running'::text) = (finished_at IS NULL))),
    CONSTRAINT fetch_run_kind_check CHECK ((kind = ANY (ARRAY['fetch'::text, 'seed'::text, 'sync'::text]))),
    CONSTRAINT fetch_run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text])))
);


--
-- Name: fetch_run_id_seq; Type: SEQUENCE; Schema: ctis; Owner: -
--

ALTER TABLE ctis.fetch_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME ctis.fetch_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: search_export_snapshot; Type: TABLE; Schema: ctis; Owner: -
--

CREATE TABLE ctis.search_export_snapshot (
    id bigint NOT NULL,
    ct_number text NOT NULL,
    fetch_run_id bigint NOT NULL,
    captured_on date NOT NULL,
    source_file text NOT NULL,
    content_hash bytea NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT search_export_snapshot_content_hash_check CHECK ((length(content_hash) = 32))
);
ALTER TABLE ONLY ctis.search_export_snapshot ALTER COLUMN payload SET COMPRESSION lz4;


--
-- Name: search_export_snapshot_id_seq; Type: SEQUENCE; Schema: ctis; Owner: -
--

ALTER TABLE ctis.search_export_snapshot ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME ctis.search_export_snapshot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: trial; Type: TABLE; Schema: ctis; Owner: -
--

CREATE TABLE ctis.trial (
    ct_number text NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT trial_ct_number_check CHECK ((ct_number ~ '^\d{4}-\d{6}-\d{2}-\d{2}$'::text))
);


--
-- Name: trial_snapshot; Type: TABLE; Schema: ctis; Owner: -
--

CREATE TABLE ctis.trial_snapshot (
    id bigint NOT NULL,
    ct_number text NOT NULL,
    fetch_run_id bigint NOT NULL,
    fetched_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    content_hash bytea NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT trial_snapshot_content_hash_check CHECK ((length(content_hash) = 32))
);
ALTER TABLE ONLY ctis.trial_snapshot ALTER COLUMN payload SET COMPRESSION lz4;


--
-- Name: trial_latest; Type: VIEW; Schema: ctis; Owner: -
--

CREATE VIEW ctis.trial_latest AS
 SELECT DISTINCT ON (ct_number) id,
    ct_number,
    fetch_run_id,
    fetched_at,
    content_hash,
    payload
   FROM ctis.trial_snapshot
  ORDER BY ct_number, fetched_at DESC;


--
-- Name: trial_snapshot_id_seq; Type: SEQUENCE; Schema: ctis; Owner: -
--

ALTER TABLE ctis.trial_snapshot ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME ctis.trial_snapshot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: bruckner_document; Type: TABLE; Schema: gold; Owner: -
--

CREATE TABLE gold.bruckner_document (
    ct_number text NOT NULL,
    fetch_run_id bigint NOT NULL,
    source_file text NOT NULL,
    results_posted_on date NOT NULL,
    reviewers_agreed boolean NOT NULL,
    final_comment text,
    content_hash bytea NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT bruckner_document_content_hash_check CHECK ((length(content_hash) = 32))
);


--
-- Name: bruckner_review; Type: TABLE; Schema: gold; Owner: -
--

CREATE TABLE gold.bruckner_review (
    ct_number text NOT NULL,
    reviewer text NOT NULL,
    participants boolean,
    outcomes boolean,
    cancelled boolean,
    asked_to_check boolean,
    comment text,
    CONSTRAINT bruckner_review_reviewer_check CHECK ((reviewer = ANY (ARRAY['first'::text, 'second'::text, 'third'::text])))
);


--
-- Name: bruckner_verdict; Type: TABLE; Schema: gold; Owner: -
--

CREATE TABLE gold.bruckner_verdict (
    ct_number text NOT NULL,
    fetch_run_id bigint NOT NULL,
    source_file text NOT NULL,
    verdict text NOT NULL,
    results_document text NOT NULL,
    lay_summary boolean NOT NULL,
    days_to_results integer,
    comment text,
    content_hash bytea NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT bruckner_verdict_check CHECK (((days_to_results IS NULL) = (verdict !~~ 'reported fully%'::text))),
    CONSTRAINT bruckner_verdict_content_hash_check CHECK ((length(content_hash) = 32)),
    CONSTRAINT bruckner_verdict_results_document_check CHECK ((results_document = ANY (ARRAY['full'::text, 'incomplete'::text, 'none'::text]))),
    CONSTRAINT bruckner_verdict_verdict_check CHECK ((verdict = ANY (ARRAY['reported fully on time'::text, 'reported fully late'::text, 'not fully reported'::text, 'extended'::text, 'cancelled'::text])))
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying NOT NULL
);


--
-- Name: age_group; Type: TABLE; Schema: ref; Owner: -
--

CREATE TABLE ref.age_group (
    code smallint NOT NULL,
    label text NOT NULL
);


--
-- Name: document_type; Type: TABLE; Schema: ref; Owner: -
--

CREATE TABLE ref.document_type (
    code smallint NOT NULL,
    label text NOT NULL,
    is_result boolean DEFAULT false NOT NULL
);


--
-- Name: early_termination_reason; Type: TABLE; Schema: ref; Owner: -
--

CREATE TABLE ref.early_termination_reason (
    code smallint NOT NULL,
    label text NOT NULL
);


--
-- Name: member_state; Type: TABLE; Schema: ref; Owner: -
--

CREATE TABLE ref.member_state (
    iso2 character(2) NOT NULL,
    name text NOT NULL,
    is_eu boolean NOT NULL
);


--
-- Name: sponsor_type; Type: TABLE; Schema: ref; Owner: -
--

CREATE TABLE ref.sponsor_type (
    code smallint NOT NULL,
    label text NOT NULL
);


--
-- Name: trial_phase; Type: TABLE; Schema: ref; Owner: -
--

CREATE TABLE ref.trial_phase (
    code smallint NOT NULL,
    label text NOT NULL
);


--
-- Name: trial_region; Type: TABLE; Schema: ref; Owner: -
--

CREATE TABLE ref.trial_region (
    code smallint NOT NULL,
    label text NOT NULL
);


--
-- Name: trial_status; Type: TABLE; Schema: ref; Owner: -
--

CREATE TABLE ref.trial_status (
    code smallint NOT NULL,
    label text NOT NULL
);


--
-- Name: fetch_run fetch_run_pkey; Type: CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.fetch_run
    ADD CONSTRAINT fetch_run_pkey PRIMARY KEY (id);


--
-- Name: search_export_snapshot search_export_snapshot_pkey; Type: CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.search_export_snapshot
    ADD CONSTRAINT search_export_snapshot_pkey PRIMARY KEY (id);


--
-- Name: search_export_snapshot search_export_snapshot_row_key; Type: CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.search_export_snapshot
    ADD CONSTRAINT search_export_snapshot_row_key UNIQUE (ct_number, captured_on, source_file, content_hash);


--
-- Name: trial trial_pkey; Type: CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.trial
    ADD CONSTRAINT trial_pkey PRIMARY KEY (ct_number);


--
-- Name: trial_snapshot trial_snapshot_ct_number_fetched_at_key; Type: CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.trial_snapshot
    ADD CONSTRAINT trial_snapshot_ct_number_fetched_at_key UNIQUE (ct_number, fetched_at);


--
-- Name: trial_snapshot trial_snapshot_pkey; Type: CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.trial_snapshot
    ADD CONSTRAINT trial_snapshot_pkey PRIMARY KEY (id);


--
-- Name: bruckner_document bruckner_document_pkey; Type: CONSTRAINT; Schema: gold; Owner: -
--

ALTER TABLE ONLY gold.bruckner_document
    ADD CONSTRAINT bruckner_document_pkey PRIMARY KEY (ct_number);


--
-- Name: bruckner_review bruckner_review_pkey; Type: CONSTRAINT; Schema: gold; Owner: -
--

ALTER TABLE ONLY gold.bruckner_review
    ADD CONSTRAINT bruckner_review_pkey PRIMARY KEY (ct_number, reviewer);


--
-- Name: bruckner_verdict bruckner_verdict_pkey; Type: CONSTRAINT; Schema: gold; Owner: -
--

ALTER TABLE ONLY gold.bruckner_verdict
    ADD CONSTRAINT bruckner_verdict_pkey PRIMARY KEY (ct_number);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: age_group age_group_label_key; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.age_group
    ADD CONSTRAINT age_group_label_key UNIQUE (label);


--
-- Name: age_group age_group_pkey; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.age_group
    ADD CONSTRAINT age_group_pkey PRIMARY KEY (code);


--
-- Name: document_type document_type_label_key; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.document_type
    ADD CONSTRAINT document_type_label_key UNIQUE (label);


--
-- Name: document_type document_type_pkey; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.document_type
    ADD CONSTRAINT document_type_pkey PRIMARY KEY (code);


--
-- Name: early_termination_reason early_termination_reason_label_key; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.early_termination_reason
    ADD CONSTRAINT early_termination_reason_label_key UNIQUE (label);


--
-- Name: early_termination_reason early_termination_reason_pkey; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.early_termination_reason
    ADD CONSTRAINT early_termination_reason_pkey PRIMARY KEY (code);


--
-- Name: member_state member_state_name_key; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.member_state
    ADD CONSTRAINT member_state_name_key UNIQUE (name);


--
-- Name: member_state member_state_pkey; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.member_state
    ADD CONSTRAINT member_state_pkey PRIMARY KEY (iso2);


--
-- Name: sponsor_type sponsor_type_label_key; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.sponsor_type
    ADD CONSTRAINT sponsor_type_label_key UNIQUE (label);


--
-- Name: sponsor_type sponsor_type_pkey; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.sponsor_type
    ADD CONSTRAINT sponsor_type_pkey PRIMARY KEY (code);


--
-- Name: trial_phase trial_phase_label_key; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.trial_phase
    ADD CONSTRAINT trial_phase_label_key UNIQUE (label);


--
-- Name: trial_phase trial_phase_pkey; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.trial_phase
    ADD CONSTRAINT trial_phase_pkey PRIMARY KEY (code);


--
-- Name: trial_region trial_region_label_key; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.trial_region
    ADD CONSTRAINT trial_region_label_key UNIQUE (label);


--
-- Name: trial_region trial_region_pkey; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.trial_region
    ADD CONSTRAINT trial_region_pkey PRIMARY KEY (code);


--
-- Name: trial_status trial_status_label_key; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.trial_status
    ADD CONSTRAINT trial_status_label_key UNIQUE (label);


--
-- Name: trial_status trial_status_pkey; Type: CONSTRAINT; Schema: ref; Owner: -
--

ALTER TABLE ONLY ref.trial_status
    ADD CONSTRAINT trial_status_pkey PRIMARY KEY (code);


--
-- Name: search_export_snapshot_fetch_run_idx; Type: INDEX; Schema: ctis; Owner: -
--

CREATE INDEX search_export_snapshot_fetch_run_idx ON ctis.search_export_snapshot USING btree (fetch_run_id);


--
-- Name: trial_snapshot_fetch_run_idx; Type: INDEX; Schema: ctis; Owner: -
--

CREATE INDEX trial_snapshot_fetch_run_idx ON ctis.trial_snapshot USING btree (fetch_run_id);


--
-- Name: trial_snapshot_latest_idx; Type: INDEX; Schema: ctis; Owner: -
--

CREATE INDEX trial_snapshot_latest_idx ON ctis.trial_snapshot USING btree (ct_number, fetched_at DESC);


--
-- Name: bruckner_document_fetch_run_idx; Type: INDEX; Schema: gold; Owner: -
--

CREATE INDEX bruckner_document_fetch_run_idx ON gold.bruckner_document USING btree (fetch_run_id);


--
-- Name: bruckner_verdict_fetch_run_idx; Type: INDEX; Schema: gold; Owner: -
--

CREATE INDEX bruckner_verdict_fetch_run_idx ON gold.bruckner_verdict USING btree (fetch_run_id);


--
-- Name: search_export_snapshot search_export_snapshot_ct_number_fkey; Type: FK CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.search_export_snapshot
    ADD CONSTRAINT search_export_snapshot_ct_number_fkey FOREIGN KEY (ct_number) REFERENCES ctis.trial(ct_number);


--
-- Name: search_export_snapshot search_export_snapshot_fetch_run_id_fkey; Type: FK CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.search_export_snapshot
    ADD CONSTRAINT search_export_snapshot_fetch_run_id_fkey FOREIGN KEY (fetch_run_id) REFERENCES ctis.fetch_run(id);


--
-- Name: trial_snapshot trial_snapshot_ct_number_fkey; Type: FK CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.trial_snapshot
    ADD CONSTRAINT trial_snapshot_ct_number_fkey FOREIGN KEY (ct_number) REFERENCES ctis.trial(ct_number);


--
-- Name: trial_snapshot trial_snapshot_fetch_run_id_fkey; Type: FK CONSTRAINT; Schema: ctis; Owner: -
--

ALTER TABLE ONLY ctis.trial_snapshot
    ADD CONSTRAINT trial_snapshot_fetch_run_id_fkey FOREIGN KEY (fetch_run_id) REFERENCES ctis.fetch_run(id);


--
-- Name: bruckner_document bruckner_document_ct_number_fkey; Type: FK CONSTRAINT; Schema: gold; Owner: -
--

ALTER TABLE ONLY gold.bruckner_document
    ADD CONSTRAINT bruckner_document_ct_number_fkey FOREIGN KEY (ct_number) REFERENCES ctis.trial(ct_number);


--
-- Name: bruckner_document bruckner_document_fetch_run_id_fkey; Type: FK CONSTRAINT; Schema: gold; Owner: -
--

ALTER TABLE ONLY gold.bruckner_document
    ADD CONSTRAINT bruckner_document_fetch_run_id_fkey FOREIGN KEY (fetch_run_id) REFERENCES ctis.fetch_run(id);


--
-- Name: bruckner_review bruckner_review_ct_number_fkey; Type: FK CONSTRAINT; Schema: gold; Owner: -
--

ALTER TABLE ONLY gold.bruckner_review
    ADD CONSTRAINT bruckner_review_ct_number_fkey FOREIGN KEY (ct_number) REFERENCES gold.bruckner_document(ct_number);


--
-- Name: bruckner_verdict bruckner_verdict_ct_number_fkey; Type: FK CONSTRAINT; Schema: gold; Owner: -
--

ALTER TABLE ONLY gold.bruckner_verdict
    ADD CONSTRAINT bruckner_verdict_ct_number_fkey FOREIGN KEY (ct_number) REFERENCES ctis.trial(ct_number);


--
-- Name: bruckner_verdict bruckner_verdict_fetch_run_id_fkey; Type: FK CONSTRAINT; Schema: gold; Owner: -
--

ALTER TABLE ONLY gold.bruckner_verdict
    ADD CONSTRAINT bruckner_verdict_fetch_run_id_fkey FOREIGN KEY (fetch_run_id) REFERENCES ctis.fetch_run(id);


--
-- PostgreSQL database dump complete
--

\unrestrict dbmate


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20261030143522');
