"""What patient search shares (ADR-008), in one place: the age groups and phases a profile names and how
pages say them, the distances a search offers, and the tokens a search document holds besides words."""

# The age groups a profile names (sources/profile.py AGE_GROUPS), and the ones a query or an age means
BEFORE_BIRTH = "in-utero"
CHILDREN = "0-17"
ADULTS = "18-64"
OLDER = "65+"
AGE_GROUP_WORDS = {
    BEFORE_BIRTH: "before birth",
    CHILDREN: "children (0–17)",
    ADULTS: "adults (18–64)",
    OLDER: "older people (65+)",
}


def age_group_of(age: int) -> str:
    """The age group an age is in."""
    if age < 18:
        return CHILDREN
    if age < 65:
        return ADULTS
    return OLDER


# A profile's phases (sources/profile.py PHASES) in plain words; the search filters by the plain ones
PHASE_WORDS = {
    "1": "phase 1: first tests in people, mostly about safety",
    "1-2": "phase 1–2: safety and first signs it works",
    "2": "phase 2: does it work, and at what dose",
    "2-3": "phase 2–3: does it work, compared with usual care",
    "3": "phase 3: compared with usual care in many people",
    "3-4": "phase 3–4: an approved medicine, compared further",
    "4": "phase 4: an approved medicine, studied further",
}
FILTER_PHASES = ("1", "2", "3", "4")

# The distances a search offers around a place, in km (their counts tell a person how far to go); 0 on
# the page: the whole country
RADII = (10, 25, 50, 100, 200)
DISTANCES = RADII + (0,)
# A place named with no distance
DEFAULT_RADIUS_KM = 50

# Lexemes a search document holds besides words, each one token the text parser never splits: a MeSH
# concept (zmeshd003924), a category (zcatcancer), a MedDRA code (zmeddra10012601)
MESH_TOKEN = "zmesh"
CATEGORY_TOKEN = "zcat"
MEDDRA_TOKEN = "zmeddra"
TOKENS = (MESH_TOKEN, CATEGORY_TOKEN, MEDDRA_TOKEN)
