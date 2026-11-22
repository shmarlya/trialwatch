-- migrate:up

-- Medical concepts (ADR-008, patient search): MeSH, the US National Library of Medicine's vocabulary
-- (free to use, credited on the site), so a search for "diabetes" finds a trial filed as "Type 2
-- Diabetes Mellitus", "T2DM" or "NIDDM", however its sponsor wrote it. Only diseases (tree C) and
-- mental disorders (F03): what a trial's condition is. Each descriptor keeps its entry terms (synonyms,
-- many of them lay words: "Heart Attack" for Myocardial Infarction) and its tree numbers, the
-- hierarchy that makes "Diabetes Mellitus" cover type 1, type 2 and gestational diabetes.
--
-- Reference data replaced as a whole by `trialwatch vocab load`: not audited row by row, the load run
-- is (vocab.load_run, with the file's address and sha256). MedDRA, which CTIS codes conditions in, is
-- licensed: only the MedDRA terms CTIS publishes per trial are kept (in each trial's profile).
SET LOCAL trialwatch.actor = '1';

CREATE SCHEMA vocab;

-- The words of a name in alphabetical order, each once: "Diabetes Mellitus, Type II" and "Type II
-- diabetes mellitus" are the same words
CREATE FUNCTION vocab.words_key(name text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN array_to_string(ARRAY(SELECT DISTINCT word FROM unnest(string_to_array(geo.name_key(name), ' ')) AS word
                                 WHERE word <> '' ORDER BY word), ' ');

CREATE TABLE vocab.load_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vocabulary text NOT NULL,
    version text NOT NULL,
    file text NOT NULL,
    sha256 text NOT NULL,
    descriptors integer,
    terms integer,
    license text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    created_by bigint NOT NULL REFERENCES core.actor (id)
);
COMMENT ON TABLE vocab.load_run IS 'each load of a vocabulary (trialwatch vocab load)';
CREATE TRIGGER load_run_audit AFTER INSERT OR DELETE OR UPDATE ON vocab.load_run
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');

CREATE TABLE vocab.mesh_descriptor (
    ui text PRIMARY KEY CHECK (ui ~ '^D[0-9]+$'),
    name text NOT NULL,
    tree_numbers text[] NOT NULL
);
COMMENT ON TABLE vocab.mesh_descriptor IS 'MeSH descriptors of diseases (C) and mental disorders (F03)';

CREATE TABLE vocab.mesh_term (
    name_key text NOT NULL,
    ui text NOT NULL REFERENCES vocab.mesh_descriptor (ui) ON DELETE CASCADE,
    term text NOT NULL,
    words_key text NOT NULL,
    PRIMARY KEY (name_key, ui)
);
COMMENT ON TABLE vocab.mesh_term IS 'every entry term of a MeSH descriptor (its name and synonyms), keyed by geo.name_key';
CREATE INDEX mesh_term_words_idx ON vocab.mesh_term (words_key);
CREATE INDEX mesh_term_trgm_idx ON vocab.mesh_term USING gin (name_key gin_trgm_ops);

CREATE TABLE vocab.mesh_tree (
    tree_number text PRIMARY KEY,
    ui text NOT NULL REFERENCES vocab.mesh_descriptor (ui) ON DELETE CASCADE
);
COMMENT ON TABLE vocab.mesh_tree IS 'where each MeSH descriptor sits in the hierarchy (C18.452.394.750 is under C18.452.394)';
CREATE INDEX mesh_tree_ui_idx ON vocab.mesh_tree (ui);


-- migrate:down
SET LOCAL trialwatch.actor = '1';
DROP SCHEMA vocab CASCADE;
