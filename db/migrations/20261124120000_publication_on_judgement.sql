-- migrate:up

-- Publication is the judgement's own (ADR-007, everything is public): every judgement without an error
-- of a finished run is public, so there is no separate publication to record. What one recorded is now
-- the judgement's: how judgements of its kind agreed with gold labels when its run finished
-- (`agreement`, checks/publishing.py), and its withdrawal by staff, with the reason shown to everyone (a
-- withdrawn judgement stays public, marked). A withdrawal is audited; the judgement's other columns are
-- written once, with its run.
SET LOCAL trialwatch.actor = '1';

ALTER TABLE checks.judgement
    ADD COLUMN agreement jsonb,
    ADD COLUMN withdrawn_at timestamptz,
    ADD COLUMN withdrawn_by bigint REFERENCES core.actor (id),
    ADD COLUMN withdrawn_reason text,
    ADD CONSTRAINT judgement_withdrawn_check CHECK ((withdrawn_at IS NULL) = (withdrawn_by IS NULL)
        AND (withdrawn_at IS NULL) = (withdrawn_reason IS NULL));
COMMENT ON COLUMN checks.judgement.agreement IS
    '{"confidence": share or null, "basis": in words}: how judgements of its kind agreed with gold labels when its run finished';
COMMENT ON COLUMN checks.judgement.withdrawn_reason IS 'why staff withdrew it: shown to the public, with the judgement';

-- what the publications recorded, onto their judgements (the newest publication of each)
UPDATE checks.judgement j
   SET agreement = jsonb_build_object('confidence', p.confidence, 'basis', p.basis)
  FROM (SELECT DISTINCT ON (run_id, trial_id) * FROM checks.publication ORDER BY run_id, trial_id, id DESC) p
 WHERE p.run_id = j.run_id AND p.trial_id = j.trial_id AND p.basis IS NOT NULL;
UPDATE checks.judgement j
   SET withdrawn_at = p.withdrawn_at, withdrawn_by = p.withdrawn_by, withdrawn_reason = coalesce(p.note, 'withdrawn')
  FROM (SELECT DISTINCT ON (run_id, trial_id) * FROM checks.publication ORDER BY run_id, trial_id, id DESC) p
 WHERE p.run_id = j.run_id AND p.trial_id = j.trial_id AND p.withdrawn_at IS NOT NULL;

CREATE TRIGGER judgement_withdrawn_audit AFTER UPDATE OF withdrawn_at, withdrawn_by, withdrawn_reason
    ON checks.judgement FOR EACH ROW EXECUTE FUNCTION core.audit('run_id,trial_id', 'trace,parts,agreement');

-- the one view of the judgement each trial shows, for staff and the public alike: it is public once
-- its run has finished (published_at), withdrawn or not
CREATE OR REPLACE VIEW checks.judgement_current AS
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
                     AND c.fetched_at > r.started_at)) AS outdated,
       r.finished_at AS published_at, j.agreement, j.withdrawn_at, j.withdrawn_reason
FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
WHERE j.error IS NULL AND r.status <> 'running'
ORDER BY j.trial_id, r.requirements, j.run_id DESC;

DROP VIEW checks.judgement_published;
DROP TABLE checks.publication;
DROP FUNCTION checks.publishable();


-- migrate:down
SET LOCAL trialwatch.actor = '1';
-- the view as it was; the publications themselves are not rebuilt (restore a backup to go back fully)
DROP VIEW checks.judgement_current;
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
DROP TRIGGER judgement_withdrawn_audit ON checks.judgement;
ALTER TABLE checks.judgement DROP CONSTRAINT judgement_withdrawn_check, DROP COLUMN agreement,
    DROP COLUMN withdrawn_at, DROP COLUMN withdrawn_by, DROP COLUMN withdrawn_reason;
