-- migrate:up

-- What TrialWatch spends on paid APIs. Every billed model attempt is one core.api_call row with its
-- tokens, the price list it was charged at and its cost in US dollars: the one source of every money
-- total (per call, trial, run, job, month). Prices are the current list in
-- src/trialwatch/costs/prices.json, copied into each row, so a later price change never rewrites
-- what was spent. A model missing from the list is recorded unpriced (cost_usd NULL), never as $0.
SET LOCAL trialwatch.actor = '1';

CREATE TABLE core.api_call (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- who the call was made for: the person who started the check, or trialwatch
    actor_id           bigint NOT NULL REFERENCES core.actor,
    provider           text NOT NULL DEFAULT 'anthropic',
    -- the model that ran this attempt (after a fallback, the fallback model)
    model              text NOT NULL,
    -- what the call was for, e.g. check
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
    request_id         text
);
CREATE INDEX api_call_at_idx ON core.api_call (at);
CREATE INDEX api_call_run_idx ON core.api_call (check_run_id, trial_id);
COMMENT ON TABLE core.api_call IS 'every billed call to a paid API, with its tokens and cost: the source of all spend totals';

-- Cost next to the tokens a judgement and a run already keep (sums of their core.api_call rows)
ALTER TABLE checks.judgement ADD COLUMN cache_write_tokens integer, ADD COLUMN cache_read_tokens integer,
    ADD COLUMN cost_usd numeric(14, 6);
ALTER TABLE checks.run ADD COLUMN cache_write_tokens bigint NOT NULL DEFAULT 0,
    ADD COLUMN cache_read_tokens bigint NOT NULL DEFAULT 0, ADD COLUMN cost_usd numeric(14, 6) NOT NULL DEFAULT 0;

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


-- migrate:down
SET LOCAL trialwatch.actor = '1';

DROP TABLE core.app_setting;
ALTER TABLE checks.run DROP COLUMN cache_write_tokens, DROP COLUMN cache_read_tokens, DROP COLUMN cost_usd;
ALTER TABLE checks.judgement DROP COLUMN cache_write_tokens, DROP COLUMN cache_read_tokens, DROP COLUMN cost_usd;
DROP TABLE core.api_call;
