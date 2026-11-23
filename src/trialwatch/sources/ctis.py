"""CTIS adapter (ADR-003, ADR-005): the EU Clinical Trials Information System as a registered source.

Its list is CTIS's public search (the one on euclinicaltrials.eu), read page by page in EU CT number
order with the criteria of the source's config; each listed trial is matched by its EU CT number.

A trial's record (/retrieve) is read into a SourceTrial: its identifiers (EU CT number, NCT, WHO UTN,
EudraCT), the registered information checks ask about (`registered`), its sponsors, where it runs, its
profile for patient search, and its documents with the day CTIS says their results were submitted.
trialwatch.trials.ingest saves it, downloading the listed documents of the configured kinds (by default
the result documents). This module never writes to the database.

CTIS serves only the current file of each document, so each version is downloaded once and kept.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

from trialwatch.db import core
from trialwatch.db.core import Identifier, Listed
from trialwatch.hashing import json_hash, storable
from trialwatch.sources import profile
from trialwatch.sources.ctis_client import CT_NUMBER, SEARCH_LIMIT, CtisClient
from trialwatch.sources.registry import Adapter, DocumentRef, DownloadedFile, ListingPage, SourceTrial

SOURCE = "ctis"
# CTIS document types (its documents[].documentType) in our own kinds (core.document_kind); any other
# type is "other"
DOCUMENT_KINDS = {
    103: "results-summary",  # Summary of results (for publication)
    46: "lay-summary",  # Laypersons summary of results (for publication)
    45: "study-report",  # Clinical study report (for publication)
    104: "protocol",  # Protocol (for publication)
    7: "protocol",  # Synopsis of the protocol (for publication)
    14: "other",  # Recruitment arrangements
    15: "other",  # Subject information and informed consent form
    19: "other",  # Summary of Product Characteristics
}
# What a transfer brings by default: the kinds the evidence check reads that CTIS has. Lay summaries
# (often one per language) are only downloaded when a source's config asks for them.
DEFAULT_EVIDENCE_KINDS = ("results-summary", "study-report")
# Secondary identifiers in a record's secondaryIdentifyingNumbers, by their key there
SECONDARY_SCHEMES = {"nctNumber": "nct", "whoUniversalTrialNumber": "who-utn", "isrctnNumber": "isrctn"}
# The parts of a record listing results submissions; a document's associatedEntityId names one of them
RESULTS_LISTS = ("summaryResults", "laypersonResults")


# Top-level fields CTIS changes when it republishes, without the trial itself changing: kept in the
# stored payload, left out of the content hash
VOLATILE_FIELDS = frozenset({"publishDate"})


def content_hash(payload: dict) -> bytes:
    return json_hash(payload, VOLATILE_FIELDS)


# --- reading a CTIS record ---------------------------------------------------------------------

def _get(node: object, *path: str) -> object:
    """The value at path in nested dicts, or None where a step is missing or not a dict."""
    current = node
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _string(value: object) -> str | None:
    """A text value as given (stripped), or None for anything else: a number field holding a dict or a
    flag is not an identifier."""
    if not isinstance(value, str):
        return None
    return _text(value)


def identifiers(payload: dict) -> list[Identifier]:
    """Every identifier CTIS gives for the trial: its EU CT number, the EudraCT number of a trial that
    moved over from EudraCT, and the secondary ones (NCT, WHO UTN, ISRCTN, and an EudraCT number
    listed among other registries). A value that is not a valid identifier of its scheme (e.g.
    "N/A") is skipped when the record is stored."""
    found = [Identifier("eu-ct", str(payload["ctNumber"]))]
    eudract = _string(_get(payload, "authorizedApplication", "eudraCt", "eudraCtCode"))
    if eudract:
        found.append(Identifier("eudract", eudract))
    secondary = _get(payload, "authorizedApplication", "authorizedPartI", "trialDetails", "clinicalTrialIdentifiers",
                     "secondaryIdentifyingNumbers")
    if not isinstance(secondary, dict):
        return found
    for key, scheme in SECONDARY_SCHEMES.items():
        number = _string(_get(secondary, key, "number"))
        if number:
            found.append(Identifier(scheme, number))
    others = secondary.get("additionalRegistries")
    if isinstance(others, list):
        for registry in others:
            if not isinstance(registry, dict):
                continue
            name = _string(registry.get("otherRegistryName"))
            number = _string(registry.get("number"))
            if name and number and "eudract" in name.lower():
                found.append(Identifier("eudract", number))
    return found


def _primary_endpoints(payload: dict) -> list[dict]:
    endpoints = _get(payload, "authorizedApplication", "authorizedPartI", "trialDetails", "trialInformation",
                     "endPoint", "primaryEndPoints")
    found = []
    if not isinstance(endpoints, list):
        return found
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        text = _text(endpoint.get("endPoint"))
        if text is None:
            continue
        found.append({"id": endpoint.get("id"), "number": endpoint.get("number"), "text": text})
    return found


ORGANISATION_ID = re.compile(r"ORG-\d+")


def _country(sponsor: dict) -> str | None:
    addresses = sponsor.get("addresses")
    if not isinstance(addresses, list):
        return None
    for address in addresses:
        country = _text(_get(address, "address", "countryName"))
        if country:
            return country
    return None


def sponsors(payload: dict) -> list[core.Sponsor]:
    """The trial's sponsors as CTIS lists them: each an organisation, with EMA's organisation id
    (ORG-…) when CTIS gives one, and which of them is the primary sponsor."""
    listed = _get(payload, "authorizedApplication", "authorizedPartI", "sponsors")
    found = []
    if not isinstance(listed, list):
        return found
    for sponsor in listed:
        if not isinstance(sponsor, dict):
            continue
        organisation = sponsor.get("organisation")
        if not isinstance(organisation, dict):
            continue
        name = _text(organisation.get("name"))
        if name is None:
            continue
        found_identifiers = []
        business_key = _text(organisation.get("businessKey"))
        if business_key and ORGANISATION_ID.fullmatch(business_key):
            found_identifiers.append(Identifier("ema-org", business_key))
        commercial = sponsor.get("isCommercial")
        if not isinstance(commercial, bool):
            commercial = None
        found.append(core.Sponsor(
            name=name, primary=sponsor.get("primary") is True, identifiers=tuple(found_identifiers),
            kind=_text(organisation.get("type")), country=_country(sponsor), commercial=commercial,
        ))
    return found


def _list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    found = []
    for item in value:
        if isinstance(item, dict):
            found.append(item)
    return found


def locations(payload: dict) -> list[core.Location]:
    """Where the trial runs: every site of each member state concerned (its organisation, city,
    postcode and country) with the trial's status in that country, then each member state with no
    site listed, then the countries outside the EU/EEA CTIS names (no sites or status there). The
    people at a site (investigator, email, phone) are left out."""
    application = _get(payload, "authorizedApplication")
    found = []
    countries = []
    for part in _list(_get(application, "authorizedPartsII")):
        msc = _get(part, "mscInfo")
        country = _text(_get(msc, "countryName")) or _text(_get(msc, "mscName"))
        status = _text(_get(msc, "trialStatus"))
        for site in _list(part.get("trialSites")):
            address = _get(site, "organisationAddressInfo", "address")
            site_country = _text(_get(address, "countryName")) or country
            if site_country is None:
                continue
            found.append(core.Location(
                country=site_country, city=_text(_get(address, "city")), postcode=_text(_get(address, "postcode")),
                site=_text(_get(site, "organisationAddressInfo", "organisation", "name")), status=status,
            ))
            countries.append(site_country)
        if country is not None and country not in countries:
            found.append(core.Location(country=country, status=status))
            countries.append(country)
    for msc in _list(_get(application, "memberStatesConcerned")):
        country = _text(msc.get("mscName"))
        if country is not None and country not in countries:
            found.append(core.Location(country=country))
            countries.append(country)
    for other in _list(_get(application, "authorizedPartI", "rowCountriesInfo")):
        country = _text(other.get("name"))
        if country is not None and country not in countries:
            found.append(core.Location(country=country))
            countries.append(country)
    return found


# --- a trial's profile, for patient search (ADR-008) --------------------------------------------

# CTIS's phase codes (its public search's option list, the same codes its records hold) and labels,
# kept as CTIS spells them, with the plain phase each one is (profile.PHASES)
PHASE_CODES = {
    1: ("1", "Human Pharmacology (Phase I)- First administration to humans"),
    2: ("1", "Human Pharmacology (Phase I)- Bioequivalence Study"),
    3: ("1", "Human Pharmacology (Phase I)- Other"),
    4: ("2", "Therapeutic exploratory (Phase II)"),
    5: ("3", "Therapeutic confirmatory (Phase III)"),
    6: ("4", "Therapeutic use (Phase IV)"),
    7: ("1-2", "Phase I and Phase II (Integrated)- First administration to humans"),
    8: ("1-2", "Phase I and Phase II (Integrated)- Bioequivalence Study"),
    9: ("1-2", "Phase I and Phase II (Integrated)- Other"),
    10: ("2-3", "Phase II and Phase III (Integrated)"),
    11: ("3-4", "Phase III and phase IV (Integrated)"),
}
# CTIS's age range categories (populationOfTrialSubjects.ageRanges[].ageRangeCategoryCode)
AGE_RANGE_CODES = {1: "in-utero", 2: "0-17", 3: "18-64", 4: "65+"}
# The status texts of a member state's part of a trial (mscInfo.trialStatus) that mean it recruits now
RECRUITING_TEXTS = ("authorised, recruiting", "ongoing, recruiting")
# CTIS names a translation's language in English, sometimes with its country ("French (France)")
LANGUAGES = {
    "bulgarian": "bg", "croatian": "hr", "czech": "cs", "danish": "da", "dutch": "nl", "english": "en",
    "estonian": "et", "finnish": "fi", "french": "fr", "german": "de", "greek": "el", "hungarian": "hu",
    "icelandic": "is", "irish": "ga", "italian": "it", "latvian": "lv", "lithuanian": "lt", "maltese": "mt",
    "norwegian": "no", "polish": "pl", "portuguese": "pt", "romanian": "ro", "slovak": "sk", "slovenian": "sl",
    "slovene": "sl", "spanish": "es", "swedish": "sv",
}
# A MeSH tree branch in a CTIS therapeutic area's name, e.g. "Diseases [C] - Neoplasms [C04]"
MESH_BRANCH = re.compile(r"\[([A-Z]\d{2})\]\s*$")
MEDDRA_CODE = re.compile(r"\d{8}")


def _same_words(text: str | None) -> str | None:
    """The text lower-cased with its spaces collapsed, for comparing labels CTIS spells with stray spaces."""
    if text is None:
        return None
    return " ".join(text.lower().split())


def _language(description: object) -> str | None:
    """ISO 639-1 code of a language CTIS names, e.g. "French (France)" or "'Danish"."""
    text = _text(description)
    if text is None:
        return None
    first_word = re.sub(r"[^a-z]", "", text.lower().split("(")[0])
    if first_word in LANGUAGES:
        return LANGUAGES[first_word]
    return None


def _translations(node: dict, field: str) -> tuple[profile.Text, ...]:
    """The translations CTIS gives of a text field (its `<field>Translations` list), in languages we know."""
    found = []
    for item in _list(node.get(f"{field}Translations")):
        text = _text(item.get("attributeTranslation"))
        language = _language(item.get("languageDescription"))
        if text is None or language is None:
            continue
        translation = profile.Text(" ".join(text.split()), language)
        if translation not in found:
            found.append(translation)
    return tuple(found)


def _flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _profile_conditions(trial_information: object, part_one: object) -> tuple[profile.Condition, ...]:
    listed = _list(_get(trial_information, "medicalCondition", "partIMedicalConditions"))
    if not listed:
        listed = _list(_get(part_one, "medicalConditions"))
    found = []
    for item in listed:
        name = _text(item.get("medicalCondition"))
        if name is None:
            continue
        condition = profile.Condition(" ".join(name.split()), _translations(item, "medicalCondition"),
                                      _flag(item.get("isConditionRareDisease")))
        if condition not in found:
            found.append(condition)
    return tuple(found)


def _coded_terms(trial_information: object) -> tuple[profile.CodedTerm, ...]:
    """The MedDRA terms CTIS publishes for the trial's conditions."""
    found = []
    for item in _list(_get(trial_information, "medicalCondition", "meddraConditionTerms")):
        code = _text(item.get("classificationCode"))
        name = _text(item.get("termName"))
        if code is None or name is None:
            continue
        group = _text(item.get("organClass"))
        # only a real MedDRA code is a group; CTIS sometimes puts an internal id there
        if group is not None and not MEDDRA_CODE.fullmatch(group):
            group = None
        term = profile.CodedTerm("meddra", code, name, _text(item.get("level")), group)
        if term not in found:
            found.append(term)
    return tuple(found)


