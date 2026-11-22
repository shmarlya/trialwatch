"""CTIS records of recruiting trials, shaped as CTIS gives them, for the patient search tests (ADR-008).
`record(...)` makes one with the conditions, sites and population a test needs; `listing(...)` the row
CTIS's list gives for it."""

import copy

EMAIL = "investigator@hospital.example"

DIABETES = {"name": "Type 2 Diabetes Mellitus", "translations": {"French": "Diabète de type 2",
                                                                 "Spanish": "Diabetes tipo 2"}, "rare": False}
DIABETES_TERM = {"level": "LLT", "classificationCode": "10045242", "termName": "Type II diabetes mellitus",
                 "organClass": "10027433", "version": "26.0"}
PARIS = ("Paris", "75013", "Hôpital Pitié-Salpêtrière", "France")
VILLEJUIF = ("Villejuif", "94800", "Gustave Roussy", "France")
LYON = ("Lyon", "69003", "Hôpital Édouard Herriot", "France")
MADRID = ("Madrid", "28046", "Hospital La Paz", "Spain")


def _translations(texts: dict) -> list:
    found = []
    for number, (language, text) in enumerate(texts.items()):
        found.append({"id": 900 + number, "language": number, "languageDescription": language,
                      "attributeTranslation": text})
    return found


def _site(city, postcode, organisation, country):
    return {"id": 1,
            "personInfo": {"firstName": "Ada", "lastName": "Investigator", "email": EMAIL, "telephone": "+33100000000"},
            "organisationAddressInfo": {"email": EMAIL, "phone": "+33100000001",
                                        "address": {"city": city, "postcode": postcode, "countryName": country},
                                        "organisation": {"name": organisation}}}


def record(ct_number, *, conditions=(DIABETES,), terms=(DIABETES_TERM,),
           areas=("Diseases [C] - Nutritional and Metabolic Diseases [C18]",), sites=(PARIS, VILLEJUIF),
           statuses=None, status="Ongoing, recruiting", status_code=4, age_codes=("3", "4"),
           groups=("Patients",), female=True, male=True, phase_code="5", title="A study of a new medicine",
           title_translations=None, inclusion=("Adults aged 18 to 75 years with type 2 diabetes",),
           exclusion=("Pregnancy",), products=(("Test", "SEMAGLUTIDE", "semaglutide", "Chemical"),),
           objective="To compare the new medicine with placebo"):
    """A CTIS record of a recruiting trial. `statuses`: country -> its status text (default: each site's
    country "Ongoing, recruiting")."""
    if statuses is None:
        statuses = {}
        for site in sites:
            statuses[site[3]] = "Ongoing, recruiting"
    parts = []
    for country, country_status in statuses.items():
        country_sites = []
        for site in sites:
            if site[3] == country:
                country_sites.append(_site(*site))
        parts.append({"mscInfo": {"mscName": country, "countryName": country, "trialStatus": country_status,
                                  "activeTrialRecruitmentPeriod": {"recruitmentStartDate": "2026-01-15"}},
                      "trialSites": country_sites})
    condition_items = []
    for condition in conditions:
        condition_items.append({"medicalCondition": condition["name"], "isConditionRareDisease": condition["rare"],
                                "medicalConditionTranslations": _translations(condition["translations"])})
    area_items = []
    for number, area in enumerate(areas):
        area_items.append({"code": str(number), "name": area})
    inclusion_items = []
    for number, text in enumerate(inclusion, start=1):
        inclusion_items.append({"id": number, "number": number, "principalInclusionCriteria": text,
                                "principalInclusionCriteriaTranslations": []})
    exclusion_items = []
    for number, text in enumerate(exclusion, start=1):
        exclusion_items.append({"id": number, "number": number, "principalExclusionCriteria": text})
    role_groups = []
    for role, name, substance, origin in products:
        role_groups.append({"productRoleName": role, "products": [{
            "productName": name, "jsonActiveSubstanceNames": substance, "routes": ["ORAL USE"], "orphanDrugEdit": False,
            "productDictionaryInfo": {"pharmForm": "FILM-COATED TABLET", "atcCode": "A10BJ06", "atcName": "semaglutide",
                                      "productSubstances": [{"actSubstName": substance.upper(), "actSubstOrigin": origin}]},
        }]})
    age_ranges = []
    for code in age_codes:
        age_ranges.append({"ageRangeCategoryCode": code})
    group_items = []
    for name in groups:
        group_items.append({"name": name})
    members = []
    for country in statuses:
        members.append({"mscName": country})
    if title_translations is None:
        title_translations = {}
    return {
        "ctNumber": ct_number, "ctStatus": status, "ctPublicStatusCode": status_code, "documents": [],
        "authorizedApplication": {
            "memberStatesConcerned": members,
            "authorizedPartsII": parts,
            "authorizedPartI": {
                "therapeuticAreas": area_items,
                "productRoleGroupInfos": role_groups,
                "trialDetails": {
                    "clinicalTrialIdentifiers": {"publicTitle": title, "fullTitle": f"{title}: the full title",
                                                 "publicTitleTranslations": _translations(title_translations)},
                    "trialInformation": {
                        "medicalCondition": {"partIMedicalConditions": condition_items,
                                             "meddraConditionTerms": list(copy.deepcopy(terms))},
                        "trialCategory": {"trialPhase": phase_code, "isLowIntervention": False},
                        "trialObjective": {"mainObjective": objective},
                        "eligibilityCriteria": {"principalInclusionCriteria": inclusion_items,
                                                "principalExclusionCriteria": exclusion_items},
                        "populationOfTrialSubjects": {"ageRanges": age_ranges, "isFemaleSubjects": female,
                                                      "isMaleSubjects": male, "clinicalTrialGroups": group_items},
                    },
                },
            },
        },
    }


def listing(ct_number, *, status_code=4, countries=None, phase="Therapeutic confirmatory  (Phase III)",
            updated="10/11/2026"):
    """CTIS's list row for a trial. `countries`: country -> its status code (default France 4)."""
    if countries is None:
        countries = {"France": 4}
    listed_countries = []
    for country, code in countries.items():
        listed_countries.append(f"{country}:{code}")
    return {"ctNumber": ct_number, "ctTitle": "A listed study", "ctStatus": status_code, "trialPhase": phase,
            "trialCountries": listed_countries, "lastUpdated": updated, "sponsor": "Example Pharma",
            "therapeuticAreas": ["Diseases [C] - Nutritional and Metabolic Diseases [C18]"],
            "ageGroup": "18-64 years, 65+ years", "gender": "Female, Male", "conditions": "Type 2 Diabetes Mellitus"}


class ManyCtis:
    """CTIS's search and retrieve over several trials: `records` maps an EU CT number to its record,
    `rows` is its list (CTIS order: by EU CT number)."""

    def __init__(self, records, rows=()):
        self.records = records
        self.rows = list(rows)
        self.retrieved = []

    def retrieve(self, ct_number):
        self.retrieved.append(ct_number)
        return copy.deepcopy(self.records[ct_number])

    def download_document(self, ct_number, uuid):
        raise AssertionError("these trials list no documents")

    def search(self, criteria, page, size, direction="ASC"):
        ordered = sorted(self.rows, key=_ct_number, reverse=direction == "DESC")
        start = (page - 1) * size
        rows = ordered[start:start + size]
        more = start + size < len(ordered)
        return {"pagination": {"totalRecords": len(ordered), "currentPage": page, "nextPage": more}, "data": rows}


def _ct_number(row):
    return row["ctNumber"]
