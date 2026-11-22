"""A few GeoNames files in GeoNames' own formats, for the place and search tests (ADR-008), so they never
reach the network: countries, places with their alternate names, and postal codes."""

from pathlib import Path

from trialwatch.geo import load as geo_loading

COUNTRIES = [
    # ISO, ISO3, ISO-numeric, fips, Country, Capital, Area, Population, Continent, tld, CurrencyCode,
    # CurrencyName, Phone, Postal Code Format, Postal Code Regex, Languages, geonameid, neighbours, fips
    ("FR", "FRA", "250", "FR", "France", "Paris", "547030", "66987244", "EU", ".fr", "EUR", "Euro", "33", "#####",
     "", "fr-FR,frp,br,co,ca,eu,oc", "3017382", "CH,DE,BE,LU,IT,AD,MC,ES", ""),
    ("DE", "DEU", "276", "GM", "Germany", "Berlin", "357021", "82927922", "EU", ".de", "EUR", "Euro", "49", "#####",
     "", "de", "2921044", "CH,PL,NL,DK,BE,CZ,LU,FR,AT", ""),
    ("AT", "AUT", "040", "AU", "Austria", "Vienna", "83858", "8847037", "EU", ".at", "EUR", "Euro", "43", "####",
     "", "de-AT,hr,hu,sl", "2782113", "CH,DE,HU,SK,CZ,IT,SI,LI", ""),
    ("BE", "BEL", "056", "BE", "Belgium", "Brussels", "30510", "11422068", "EU", ".be", "EUR", "Euro", "32", "####",
     "", "nl-BE,fr-BE,de-BE", "2802361", "DE,NL,LU,FR", ""),
    ("ES", "ESP", "724", "SP", "Spain", "Madrid", "504782", "46723749", "EU", ".es", "EUR", "Euro", "34", "#####",
     "", "es-ES,ca,gl,eu,oc", "2510769", "AD,FR,GI,PT,MA", ""),
    ("GB", "GBR", "826", "UK", "United Kingdom", "London", "244820", "66488991", "EU", ".uk", "GBP", "Pound", "44",
     "", "", "en-GB,cy-GB,gd", "2635167", "IE", ""),
]
PLACES = [
    # geonameid, name, asciiname, alternatenames, latitude, longitude, class, code, country, cc2, admin1-4,
    # population, elevation, dem, timezone, modified
    (2988507, "Paris", "Paris", "Parigi,Paryż,Parijs,Lutetia", 48.85341, 2.3488, "FR", 2138551),
    (2968705, "Villejuif", "Villejuif", "", 48.7922, 2.3634, "FR", 55208),
    (2996944, "Lyon", "Lyon", "Lyons,Lione,Lugdunum", 45.74846, 4.84671, "FR", 522969),
    (2990440, "Nice", "Nice", "Nizza", 43.70313, 7.26608, "FR", 342669),
    (2797656, "Gent", "Gent", "Ghent,Gand,Gante", 51.05, 3.71667, "BE", 231493),
    (2761369, "Vienna", "Vienna", "Wien,Vienne,Viena,Wiedeń,Bécs", 48.20849, 16.37208, "AT", 1691468),
    (2873759, "Marburg", "Marburg", "Marburg an der Lahn", 50.80904, 8.77069, "DE", 72031),
    (2925533, "Frankfurt am Main", "Frankfurt am Main", "Frankfurt,Francfort-sur-le-Main", 50.11552, 8.68417, "DE", 650000),
    (2925535, "Frankfurt (Oder)", "Frankfurt (Oder)", "Frankfurt an der Oder", 52.34714, 14.55062, "DE", 60625),
    (2867714, "Munich", "Munich", "München,Monaco di Baviera,Monachium", 48.13743, 11.57549, "DE", 1260391),
    (3117735, "Madrid", "Madrid", "", 40.4165, -3.70256, "ES", 3255944),
    (2643066, "Lyme Regis", "Lyme Regis", "Lyme", 50.72552, -2.93716, "GB", 4712),
]
POSTCODES = {
    "FR": [("FR", "75013", "Paris 13", 48.8322, 2.3561), ("FR", "94800", "Villejuif", 48.7922, 2.3634),
           ("FR", "69003", "Lyon 03", 45.7597, 4.8422), ("FR", "06000", "Nice", 43.7031, 7.2661)],
    "BE": [("BE", "9000", "Gent", 51.05, 3.7167)],
    "AT": [("AT", "1090", "Wien, Alsergrund", 48.2225, 16.3567)],
    "DE": [("DE", "35037", "Marburg", 50.8021, 8.7667)],
    "ES": [("ES", "28046", "Madrid", 40.4688, -3.6893)],
}
LOADED = ["FR", "DE", "AT", "BE", "ES"]


def write_files(folder: Path) -> Path:
    lines = ["# a comment line, as GeoNames starts its file with"]
    for country in COUNTRIES:
        lines.append("\t".join(country))
    (folder / "countryInfo.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    lines = []
    for geonameid, name, ascii_name, alternates, latitude, longitude, country, population in PLACES:
        columns = [str(geonameid), name, ascii_name, alternates, str(latitude), str(longitude), "P", "PPL", country, "",
                   "", "", "", "", str(population), "", "", "Europe/Paris", "2026-01-01"]
        lines.append("\t".join(columns))
    (folder / "cities1000.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for country, rows in POSTCODES.items():
        lines = []
        for code, postcode, place, latitude, longitude in rows:
            lines.append("\t".join([code, postcode, place, "", "", "", "", "", "", str(latitude), str(longitude), "4"]))
        (folder / f"{country}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def load_places(conn, folder: Path):
    """GeoNames loaded from the files above, as `trialwatch geo load --from` would."""
    return geo_loading.load(conn, LOADED, write_files(folder))