def _area(label: str) -> profile.Area:
    match = MESH_BRANCH.search(label)
    if match is None:
        return profile.Area(None, label)
    return profile.Area(match.group(1), label)


def _areas(part_one: object, listing: dict) -> tuple[profile.Area, ...]:
    labels = []
    for item in _list(_get(part_one, "therapeuticAreas")):
        labels.append(_text(item.get("name")))
    if not labels:
        for item in _list(_get(part_one, "partOneTherapeuticAreas")):
            labels.append(_text(_get(item, "therapeuticArea", "name")))
    if not labels and isinstance(listing.get("therapeuticAreas"), list):
        for item in listing["therapeuticAreas"]:
            labels.append(_text(item))
    found = []
    for label in labels:
        if label is None:
            continue
        area = _area(" ".join(label.split()))
        if area not in found:
            found.append(area)
    return tuple(found)


def _phase(trial_information: object, listing: dict) -> tuple[str | None, str | None]:
    """(plain phase, CTIS's label): from the list's label when it gives one, else from the record's code."""
    by_label = {}
    for phase, label in PHASE_CODES.values():
        by_label[_same_words(label)] = (phase, label)
    listed_label = _same_words(_text(listing.get("trialPhase")))
    if listed_label in by_label:
        return by_label[listed_label]
    code = _status_code(_get(trial_information, "trialCategory", "trialPhase"))
    if code in PHASE_CODES:
        return PHASE_CODES[code]
    return None, _text(listing.get("trialPhase"))


