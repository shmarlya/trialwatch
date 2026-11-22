-- migrate:up

-- Everything is public (ADR-007, revised 2026-11-22): every judgement without an error is published by
-- its run as the run finishes, whatever its label and however often judgements of its kind agreed with
-- the gold datasets; that agreement is recorded with it (confidence, when measured, and basis, always).
-- No person publishes. Staff may withdraw a judgement with a reason; a withdrawn one stays public,
-- marked withdrawn with its reason, so nothing disappears silently.
SET LOCAL trialwatch.actor = '1';

-- basis is now kept for every publication, also when too few gold items measured a confidence
ALTER TABLE checks.publication DROP CONSTRAINT publication_check;
COMMENT ON COLUMN checks.publication.confidence IS
    'share of gold items judgements of its kind agreed with, once enough were compared (checks/publishing.py); NULL: not measured';
COMMENT ON COLUMN checks.publication.basis IS 'how its kind compared with gold labels when it was published';
COMMENT ON COLUMN checks.publication.note IS 'why staff withdrew it: shown to the public';
COMMENT ON TABLE checks.publication IS 'every finished judgement without an error, published by its run (ADR-007); withdrawn ones stay, marked';

-- The judgement the public sees per trial and requirement set: the newest published one, withdrawn or
-- not (a withdrawn one says so, with its reason), with whether its inputs changed since
DROP VIEW checks.judgement_published;
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
                       AND c.fetched_at > r.started_at))) AS outdated,
       p.withdrawn_at, p.note AS withdrawn_note
FROM checks.publication p
JOIN checks.judgement j ON j.run_id = p.run_id AND j.trial_id = p.trial_id
JOIN checks.run r ON r.id = j.run_id
ORDER BY j.trial_id, r.requirements, j.run_id DESC, p.id DESC;

-- What was judged before this and never published goes public now, as TrialWatch itself
INSERT INTO checks.publication (run_id, trial_id, published_by, basis)
SELECT j.run_id, j.trial_id, 1, 'published when every judgement became public (2026-11-22); gold agreement not recorded then'
FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
WHERE j.error IS NULL AND r.status <> 'running'
  AND NOT EXISTS (SELECT FROM checks.publication p WHERE p.run_id = j.run_id AND p.trial_id = j.trial_id);


-- migrate:down
SET LOCAL trialwatch.actor = '1';

DROP VIEW checks.judgement_published;
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
-- the publications this made stay (they can't be told from others); the old rule is back for new ones
UPDATE checks.publication SET basis = NULL WHERE confidence IS NULL;
ALTER TABLE checks.publication ADD CONSTRAINT publication_check CHECK ((confidence IS NULL) = (basis IS NULL));
