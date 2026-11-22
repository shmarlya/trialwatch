"""A trial's profile (ADR-008, patient search): what someone looking for a trial to join needs to know,
read by a source's adapter from the trial's stored record into one shape for every source.

What it holds: the conditions (with their translations, a rare-disease flag, and the coded terms the
source publishes, e.g. MedDRA), the therapeutic areas (MeSH tree branches), the phase, who can join
(age groups, sex, patients or healthy volunteers, the inclusion and exclusion criteria as written),
what is tested (products, substances, ATC codes) and, per country, the trial's status there and
whether it recruits now. Never the people at a site.

A profile is read at every transfer (like locations), stored in core.trial_profile and rebuilt from
stored records without fetching anything (`trialwatch source profiles`). It is not part of a record's
`registered` reading: checks don't read it, so reading it differently never outdates a judgement.
"""

from dataclasses import dataclass

import psycopg

from trialwatch.db import core
from trialwatch.hashing import json_hash

# Age groups every source's profile uses (CTIS's own groups)
AGE_GROUPS = ("in-utero", "0-17", "18-64", "65+")
# Phases in plain order: a single phase, or two phases a trial integrates
PHASES = ("1", "1-2", "2", "2-3", "3", "3-4", "4")


@dataclass(frozen=True)
class Text:
    """A text in a language (ISO 639-1; "en" for the registry's own text, which CTIS asks in English)."""

    text: str
    language: str


@dataclass(frozen=True)
class Condition:
    name: str
    translations: tuple[Text, ...] = ()
    rare: bool | None = None


@dataclass(frozen=True)
class CodedTerm:
    """A term of a coding system as the source publishes it for this trial (e.g. MedDRA): only the
    terms the source gives, never the system's dictionary."""

    system: str
    code: str
    name: str
    # the term's level in its system (MedDRA: LLT, PT, HLT, HLGT, SOC)
    level: str | None = None
    # the broad group the source files it under (MedDRA: the system organ class's code)
    group: str | None = None


@dataclass(frozen=True)
class Area:
    """A therapeutic area: a MeSH tree branch, e.g. C04 Neoplasms (code None: "Not possible to specify")."""

    code: str | None
    label: str


@dataclass(frozen=True)
class Product:
    name: str | None
    # test, comparator, placebo, auxiliary
    role: str | None = None
    substances: tuple[str, ...] = ()
    atc_code: str | None = None
    atc_name: str | None = None
    form: str | None = None
    routes: tuple[str, ...] = ()
    # what the substances are made of as the source says (e.g. Chemical, Protein, Vaccine, Cell therapy)
    origins: tuple[str, ...] = ()
    orphan: bool | None = None


@dataclass(frozen=True)
class CountryStatus:
    country: str
    status: str | None = None
    # the trial recruits in this country now (None: unknown)
    recruiting: bool | None = None
    recruitment_start: str | None = None
    recruitment_end: str | None = None


@dataclass(frozen=True)
class Criterion:
    text: str
    translations: tuple[Text, ...] = ()


@dataclass(frozen=True)
class TrialProfile:
    public_title: str | None = None
    full_title: str | None = None
    title_translations: tuple[Text, ...] = ()
    status: str | None = None
    # it recruits now somewhere (the source's list first, it is fresher than the record)
    recruiting: bool = False
    conditions: tuple[Condition, ...] = ()
    coded_terms: tuple[CodedTerm, ...] = ()
    areas: tuple[Area, ...] = ()
    # one of PHASES, or None
    phase: str | None = None
    phase_label: str | None = None
    low_intervention: bool | None = None
    main_objective: str | None = None
    inclusion: tuple[Criterion, ...] = ()
    exclusion: tuple[Criterion, ...] = ()
    # some of AGE_GROUPS
    age_groups: tuple[str, ...] = ()
    female: bool | None = None
    male: bool | None = None
    healthy_volunteers: bool | None = None
    patients: bool | None = None
    products: tuple[Product, ...] = ()
    countries: tuple[CountryStatus, ...] = ()


# --- to and from stored JSON (core.trial_profile.profile) ----------------------------------------

def _texts_data(texts: tuple[Text, ...]) -> list[dict]:
    found = []
    for text in texts:
        found.append({"text": text.text, "language": text.language})
    return found


def _texts_from(data: list) -> tuple[Text, ...]:
    found = []
    for item in data:
        found.append(Text(item["text"], item["language"]))
    return tuple(found)


def _criteria_data(criteria: tuple[Criterion, ...]) -> list[dict]:
    found = []
    for criterion in criteria:
        found.append({"text": criterion.text, "translations": _texts_data(criterion.translations)})
    return found


