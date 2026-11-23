-- migrate:up

-- A judgement's labels are the requirement set's (checks/requirement_sets/*.json, "labels"): each set says
-- which labels it has and what each means, so the database no longer lists them.
SET LOCAL trialwatch.actor = '1';
ALTER TABLE checks.judgement DROP CONSTRAINT judgement_label_check;
COMMENT ON COLUMN checks.judgement.label IS 'one of its requirement set''s labels; NULL only with an error';


-- migrate:down
SET LOCAL trialwatch.actor = '1';
COMMENT ON COLUMN checks.judgement.label IS NULL;
ALTER TABLE checks.judgement ADD CONSTRAINT judgement_label_check CHECK (label IN ('full', 'not full', 'undecided'));