def _criteria(trial_information: object, kind: str) -> tuple[profile.Criterion, ...]:
    """The principal inclusion or exclusion criteria ("Inclusion", "Exclusion") as registered, in their order."""
    field = f"principal{kind}Criteria"
    items = _list(_get(trial_information, "eligibilityCriteria", field))
    numbered = []
    for position, item in enumerate(items):
        number = item.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            number = position + 1
        numbered.append((number, position, item))
    numbered.sort()
    found = []
    for _number, _position, item in numbered:
        text = _text(item.get(field))
        if text is None:
            continue
        found.append(profile.Criterion(text, _translations(item, field)))
    return tuple(found)


def _age_groups(population: object, listing: dict) -> tuple[str, ...]:
    found = []
    for item in _list(_get(population, "ageRanges")):
        code = _status_code(item.get("ageRangeCategoryCode"))
        if code in AGE_RANGE_CODES and AGE_RANGE_CODES[code] not in found:
            found.append(AGE_RANGE_CODES[code])
    if not found:
        listed = _same_words(_text(listing.get("ageGroup")))
        if listed is not None:
            for group in profile.AGE_GROUPS:
                words = group
                if group == "in-utero":
                    words = "in utero"
                if words in listed:
                    found.append(group)
    ordered = []
    for group in profile.AGE_GROUPS:
        if group in found:
            ordered.append(group)
    return tuple(ordered)


