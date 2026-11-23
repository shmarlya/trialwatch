-- migrate:up

-- One table for every load of reference data, GeoNames (`trialwatch geo load`) and MeSH (`trialwatch
-- vocab load`), instead of one per schema: what it was asked, each file it read with its address and
-- sha256, the rows it holds, and the licence. The reference data itself is replaced as a whole by each
-- load and not audited row by row; the load is.
SET LOCAL trialwatch.actor = '1';

CREATE TABLE core.reference_load (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL CHECK (name IN ('geonames', 'mesh')),
    -- the release read, when the files name one (MeSH's year)
    version text,
    -- what it was asked to load, e.g. {"countries": ["AT", "BE", ...]}
    params jsonb NOT NULL DEFAULT '{}',
    -- each file read: {"name", "from" (its address, or a local path), "sha256"}
    files jsonb NOT NULL DEFAULT '[]',
    -- the rows it holds, e.g. {"places": 1234, "postcodes": 5678} or {"descriptors": 1234, "terms": 5678}
    counts jsonb NOT NULL DEFAULT '{}',
    license text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    created_by bigint NOT NULL REFERENCES core.actor (id)
);
COMMENT ON TABLE core.reference_load IS 'each load of reference data: GeoNames (geo load), MeSH (vocab load)';
CREATE TRIGGER reference_load_audit AFTER INSERT OR DELETE OR UPDATE ON core.reference_load
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');

INSERT INTO core.reference_load (name, version, params, files, counts, license, started_at, finished_at, created_by)
SELECT name, version, params, files, counts, license, started_at, finished_at, created_by FROM (
    SELECT 'geonames' AS name, NULL AS version, jsonb_build_object('countries', to_jsonb(countries)) AS params,
           files, jsonb_strip_nulls(jsonb_build_object('places', places, 'postcodes', postcodes)) AS counts,
           license, started_at, finished_at, created_by
      FROM geo.load_run
    UNION ALL
    SELECT vocabulary, version, '{}'::jsonb,
           jsonb_build_array(jsonb_build_object('name', regexp_replace(file, '^.*/', ''), 'from', file, 'sha256', sha256)),
           jsonb_strip_nulls(jsonb_build_object('descriptors', descriptors, 'terms', terms)),
           license, started_at, finished_at, created_by
      FROM vocab.load_run
) loads
ORDER BY started_at;

DROP TABLE geo.load_run;
DROP TABLE vocab.load_run;

-- migrate:down
SET LOCAL trialwatch.actor = '1';

CREATE TABLE geo.load_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    files jsonb NOT NULL DEFAULT '[]',
    countries text[] NOT NULL,
    places integer,
    postcodes integer,
    license text NOT NULL DEFAULT 'GeoNames, CC BY 4.0 (https://www.geonames.org)',
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    created_by bigint NOT NULL REFERENCES core.actor (id)
);
COMMENT ON TABLE geo.load_run IS 'each load of GeoNames reference data (trialwatch geo load)';
CREATE TRIGGER load_run_audit AFTER INSERT OR DELETE OR UPDATE ON geo.load_run
    FOR EACH ROW EXECUTE FUNCTION core.audit('id');
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
INSERT INTO geo.load_run (files, countries, places, postcodes, license, started_at, finished_at, created_by)
SELECT files, ARRAY(SELECT jsonb_array_elements_text(params->'countries')), (counts->>'places')::int,
       (counts->>'postcodes')::int, license, started_at, finished_at, created_by
  FROM core.reference_load WHERE name = 'geonames' ORDER BY id;
INSERT INTO vocab.load_run (vocabulary, version, file, sha256, descriptors, terms, license, started_at, finished_at,
                            created_by)
SELECT name, coalesce(version, 'unknown'), files->0->>'from', files->0->>'sha256', (counts->>'descriptors')::int,
       (counts->>'terms')::int, license, started_at, finished_at, created_by
  FROM core.reference_load WHERE name = 'mesh' ORDER BY id;
DROP TABLE core.reference_load;