def _criteria_from(data: list) -> tuple[Criterion, ...]:
    found = []
    for item in data:
        found.append(Criterion(item["text"], _texts_from(item["translations"])))
    return tuple(found)


def profile_data(profile: TrialProfile) -> dict:
    """The profile as JSON, to store; profile_from_data reads it back."""
    conditions = []
    for condition in profile.conditions:
        conditions.append({"name": condition.name, "translations": _texts_data(condition.translations),
                           "rare": condition.rare})
    terms = []
    for term in profile.coded_terms:
        terms.append({"system": term.system, "code": term.code, "name": term.name, "level": term.level,
                      "group": term.group})
    areas = []
    for area in profile.areas:
        areas.append({"code": area.code, "label": area.label})
    products = []
    for product in profile.products:
        products.append({"name": product.name, "role": product.role, "substances": list(product.substances),
                         "atc_code": product.atc_code, "atc_name": product.atc_name, "form": product.form,
                         "routes": list(product.routes), "origins": list(product.origins), "orphan": product.orphan})
    countries = []
    for country in profile.countries:
        countries.append({"country": country.country, "status": country.status, "recruiting": country.recruiting,
                          "recruitment_start": country.recruitment_start,
                          "recruitment_end": country.recruitment_end})
    return {
        "public_title": profile.public_title, "full_title": profile.full_title,
        "title_translations": _texts_data(profile.title_translations), "status": profile.status,
        "recruiting": profile.recruiting, "conditions": conditions, "coded_terms": terms, "areas": areas,
        "phase": profile.phase, "phase_label": profile.phase_label, "low_intervention": profile.low_intervention,
        "main_objective": profile.main_objective, "inclusion": _criteria_data(profile.inclusion),
        "exclusion": _criteria_data(profile.exclusion), "age_groups": list(profile.age_groups),
        "female": profile.female, "male": profile.male, "healthy_volunteers": profile.healthy_volunteers,
        "patients": profile.patients, "products": products, "countries": countries,
    }


def profile_from_data(data: dict) -> TrialProfile:
    conditions = []
    for item in data["conditions"]:
        conditions.append(Condition(item["name"], _texts_from(item["translations"]), item["rare"]))
    terms = []
    for item in data["coded_terms"]:
        terms.append(CodedTerm(item["system"], item["code"], item["name"], item["level"], item["group"]))
    areas = []
    for item in data["areas"]:
        areas.append(Area(item["code"], item["label"]))
    products = []
    for item in data["products"]:
        products.append(Product(
            name=item["name"], role=item["role"], substances=tuple(item["substances"]), atc_code=item["atc_code"],
            atc_name=item["atc_name"], form=item["form"], routes=tuple(item["routes"]),
            origins=tuple(item["origins"]), orphan=item["orphan"],
        ))
    countries = []
    for item in data["countries"]:
        countries.append(CountryStatus(item["country"], item["status"], item["recruiting"],
                                       item["recruitment_start"], item["recruitment_end"]))
    return TrialProfile(
        public_title=data["public_title"], full_title=data["full_title"],
        title_translations=_texts_from(data["title_translations"]), status=data["status"],
        recruiting=data["recruiting"], conditions=tuple(conditions), coded_terms=tuple(terms), areas=tuple(areas),
        phase=data["phase"], phase_label=data["phase_label"], low_intervention=data["low_intervention"],
        main_objective=data["main_objective"], inclusion=_criteria_from(data["inclusion"]),
        exclusion=_criteria_from(data["exclusion"]), age_groups=tuple(data["age_groups"]), female=data["female"],
        male=data["male"], healthy_volunteers=data["healthy_volunteers"], patients=data["patients"],
        products=tuple(products), countries=tuple(countries),
    )


def profile_hash(profile: TrialProfile) -> bytes:
    """sha256 of the profile's content: a search document or an AI annotation made from the same hash
    needs no rebuilding."""
    return json_hash(profile_data(profile))


def recruiting_countries(profile: TrialProfile) -> list[str]:
    """The countries where the trial recruits now, in the profile's order."""
    found = []
    for country in profile.countries:
        if country.recruiting is True and country.country not in found:
            found.append(country.country)
    return found


def store(conn: psycopg.Connection, trial_id: int, source: str, source_key: str, read: TrialProfile) -> bool:
    """Store a profile read from a source's newest record of the trial; True when it is new or changed."""
    with conn.transaction():
        return core.set_trial_profile(conn, trial_id=trial_id, source=source, source_key=source_key,
                                      profile=profile_data(read), profile_hash=profile_hash(read),
                                      recruiting=read.recruiting)