def _sexes(population: object, listing: dict) -> tuple[bool | None, bool | None]:
    female = _flag(_get(population, "isFemaleSubjects"))
    male = _flag(_get(population, "isMaleSubjects"))
    if female is None and male is None:
        listed = _same_words(_text(listing.get("gender")))
        if listed is not None:
            words = listed.replace(",", " ").split()
            female = "female" in words
            male = "male" in words
    return female, male


def _groups(population: object) -> tuple[bool | None, bool | None]:
    """(healthy volunteers, patients): whether the trial takes them, None when the record doesn't say."""
    names = []
    for item in _list(_get(population, "clinicalTrialGroups")):
        name = _same_words(_text(item.get("name")))
        if name is not None:
            names.append(name)
    if not names:
        return None, None
    healthy = False
    patients = False
    for name in names:
        if "healthy" in name:
            healthy = True
        if "patient" in name:
            patients = True
    return healthy, patients


def _substances(product: dict) -> tuple[str, ...]:
    found = []
    dictionary = _get(product, "productDictionaryInfo")
    names = []
    for substance in _list(_get(dictionary, "productSubstances")):
        names.append(_text(substance.get("actSubstName")))
    names.append(_text(_get(dictionary, "activeSubstanceName")))
    listed = product.get("jsonActiveSubstanceNames")
    if isinstance(listed, list):
        for name in listed:
            names.append(_text(name))
    else:
        names.append(_text(listed))
    for name in names:
        if name is None or name == "-":
            continue
        if name.lower() not in found:
            found.append(name.lower())
    return tuple(found)


def _product(item: dict, role: str | None) -> profile.Product:
    dictionary = _get(item, "productDictionaryInfo")
    atc_code = _text(_get(dictionary, "atcCode"))
    if atc_code == "-":
        atc_code = None
    atc_name = _text(_get(dictionary, "atcName"))
    if atc_name == "-":
        atc_name = None
    routes = []
    for route in _list_of_text(item.get("routes")):
        if route not in routes:
            routes.append(route)
    origins = []
    for substance in _list(_get(dictionary, "productSubstances")):
        origin = _text(substance.get("actSubstOrigin"))
        if origin is not None and origin != "-" and origin not in origins:
            origins.append(origin)
    name = _text(item.get("productName"))
    if name is None:
        name = _text(_get(dictionary, "prodName"))
    return profile.Product(
        name=name, role=role, substances=_substances(item), atc_code=atc_code, atc_name=atc_name,
        form=_text(_get(dictionary, "pharmForm")), routes=tuple(routes), origins=tuple(origins),
        orphan=_flag(item.get("orphanDrugEdit")),
    )


