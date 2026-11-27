-- migrate:up

-- What OCR made of a page whose text layer has too few letters (trials/ocr.py): whether the page is a
-- picture (a scan, a results table saved as an image, a chart or text drawn as shapes), and, for a picture, the text read from
-- it with the engine that read it (NULL: not read, or fewer letters than the text layer has). A page that
-- is no picture has no other text than its own (a blank, cover, contents or signature page), so a check counts
-- it as read. core.page keeps the text layer as it was extracted, never changed: quotes already verified
-- against it stay where they were. Written once per page, like core.page.
CREATE TABLE core.page_ocr (
    file_sha256 bytea NOT NULL,
    page_no     integer NOT NULL,
    is_picture  boolean NOT NULL,
    text        text,
    -- e.g. "tesseract 5.3.0 (pypdfium2 5.15.2, 200 dpi)"
    extractor   text,
    tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED,
    letters     integer GENERATED ALWAYS AS (length(regexp_replace(coalesce(text, ''), '[^[:alpha:]]', '', 'g'))) STORED,
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (file_sha256, page_no),
    FOREIGN KEY (file_sha256, page_no) REFERENCES core.page,
    CHECK ((text IS NULL) = (extractor IS NULL)),
    CHECK (text IS NULL OR is_picture)
);
CREATE TRIGGER page_ocr_never_changes BEFORE UPDATE OR DELETE ON core.page_ocr
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();

-- Each page's text as checks read it: the text read from its picture where OCR read one, else its text
-- layer; with whether OCR found it a picture (NULL: not looked at)
CREATE VIEW core.page_text AS
SELECT p.file_sha256, p.page_no,
       coalesce(o.text, p.text) AS text,
       coalesce(o.extractor, p.extractor) AS extractor,
       CASE WHEN o.text IS NOT NULL THEN o.tsv ELSE p.tsv END AS tsv,
       CASE WHEN o.text IS NOT NULL THEN o.letters ELSE p.letters END AS letters,
       o.is_picture
FROM core.page p
LEFT JOIN core.page_ocr o ON o.file_sha256 = p.file_sha256 AND o.page_no = p.page_no;

-- When OCR looked at this file's pages with too few letters (NULL: not yet, or not to be: too many pages,
-- config.ocr_max_pages())
ALTER TABLE core.file ADD COLUMN ocr_at timestamptz;

-- A judgement is outdated, too, when OCR has read a picture's text in a file it read (the refresh checks
-- such a trial again, as one whose documents changed)
CREATE OR REPLACE VIEW checks.judgement_current AS
SELECT DISTINCT ON (j.trial_id, r.requirements)
       j.run_id, j.trial_id, j.label, j.parts, j.trial_record_id, j.document_versions, j.served_by, j.duration_ms,
       r.model, r.prompt, r.requirements, r.started_at,
       (EXISTS (SELECT FROM core.document_version v
                JOIN core.trial_document l ON l.document_id = v.document_id AND l.trial_id = j.trial_id
                WHERE v.id = ANY (j.document_versions) AND (v.replaced_at IS NOT NULL OR l.removed_at IS NOT NULL))
        OR EXISTS (SELECT FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
                   WHERE l.trial_id = j.trial_id AND l.removed_at IS NULL
                     AND NOT (d.version_id = ANY (j.document_versions))
                     AND d.kind IN (SELECT jsonb_array_elements_text(r.requirements_text::jsonb -> 'document_kinds')))
        OR EXISTS (SELECT FROM core.trial_record_current c
                   WHERE c.trial_id = j.trial_id AND c.id IS DISTINCT FROM j.trial_record_id
                     AND c.fetched_at > r.started_at)
        -- OCR read a picture's text in one of its files after the check began: it read less than there is
        OR EXISTS (SELECT FROM core.document_version v JOIN core.page_ocr o ON o.file_sha256 = v.file_sha256
                   WHERE v.id = ANY (j.document_versions) AND o.text IS NOT NULL AND o.created_at > r.started_at)) AS outdated,
       r.finished_at AS published_at, j.agreement, j.withdrawn_at, j.withdrawn_reason
FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
WHERE j.error IS NULL AND r.status <> 'running'
ORDER BY j.trial_id, r.requirements, j.run_id DESC;

-- migrate:down

CREATE OR REPLACE VIEW checks.judgement_current AS
SELECT DISTINCT ON (j.trial_id, r.requirements)
       j.run_id, j.trial_id, j.label, j.parts, j.trial_record_id, j.document_versions, j.served_by, j.duration_ms,
       r.model, r.prompt, r.requirements, r.started_at,
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

ALTER TABLE core.file DROP COLUMN ocr_at;
DROP VIEW core.page_text;
DROP TABLE core.page_ocr;
