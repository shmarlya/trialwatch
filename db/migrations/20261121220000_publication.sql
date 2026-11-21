-- migrate:up

-- What the public sees (ADR-007): a judgement is public only once a moderator or admin published it,
-- or a run published it because judgements of its kind agree with gold labels often enough.
-- A publication is withdrawn, never deleted, so the record of what was public and when stays. The
-- public sees, per trial and requirement set, the newest judgement whose publication is not withdrawn.
SET LOCAL trialwatch.actor = '1';

CREATE TABLE checks.publication (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id bigint NOT NULL,
    trial_id bigint NOT NULL,
    published_by bigint NOT NULL REFERENCES core.actor (id),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    withdrawn_by bigint REFERENCES core.actor (id),
    withdrawn_at timestamptz,
    -- why it was withdrawn (or anything the publisher wants to note)
    note text,
    -- NULL when a person reviewed it; else how often judgements of its kind (setup, label, quotes,
    -- widening, unanswered questions) agreed with gold labels, the share that let a run publish it
    -- without a person (checks/publishing.py)
    confidence real CHECK (confidence >= 0 AND confidence <= 1),
    basis text,
    CHECK ((confidence IS NULL) = (basis IS NULL)),
    FOREIGN KEY (run_id, trial_id) REFERENCES checks.judgement (run_id, trial_id),
    CHECK ((withdrawn_at IS NULL) = (withdrawn_by IS NULL))
);
COMMENT ON TABLE checks.publication IS 'judgements staff made public (ADR-007); withdrawn ones are kept';
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

-- The judgement the public sees per trial and requirement set: the newest published, not withdrawn,
-- with whether its inputs changed since (as checks.judgement_current says for every judgement)
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
                       AND c.fetched_at > r.started_at))) AS outdated
FROM checks.publication p
JOIN checks.judgement j ON j.run_id = p.run_id AND j.trial_id = p.trial_id
JOIN checks.run r ON r.id = j.run_id
WHERE p.withdrawn_at IS NULL
ORDER BY j.trial_id, r.requirements, j.run_id DESC;


-- migrate:down
SET LOCAL trialwatch.actor = '1';
DROP VIEW checks.judgement_published;
DROP TABLE checks.publication;
DROP FUNCTION checks.publishable();