def _list_of_text(value: object) -> list[str]:
    found = []
    if not isinstance(value, list):
        return found
    for item in value:
        text = _text(item)
        if text is not None:
            found.append(text)
    return found


def _products(part_one: object) -> tuple[profile.Product, ...]:
    """What the trial tests and gives: each product with its role (test, comparator, placebo, auxiliary)."""
    found = []
    for group in _list(_get(part_one, "productRoleGroupInfos")):
        role = _text(group.get("productRoleName"))
        if role is not None:
            role = role.lower()
        for item in _list(group.get("products")):
            product = _product(item, role)
            if product not in found:
                found.append(product)
    if found:
        return tuple(found)
    for item in _list(_get(part_one, "products")):
        product = _product(item, None)
        if product not in found:
            found.append(product)
    return tuple(found)


def _listed_country_codes(listing: dict) -> dict[str, int]:
    """Country -> status code from the list's trialCountries ("France:4")."""
    found = {}
    listed = listing.get("trialCountries")
    if not isinstance(listed, list):
        return found
    for item in listed:
        text = _text(item)
        if text is None or ":" not in text:
            continue
        country, code_text = text.rsplit(":", 1)
        code = _status_code(code_text)
        if code is not None and country.strip():
            found[country.strip()] = code
    return found


def _recruiting_text(status: str | None) -> bool | None:
    if status is None:
        return None
    return _same_words(status) in RECRUITING_TEXTS


def _countries(application: object, listing: dict) -> tuple[profile.CountryStatus, ...]:
    """Each member state with the trial's status there, recruitment dates, and whether it recruits now:
    the list's code first (it is re-read every sync), else the record's."""
    listed_codes = _listed_country_codes(listing)
    record_codes = {}
    for msc in _list(_get(application, "memberStatesConcerned")):
        name = _text(msc.get("mscName"))
        code = _status_code(msc.get("mscPublicStatusCode"))
        if name is not None and code is not None:
            record_codes[name] = code
    found = []
    seen = []
    for part in _list(_get(application, "authorizedPartsII")):
        msc = _get(part, "mscInfo")
        country = _text(_get(msc, "countryName"))
        if country is None:
            country = _text(_get(msc, "mscName"))
        if country is None or country in seen:
            continue
        seen.append(country)
        status = _text(_get(msc, "trialStatus"))
        period = _get(msc, "activeTrialRecruitmentPeriod")
        found.append(profile.CountryStatus(
            country=country, status=status, recruiting=_country_recruiting(country, status, listed_codes, record_codes),
            recruitment_start=_text(_get(period, "recruitmentStartDate")),
            recruitment_end=_text(_get(period, "recruitmentEndDate")),
        ))
    others = []
    for name in record_codes:
        others.append(name)
    for name in listed_codes:
        others.append(name)
    for country in others:
        if country in seen:
            continue
        seen.append(country)
        found.append(profile.CountryStatus(country=country,
                                           recruiting=_country_recruiting(country, None, listed_codes, record_codes)))
    return tuple(found)


def _country_recruiting(country: str, status: str | None, listed_codes: dict[str, int],
                        record_codes: dict[str, int]) -> bool | None:
    if country in listed_codes:
        return listed_codes[country] in RECRUITING_CODES
    if country in record_codes:
        return record_codes[country] in RECRUITING_CODES
    return _recruiting_text(status)


def _overall_recruiting(payload: dict, listing: dict, countries: tuple[profile.CountryStatus, ...]) -> bool:
    listed = listing_recruiting(listing)
    if listed is not None:
        return listed
    code = _status_code(payload.get("ctPublicStatusCode"))
    if code is not None:
        return code in RECRUITING_CODES
    for country in countries:
        if country.recruiting is True:
            return True
    return False


