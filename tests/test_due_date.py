from datetime import date

import pytest

from trialwatch.checks.due_date import (
    completion_date, is_due, is_paediatric, months_before, parse_date, verdict,
)

AS_OF = date(2025, 11, 10)


def test_export_dates_parse_and_na_is_none():
    assert parse_date("17/03/2025") == date(2025, 3, 17)
    assert parse_date("N/A") is None and parse_date("") is None


def test_completion_is_the_later_date_or_whichever_exists():
    assert completion_date(date(2024, 12, 18), date(2025, 2, 12)) == date(2025, 2, 12)
    assert completion_date(date(2025, 3, 17), None) == date(2025, 3, 17)
    assert completion_date(None, date(2024, 9, 14)) == date(2024, 9, 14)
    assert completion_date(None, None) is None


def test_paediatric_means_children_recruited_or_a_pip():
    assert is_paediatric("0-17 years, 18-64 years", has_pip=False)
    assert is_paediatric("In utero", has_pip=False)
    assert is_paediatric("18-64 years, 65+ years", has_pip=True)
    assert not is_paediatric("18-64 years, 65+ years", has_pip=False)


def test_months_before_keeps_the_day_or_ends_the_month():
    assert months_before(AS_OF, 12) == date(2024, 11, 10)
    assert months_before(AS_OF, 6) == date(2025, 5, 10)
    assert months_before(date(2025, 8, 31), 6) == date(2025, 2, 28)
    assert months_before(date(2024, 8, 31), 6) == date(2024, 2, 29)


@pytest.mark.parametrize(("completion", "paediatric", "due"), [
    (date(2024, 11, 9), False, True),
    (date(2024, 11, 10), False, False),  # exactly 12 months before: not yet
    (date(2025, 5, 9), True, True),
    (date(2025, 5, 10), True, False),
    (date(2025, 5, 9), False, False),
    (None, False, False),  # no end date: never due
])
def test_due_when_completed_more_than_12_or_6_months_before(completion, paediatric, due):
    assert is_due(completion, paediatric, AS_OF) is due


def test_verdict_follows_their_order_and_day_limits():
    base = dict(cancelled=False, extended=False, reported_fully=True, paediatric=False)
    assert verdict(**base, days_to_results=365) == "reported fully on time"
    assert verdict(**base, days_to_results=366) == "reported fully late"
    assert verdict(**base | {"paediatric": True}, days_to_results=183) == "reported fully on time"
    assert verdict(**base | {"paediatric": True}, days_to_results=184) == "reported fully late"
    assert verdict(**base | {"reported_fully": False}, days_to_results=None) == "not fully reported"
    assert verdict(**base | {"extended": True}, days_to_results=900) == "extended"
    assert verdict(**base | {"extended": True, "cancelled": True}, days_to_results=None) == "cancelled"
    with pytest.raises(ValueError):
        verdict(**base, days_to_results=None)
