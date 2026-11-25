-- migrate:up

-- Why a stored version's file may be incomplete (a PDF without its end-of-file marker, kept after a second
-- download looked the same), or NULL. A transfer downloads a suspect version again, even when the source
-- lists the same version, instead of trusting it as held.
ALTER TABLE core.document_version ADD COLUMN suspect text;

-- migrate:down

ALTER TABLE core.document_version DROP COLUMN suspect;
