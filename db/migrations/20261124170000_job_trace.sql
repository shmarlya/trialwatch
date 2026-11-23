-- migrate:up

-- The trace of a job's latest attempt (OpenTelemetry, in Grafana's Tempo): its page links to it, so an
-- admin sees every query, model call and log line of the attempt, in order.
SET LOCAL trialwatch.actor = '1';

ALTER TABLE jobs.job ADD COLUMN trace_id text;
COMMENT ON COLUMN jobs.job.trace_id IS 'the trace of its latest attempt (32 hex digits), when tracing is on';

-- migrate:down
SET LOCAL trialwatch.actor = '1';

ALTER TABLE jobs.job DROP COLUMN trace_id;
