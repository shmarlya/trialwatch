"""Reading a search query (ADR-008): places, countries, postcodes, ages, healthy volunteers and the
condition words, by rules and a gazetteer; and the tsquery a reading becomes."""

import pytest
from places import load_places
from vocabulary import load_mesh

from trialwatch.db import core
from trialwatch.search import query
from trialwatch.search.lookups import DatabaseLookups


@pytest.fixture
def lookups(conn, tmp_path):
    core.act_as(conn, "test-person")
    load_places(conn, tmp_path)
    load_mesh(conn, tmp_path)
    return DatabaseLookups(conn)


def read(text, lookups):
    return query.parse(text, lookups)


def test_a_condition_and_a_city(lookups):
    reading = read("diabetes Paris", lookups)
    assert reading.condition_words == ["diabetes"]
    assert (reading.place.name, reading.place.country_code, reading.radius_km) == ("Paris", "FR", 50)
    assert reading.concepts == [("D003919", "Diabetes Insipidus"), ("D003920", "Diabetes Mellitus")]
    assert query.describe(reading) == ["diabetes", "near Paris, France", "within 50 km"]
    assert query.tsquery_text(reading) == "(((diabete:*))) | zmeshd003919 | zmeshd003920"


def test_other_languages_and_names_of_a_place(lookups):
    lyon = read("cancer du sein Lyon", lookups)
    assert lyon.condition_words == ["cancer", "sein"] and lyon.place.name == "Lyon"
    assert lyon.categories == ["cancer"]
    assert query.tsquery_text(lyon) == "((cancer:*) | zcatcancer) & ((sein:*))"
    munich = read("Alzheimer München", lookups)
    assert munich.condition_words == ["alzheimer"] and munich.place.name == "Munich"
    vienna = read("healthy volunteers in Wien", lookups)
    assert vienna.healthy_volunteers is True and vienna.condition_words == [] and vienna.place.name == "Vienna"
    assert query.tsquery_text(vienna) is None
    child = read("mucoviscidose enfant", lookups)
    assert child.condition_words == ["mucoviscidose"] and child.age_group == "0-17" and child.place is None
    assert query.describe(child) == ["mucoviscidose", "anywhere in the EU/EEA", "children (0–17)"]


def test_postcodes_countries_ages_and_phases(lookups):
    paris_13 = read("breast cancer 75013", lookups)
    assert (paris_13.place.kind, paris_13.place.name) == ("postcode", "75013 Paris 13")
    assert paris_13.concepts == [("D001943", "Breast Neoplasms")]
    germany = read("asthma Allemagne", lookups)
    assert germany.country == ("DE", "Germany") and germany.place is None and germany.radius_km is None
    assert query.describe(germany) == ["asthma", "in Germany"]
    aged = read("diabète 45 ans à Paris", lookups)
    assert aged.age == 45 and aged.condition_words == ["diabète"] and aged.place.name == "Paris"
    phased = read("phase III asthma age 70", lookups)
    assert (phased.phase, phased.age, phased.condition_words) == ("3", 70, ["asthma"])
    # a place named with its country: the place says where in it
    both = read("asthma Paris France", lookups)
    assert both.place.name == "Paris" and both.country is None


def test_a_word_that_is_a_condition_and_a_place(lookups):
    """Marburg is a virus disease in MeSH and a German town: a condition unless "near" comes first."""
    disease = read("Marburg", lookups)
    assert disease.condition_words == ["marburg"] and disease.place is None
    assert disease.alternatives[0].label == "near Marburg, Germany"
    town = read("asthma near Marburg", lookups)
    assert town.place.name == "Marburg" and town.condition_words == ["asthma"]
    # Lyme Regis is outside the EU/EEA and small: Lyme stays a word; Nice is a city
    assert read("Lyme", lookups).place is None
    assert read("Nice", lookups).place.name == "Nice"
    # two Frankfurts: the larger, and the other offered
    frankfurt = read("asthma Frankfurt", lookups)
    assert frankfurt.place.name == "Frankfurt am Main"
    # a place's own name before a larger place's old name in another language
    core.act_as(lookups.conn, "test-person")
    lookups.conn.execute("INSERT INTO geo.place VALUES (3195506, 'Maribor', 'SI', 95000, 46.55, 15.65)")
    lookups.conn.execute("INSERT INTO geo.country VALUES ('SI', 'SVN', 'Slovenia', '{sl}', true)")
    lookups.conn.execute("INSERT INTO geo.place_name VALUES (3195506, 'marburg'), (3195506, 'maribor')")
    assert read("asthma near Marburg", lookups).place.name == "Marburg"


def test_the_tsquery_holds_only_letters_and_digits_of_words(lookups):
    hostile = read("x') | (y & !z:* <-> tumours", lookups)
    text = query.tsquery_text(hostile)
    # "y" and "z" are stop words (Spanish "and", Polish "with"); what is left: letters, a prefix, a category
    assert text == "((x)) & ((tumour:*) | zcatcancer)"
    # any of the words, when all of them find nothing; numbers and short words don't count alone
    assert query.tsquery_text(read("asthma tumours", lookups), relaxed=True) == "((asthma:*)) | ((tumour:*) | zcatcancer)"
    assert query.tsquery_text(read("asthma typ 2", lookups), relaxed=True) == "((asthma:*))"
    assert query.words_of("x" * 300) == ["x" * 200]
    assert len(query.words_of("a " * 40)) == query.MAX_WORDS


def test_two_boxes_read_the_condition_and_the_place_apart(lookups):
    reading = query.parse_parts("Marburg", "Paris", lookups)
    assert reading.condition_words == ["marburg"] and reading.place.name == "Paris"
    unknown = query.parse_parts("asthma", "Atlantis", lookups)
    assert unknown.place is None and unknown.notes == ["no place called “atlantis” in the EU/EEA"]