def trial_profile(payload: dict, listing: dict | None = None) -> profile.TrialProfile:
    """The trial's profile from its CTIS record, with its row of CTIS's list where we have it (fresher
    status, and labels the record holds only as codes). No people."""
    if listing is None:
        listing = {}
    application = _get(payload, "authorizedApplication")
    part_one = _get(application, "authorizedPartI")
    details = _get(part_one, "trialDetails")
    information = _get(details, "trialInformation")
    population = _get(information, "populationOfTrialSubjects")
    identifiers_node = _get(details, "clinicalTrialIdentifiers")
    if not isinstance(identifiers_node, dict):
        identifiers_node = {}
    phase, phase_label = _phase(information, listing)
    female, male = _sexes(population, listing)
    healthy, patients = _groups(population)
    countries = _countries(application, listing)
    low_intervention = _flag(_get(information, "trialCategory", "isLowIntervention"))
    if low_intervention is None:
        low_intervention = _flag(_get(part_one, "isLowIntervention"))
    status = _text(payload.get("ctStatus"))
    return profile.TrialProfile(
        public_title=_text(identifiers_node.get("publicTitle")), full_title=_text(identifiers_node.get("fullTitle")),
        title_translations=_translations(identifiers_node, "publicTitle"), status=status,
        recruiting=_overall_recruiting(payload, listing, countries),
        conditions=_profile_conditions(information, part_one), coded_terms=_coded_terms(information),
        areas=_areas(part_one, listing), phase=phase, phase_label=phase_label, low_intervention=low_intervention,
        main_objective=_text(_get(information, "trialObjective", "mainObjective")),
        inclusion=_criteria(information, "Inclusion"), exclusion=_criteria(information, "Exclusion"),
        age_groups=_age_groups(population, listing), female=female, male=male, healthy_volunteers=healthy,
        patients=patients, products=_products(part_one), countries=countries,
    )


def registered(payload: dict) -> dict:
    """The registered information checks ask about, in the shape every adapter fills."""
    trial_details = _get(payload, "authorizedApplication", "authorizedPartI", "trialDetails")
    identifiers_node = _get(trial_details, "clinicalTrialIdentifiers")
    duration = _get(trial_details, "trialInformation", "trialDuration")
    return {
        "public_title": _text(_get(identifiers_node, "publicTitle")),
        "full_title": _text(_get(identifiers_node, "fullTitle")),
        "short_title": _text(_get(identifiers_node, "shortTitle")),
        "status": _text(payload.get("ctStatus")),
        "start_date": _text(payload.get("startDateEU")),
        "end_date": _text(payload.get("endDateEU")),
        "estimated_global_end_date": _text(_get(duration, "estimatedGlobalEndDate")),
        "primary_endpoints": _primary_endpoints(payload),
    }


def document_language(entry: dict) -> str | None:
    """The document's ISO 639-1 language, if CTIS gives one; its numeric languageCode is CTIS's own list."""
    code = _text(entry.get("languageCode"))
    if code is not None and len(code) == 2 and code.isalpha():
        return code.lower()
    return None


def submission_dates(payload: dict) -> dict[str, datetime]:
    """Results submission id (as text) -> when it was submitted, from the record's results lists."""
    found = {}
    for list_name in RESULTS_LISTS:
        submissions = _get(payload, "results", list_name)
        if not isinstance(submissions, list):
            continue
        for submission in submissions:
            if not isinstance(submission, dict):
                continue
            submitted = _text(submission.get("submissionDate"))
            if submission.get("id") is None or submitted is None:
                continue
            try:
                # CTIS gives e.g. 2025-04-10T17:39:54, in UTC
                when = datetime.fromisoformat(submitted).replace(tzinfo=UTC)
            except ValueError:
                continue
            found[str(submission["id"])] = when
    return found


def published_at(entry: dict, submitted: dict[str, datetime]) -> datetime | None:
    """When the results a document belongs to were submitted, if the record says."""
    entity = entry.get("associatedEntityId")
    if entity is None:
        return None
    key = str(entity)
    if key not in submitted:
        return None
    return submitted[key]


def documents(payload: dict) -> list[DocumentRef] | None:
    """The documents the record lists; None when it has no list of documents (then it says nothing
    about them)."""
    entries = payload.get("documents")
    if not isinstance(entries, list):
        return None
    submitted = submission_dates(payload)
    found = []
    for entry in entries:
        if isinstance(entry, dict):
            found.append(document_ref(entry, submitted))
    return found


def document_ref(entry: dict, submitted: dict[str, datetime]) -> DocumentRef:
    """One entry of a record's documents. An entry without its uuid or version can't be fetched: it is
    kept with the problem, so the transfer reports it."""
    problem = None
    for needed in ("uuid", "systemVersion"):
        if problem is None and needed not in entry:
            problem = f"KeyError: {needed!r}"
    version = ""
    if "systemVersion" in entry:
        version = str(entry["systemVersion"])
    listed_as_pdf = str(entry.get("fileType", "")).upper() == "PDF"
    return DocumentRef(key=str(entry.get("uuid")), version=version, kind=document_kind(entry),
                       title=_text(entry.get("title")), language=document_language(entry),
                       published_at=published_at(entry, submitted), listed_as_pdf=listed_as_pdf, problem=problem)


