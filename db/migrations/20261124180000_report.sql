-- migrate:up

-- What Grafana reads from Postgres, for admins (ops/grafana/): views in a schema of their own, read by a
-- role that can read nothing else (grafana_ro). The views leave out what no dashboard needs: no person's
-- name or email, no sign-in, no request or answer of a model call, no document. Queue, spend, check and
-- source numbers come from here; latency and errors from the metrics (Prometheus).
--
-- grafana_ro is created without a password: compose's grafana-role service gives it one
-- (GRAFANA_DB_PASSWORD) and Grafana's Postgres data source signs in with it. Roles belong to the whole
-- server, so another database of it (the tests') shares it.
SET LOCAL trialwatch.actor = '1';

-- Each backup of the database (compose's backup service, ops/backup.sh): an alert fires when the newest
-- is older than 26 hours
CREATE TABLE core.backup (
    id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    file  text NOT NULL,
    bytes bigint NOT NULL
);
COMMENT ON TABLE core.backup IS 'each nightly pg_dump (compose service backup), for the backup alert';

CREATE SCHEMA report;
COMMENT ON SCHEMA report IS 'what Grafana reads (role grafana_ro): queue, spend, checks, sources, backups';

CREATE VIEW report.jobs AS
SELECT j.id, j.kind, j.status, j.automatic, j.attempts, j.max_attempts, j.created_at, j.started_at,
       j.finished_at, j.run_after, j.last_error, j.trace_id,
       extract(epoch FROM j.finished_at - j.started_at) AS seconds
  FROM jobs.job j;

CREATE VIEW report.llm_calls AS
SELECT c.id, c.at, c.operation, c.model, c.served_by, c.check_run_id, c.annotation_run_id, c.job_id,
       c.input_tokens, c.output_tokens, c.cache_write_tokens, c.cache_read_tokens, c.cost_usd, c.unpriced_attempts,
       c.latency_ms, c.stop_reason, c.error IS NOT NULL AS failed, split_part(c.error, ':', 1) AS error_type
  FROM core.llm_call c;

-- each billed attempt, by the model that ran it (after a fallback, two models share a call)
CREATE VIEW report.spend AS
SELECT c.at, c.operation, a.model, a.input_tokens, a.output_tokens, a.cost_usd,
       CASE WHEN p.kind = 'process' THEN p.name ELSE 'a person' END AS made_for
  FROM core.llm_call c
  JOIN core.actor p ON p.id = c.actor_id
 CROSS JOIN LATERAL jsonb_to_recordset(c.attempts)
       AS a(model text, input_tokens bigint, output_tokens bigint, cost_usd numeric(14, 6));

CREATE VIEW report.spend_cap AS
SELECT (SELECT (value #>> '{}')::numeric FROM core.app_setting WHERE key = 'spend.monthly_cap_usd') AS cap_usd,
       (SELECT coalesce(sum(cost_usd), 0) FROM core.llm_call WHERE at >= date_trunc('month', now())) AS this_month_usd;

CREATE VIEW report.check_runs AS
SELECT r.id, r.started_at, r.finished_at, r.status, r.model, r.effort, r.prompt, r.requirements, r.trials, r.errors,
       r.job_id, (SELECT coalesce(sum(c.cost_usd), 0) FROM core.llm_call c WHERE c.check_run_id = r.id) AS cost_usd
  FROM checks.run r;

CREATE VIEW report.judgements AS
SELECT j.run_id, j.trial_id, r.finished_at AS at, r.requirements, r.model, j.label, j.error IS NOT NULL AS failed,
       (j.error IS NULL AND r.status <> 'running') AS published, (j.agreement->>'confidence')::real AS confidence,
       j.withdrawn_at IS NOT NULL AS withdrawn
  FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id;

CREATE VIEW report.ingest_runs AS
SELECT i.id, s.code AS source, i.kind, i.status, i.started_at, i.finished_at, i.counts,
       (SELECT count(*) FROM jsonb_object_keys(coalesce(i.errors, '{}'))) AS errors
  FROM core.ingest_run i JOIN core.source s ON s.id = i.source_id;

CREATE VIEW report.sources AS
SELECT s.code, s.enabled,
       (SELECT max(i.finished_at) FROM core.ingest_run i
         WHERE i.source_id = s.id AND i.kind = 'list' AND i.status = 'succeeded') AS last_sync,
       (SELECT max(i.finished_at) FROM core.ingest_run i
         WHERE i.source_id = s.id AND i.kind = 'transfer') AS last_transfer,
       (SELECT count(*) FROM core.evidence_transfer e WHERE e.source_id = s.id AND e.status = 'failed') AS failed_transfers
  FROM core.source s;

CREATE VIEW report.backups AS
SELECT at, file, bytes FROM core.backup;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        CREATE ROLE grafana_ro NOLOGIN;
    END IF;
END
$$;
GRANT USAGE ON SCHEMA report TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA report TO grafana_ro;

-- migrate:down
SET LOCAL trialwatch.actor = '1';

REVOKE ALL ON ALL TABLES IN SCHEMA report FROM grafana_ro;
REVOKE ALL ON SCHEMA report FROM grafana_ro;
DROP SCHEMA report CASCADE;
DROP TABLE core.backup;
-- grafana_ro stays: a role belongs to the whole server, and other databases of it may still use it
