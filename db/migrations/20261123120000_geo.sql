-- migrate:up

-- Places (ADR-008, patient search): "diabetes Paris" means trials recruiting at a site within some
-- kilometres of Paris, not trials whose site's city is spelled "Paris" (Gustave Roussy is in
-- Villejuif). So every trial site gets a point (latitude, longitude) and a query's place too, both from
-- GeoNames (CC BY 4.0, credited on the site), loaded offline by `trialwatch geo load`: no geocoding
-- service is called per site or per search.
--
-- geo.country, geo.country_name, geo.place, geo.place_name and geo.postcode are reference data,
-- replaced as a whole by each load: they are not audited row by row, the load run is (geo.load_run,
-- with each file's address and sha256), as core.page and core.source_listing are not audited either.
-- geo.address_point is a derived cache: where each distinct site address (country, postcode, city)
-- is, found by `trialwatch geo geocode` and after each transfer; `geo geocode --again` redoes it.
-- core.trial_location gets the keys of its address, so a site finds its point by a plain join.
SET LOCAL trialwatch.actor = '1';

CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE SCHEMA geo;

-- A name for comparing: lower case, no accents, anything but letters and digits as single spaces
-- ("Saint-Étienne" -> "saint etienne", "München" -> "munchen"). IMMUTABLE so indexes and generated
-- columns can use it: the unaccent dictionary it names is fixed.
CREATE FUNCTION geo.name_key(name text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN btrim(regexp_replace(lower(public.unaccent('public.unaccent'::regdictionary, coalesce(name, ''))),
                                '[^[:alnum:]]+', ' ', 'g'));

-- A postcode for comparing: upper case letters and digits only ("1012 AB" -> "1012AB")
CREATE FUNCTION geo.postcode_key(postcode text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN regexp_replace(upper(coalesce(postcode, '')), '[^[:alnum:]]', '', 'g');

-- Great-circle distance in kilometres (haversine, Earth radius 6371 km)
CREATE FUNCTION geo.distance_km(lat1 double precision, lon1 double precision,
                                lat2 double precision, lon2 double precision) RETURNS double precision
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN 2 * 6371 * asin(least(1, sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2))));

CREATE TABLE geo.load_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- each file read: its address (or local path), sha256 and rows taken
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

CREATE TABLE geo.country (
    iso2 char(2) PRIMARY KEY,
    iso3 char(3),
    name text NOT NULL,
    -- the country's languages, most spoken first (GeoNames countryInfo, e.g. {fr-BE,nl-BE,de-BE})
    languages text[] NOT NULL DEFAULT '{}',
    -- a member of the EU/EEA: where CTIS trials have sites
    eea boolean NOT NULL DEFAULT false
);
COMMENT ON TABLE geo.country IS 'countries (GeoNames countryInfo), reference data replaced by each geo load';

CREATE TABLE geo.country_name (
    name_key text NOT NULL,
    iso2 char(2) NOT NULL REFERENCES geo.country (iso2),
    name text NOT NULL,
    PRIMARY KEY (name_key, iso2)
);
COMMENT ON TABLE geo.country_name IS
    'every name we know a country by (English, its own languages, CTIS''s spellings), keyed by geo.name_key';

CREATE TABLE geo.place (
    geonameid bigint PRIMARY KEY,
    name text NOT NULL,
    country_code char(2) NOT NULL,
    population bigint NOT NULL DEFAULT 0,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL
);
COMMENT ON TABLE geo.place IS 'populated places of the EU/EEA with 1000+ people (GeoNames cities1000)';
CREATE INDEX place_country_idx ON geo.place (country_code);

CREATE TABLE geo.place_name (
    geonameid bigint NOT NULL REFERENCES geo.place (geonameid) ON DELETE CASCADE,
    name_key text NOT NULL,
    PRIMARY KEY (name_key, geonameid)
);
COMMENT ON TABLE geo.place_name IS
    'every name of a place (its own, ASCII, and alternate names in other languages: Wien, Vienna, Vienne)';
CREATE INDEX place_name_trgm_idx ON geo.place_name USING gin (name_key gin_trgm_ops);

CREATE TABLE geo.postcode (
    country_code char(2) NOT NULL,
    postcode_key text NOT NULL,
    postcode text NOT NULL,
    place_name text NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL
);
COMMENT ON TABLE geo.postcode IS 'postal codes with a point (GeoNames postal code files), EU/EEA';
CREATE INDEX postcode_idx ON geo.postcode (country_code, postcode_key);

CREATE TABLE geo.address_point (
    country_key text NOT NULL,
    postcode_key text NOT NULL,
    city_key text NOT NULL,
    iso2 char(2),
    latitude double precision,
    longitude double precision,
    -- how it was found: postcode | city | city-fuzzy | none (not found: never in a distance search)
    precision text NOT NULL CHECK (precision IN ('postcode', 'city', 'city-fuzzy', 'none')),
    -- the place or postcode it matched, in words
    matched text,
    geocoded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (country_key, postcode_key, city_key),
    CHECK ((precision = 'none') = (latitude IS NULL))
);
COMMENT ON TABLE geo.address_point IS
    'where each distinct site address is (derived from geo.postcode and geo.place; trialwatch geo geocode)';
CREATE INDEX address_point_latitude_idx ON geo.address_point (latitude) WHERE latitude IS NOT NULL;

ALTER TABLE core.trial_location
    ADD COLUMN country_key text GENERATED ALWAYS AS (geo.name_key(country)) STORED,
    ADD COLUMN postcode_key text GENERATED ALWAYS AS (geo.postcode_key(postcode)) STORED,
    ADD COLUMN city_key text GENERATED ALWAYS AS (geo.name_key(city)) STORED;
CREATE INDEX trial_location_address_idx ON core.trial_location (country_key, postcode_key, city_key);


-- migrate:down
SET LOCAL trialwatch.actor = '1';
DROP INDEX core.trial_location_address_idx;
ALTER TABLE core.trial_location DROP COLUMN country_key, DROP COLUMN postcode_key, DROP COLUMN city_key;
DROP SCHEMA geo CASCADE;