def document_kind(entry: dict) -> str:
    try:
        code = int(entry.get("documentType"))
    except (TypeError, ValueError):
        return "other"
    if code in DOCUMENT_KINDS:
        return DOCUMENT_KINDS[code]
    return "other"


# --- the adapter (ADR-005) --------------------------------------------------------------------

# CTIS's overall trial status codes (its public search's option list, not documented by EMA; checked
# against the labels of its CSV export): 1 Under evaluation, 2 Authorised, recruitment pending,
# 3 Authorised, recruiting, 4 Ongoing, recruiting, 5 Ongoing, recruitment ended, 6 Temporarily halted,
# 7 Suspended, 8 Ended, 9 Expired, 10 Revoked, 11 Not authorised, 12 Cancelled
RECRUITING_CODES = (3, 4)


def _status_code(value: object) -> int | None:
    """A status code as CTIS gives it (a number, or a number in text), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value)
    if text is None or not text.isdigit():
        return None
    return int(text)


def listing_recruiting(row: dict) -> bool | None:
    """Whether CTIS's list calls the trial recruiting now (overall status 3 or 4); None when the row has
    no status we can read."""
    code = _status_code(row.get("ctStatus"))
    if code is None:
        return None
    return code in RECRUITING_CODES


def listed(row: dict) -> Listed:
    """One row of CTIS's public list as a listed trial."""
    ct_number = str(row.get("ctNumber", "")).strip()
    if not CT_NUMBER.fullmatch(ct_number):
        raise ValueError(f"CTIS listed a trial without an EU CT number: {str(row)[:200]}")
    title = _text(storable(row.get("ctTitle")))
    if title is not None:
        title = " ".join(title.split())
    return Listed(key=ct_number, title=title, identifiers=[Identifier("eu-ct", ct_number)],
                  updated_on=_day(row.get("lastUpdated")), summary=storable(row),
                  recruiting=listing_recruiting(row))


