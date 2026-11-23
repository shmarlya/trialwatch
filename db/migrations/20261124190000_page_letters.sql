-- migrate:up

-- How many letters each page's text holds, counted once when the page is saved. A page counts as readable
-- with at least 200 (checks/verify.py MIN_LETTERS). Counting them on every view meant scanning every page's
-- text: the staff home page took 10 seconds.
ALTER TABLE core.page
    ADD COLUMN letters integer GENERATED ALWAYS AS (length(regexp_replace(text, '[^[:alpha:]]', '', 'g'))) STORED;

-- migrate:down

ALTER TABLE core.page DROP COLUMN letters;
