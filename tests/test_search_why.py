"""Why a search found a trial (search/why.py), for the cases the search page tests don't reach: a word
found through the category it names, and a MeSH concept found under the trial's own, narrower one."""

from trialwatch.db.search import ConceptFound, TermFound
from trialwatch.search import query, why

TUMOUR = query.Term("word", "tumeur", "tumeur:*")
CANCER = query.Term("category", "Cancer", "zcatcancer", word="tumeur")
BRAIN = query.Term("word", "cerveau", "cerveau:*")
NEOPLASMS = query.Term("concept", "Neoplasms", "zmeshd009369", ui="D009369")


def test_a_word_found_by_its_category_and_one_in_the_criteria():
    asked = [TUMOUR, CANCER, BRAIN]
    found = {
        (1, 0): TermFound(1, 1, False, None, []),
        (1, 1): TermFound(1, 2, True, None, []),
        (1, 2): TermFound(1, 3, True, "d", []),
    }
    assert why._reasons(1, asked, found, {}) == [
        "“tumeur” filed under Cancer",
        "“cerveau” in who can join (its inclusion criteria)",
    ]


def test_when_the_words_did_not_find_it_the_concept_says_how():
    asked = [TUMOUR, BRAIN, NEOPLASMS]
    found = {
        (1, 0): TermFound(1, 1, True, "b", []),
        (1, 1): TermFound(1, 2, False, None, []),
        (1, 2): TermFound(1, 3, True, None, []),
    }
    narrower = ConceptFound(1, "D009369", "D001943", "Breast Neoplasms", "breast cancer")
    assert why._reasons(1, asked, found, {(1, "D009369"): narrower}) == [
        "“tumeur” in its title",
        "its condition “breast cancer” (Breast Neoplasms) is a kind of “Neoplasms” (MeSH)",
    ]