def _day(value: object) -> date | None:
    """CTIS's dd/mm/yyyy as a date; None when missing or not a date."""
    text = _text(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        return None


# Config fields and what they hold
LISTING_FIELDS = {"criteria", "page_size"}
EVIDENCE_FIELDS = {"document_kinds"}
MAX_PAGE_SIZE = 500


class CtisAdapter(Adapter):
    """CTIS through its public API: the list from its search, a trial's record from /retrieve, its
    documents from /documents/…/download."""

    name = "ctis"
    label = "EU Clinical Trials Information System"
    kind = "registry"
    url = "https://euclinicaltrials.eu"
    lists = True
    transfers = True
    # every kind DOCUMENT_KINDS maps CTIS's document types to
    document_kinds = ("lay-summary", "other", "protocol", "results-summary", "study-report")
    listing_columns = (("Sponsor", "sponsor"), ("Phase", "trialPhase"), ("Results received", "resultsFirstReceived"))
    key_label = "EU CT number"
    key_scheme = "eu-ct"

    def __init__(self, client=None) -> None:
        # a CtisClient, or a fake with the same search / retrieve / download_document methods
        self.client = client

    @classmethod
    @contextmanager
    def connected(cls) -> Iterator["CtisAdapter"]:
        with CtisClient() as client:
            yield cls(client)

    def default_config(self) -> dict:
        return {"listing": {"criteria": {}, "page_size": MAX_PAGE_SIZE},
                "evidence": {"document_kinds": list(DEFAULT_EVIDENCE_KINDS)}}

    def config_problems(self, config: dict) -> list[str]:
        problems = []
        for name in config:
            if name not in ("listing", "evidence"):
                problems.append(f"unknown field {name!r} (there are listing and evidence)")
        listing = config.get("listing", {})
        evidence = config.get("evidence", {})
        if not isinstance(listing, dict) or not isinstance(evidence, dict):
            return problems + ["listing and evidence must be objects"]
        for name in listing:
            if name not in LISTING_FIELDS:
                problems.append(f"unknown listing field {name!r} (there are {', '.join(sorted(LISTING_FIELDS))})")
        for name in evidence:
            if name not in EVIDENCE_FIELDS:
                problems.append(f"unknown evidence field {name!r} (there is document_kinds)")
        if not isinstance(listing.get("criteria", {}), dict):
            problems.append("listing.criteria must be an object of CTIS search fields, e.g. {\"status\": [8]}")
        page_size = listing.get("page_size", MAX_PAGE_SIZE)
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
            problems.append(f"listing.page_size must be a whole number from 1 to {MAX_PAGE_SIZE}")
        kinds = evidence.get("document_kinds", list(DEFAULT_EVIDENCE_KINDS))
        if kinds is not None:
            known = set(DOCUMENT_KINDS.values())
            if not isinstance(kinds, list) or not kinds:
                problems.append("evidence.document_kinds must be a list of kinds, or null for every kind")
            else:
                for kind in kinds:
                    if kind not in known:
                        problems.append(f"unknown document kind {kind!r} (CTIS documents are {', '.join(sorted(known))})")
        return problems

    def evidence_kinds(self, config: dict) -> list[str] | None:
        evidence = config.get("evidence", {})
        if "document_kinds" not in evidence:
            return list(DEFAULT_EVIDENCE_KINDS)
        return evidence["document_kinds"]

    def key_problem(self, key: str) -> str | None:
        if CT_NUMBER.fullmatch(key):
            return None
        return f"not an EU CT number: {key!r}"

    def list_pages(self, config: dict) -> Iterator[ListingPage]:
        """The list in EU CT number order. CTIS answers at most SEARCH_LIMIT rows per query, so a
        longer list is read from both ends (ascending, then descending until it comes down to the
        trials the ascending pass read): up to twice SEARCH_LIMIT. Each trial is given once; the
        stage compares how many came with the total CTIS gives. A trial seen twice in the same pass
        means the list shifted between two pages (a trial came or went while it was read): it is
        skipped, the pass goes on, and the count tells the stage whether anything was missed."""
        listing = config.get("listing", {})
        criteria = listing.get("criteria", {})
        size = listing.get("page_size", MAX_PAGE_SIZE)
        pages_per_query = max(1, SEARCH_LIMIT // size)
        # every trial given so far, and the ones the ascending pass read
        seen = set()
        ascending = set()
        total = None
        for direction in ("ASC", "DESC"):
            for page in range(1, pages_per_query + 1):
                answer = self.client.search(criteria, page, size, direction)
                pagination = answer.get("pagination", {})
                if total is None:
                    total = pagination.get("totalRecords")
                rows = []
                met_the_other_end = False
                for row in answer["data"]:
                    found = listed(row)
                    if direction == "DESC" and found.key in ascending:
                        met_the_other_end = True
                        continue
                    if found.key in seen:
                        continue
                    seen.add(found.key)
                    if direction == "ASC":
                        ascending.add(found.key)
                    rows.append(found)
                everything = total is not None and len(seen) >= total
                last_page = not pagination.get("nextPage") or not answer["data"]
                done = everything or met_the_other_end or (direction == "ASC" and last_page)
                ended = done or direction == "DESC" and (last_page or page == pages_per_query)
                yield ListingPage(rows=rows, total=total, more=not ended)
                if done:
                    return
                if last_page:
                    break
            if total is None or total <= len(seen):
                return

    def public_url(self, key: str) -> str | None:
        return f"https://euclinicaltrials.eu/search-for-clinical-trials/?lang=en&EUCT={key}"

    def fetch(self, key: str) -> dict:
        return self.client.retrieve(key)

    def parse(self, payload: dict, listing: dict | None) -> SourceTrial:
        """A record (cleaned of what Postgres can't store) as a SourceTrial. Its identifiers and
        registered reading must be read, or the transfer fails; its sponsors, sites and profile are not
        evidence, so a problem there is a warning."""
        trial = SourceTrial(content_hash=content_hash(payload), identifiers=identifiers(payload),
                            registered=registered(payload), sponsors=None, locations=None, profile=None,
                            documents=documents(payload))
        try:
            trial.sponsors = sponsors(payload)
        except Exception as exc:  # reported, the transfer can still complete
            trial.warnings["sponsors"] = f"{type(exc).__name__}: {exc}"
        try:
            trial.locations = locations(payload)
        except Exception as exc:
            trial.warnings["locations"] = f"{type(exc).__name__}: {exc}"
        try:
            trial.profile = trial_profile(payload, listing)
        except Exception as exc:
            trial.warnings["profile"] = f"{type(exc).__name__}: {exc}"
        return trial

    def download(self, key: str, document: DocumentRef) -> DownloadedFile:
        # Downloaded every time, even when we hold this version: CTIS could replace the file without
        # raising its systemVersion, and only the bytes would show it
        return self.client.download_document(key, document.key)
