"""When a trial's results are due, and whether they came on time: plain code, no LLM.

These are Bruckner et al.'s rules, as their R code applies them (ADR-001: reproduce their verdicts
first). The EU CTR gives sponsors one year from the end of the trial, six months for trials under
the paediatric regulation. Their code counts those periods two ways:

- picking which trials were due on the export day: calendar months (12 or 6 months before it);
- deciding whether results came on time: days (365 or 183 after completion).

Six calendar months can be 184 days, so a paediatric trial can be on time by one rule and not yet
due by the other. Kept as they are until an ADR decides our own rule.
"""

import calendar
from datetime import date, datetime

CHILD_AGE_GROUPS = ("In utero", "0-17 years")
MONTHS = {False: 12, True: 6}  # due-trial selection, by paediatric
DAYS = {False: 365, True: 183}  # on time or late, by paediatric


def parse_date(text: str) -> date | None:
    """A CTIS export date (dd/mm/yyyy); None for 'N/A' or empty."""
    text = text.strip()
    if text in ("", "N/A"):
        return None
    parsed = datetime.strptime(text, "%d/%m/%Y")
    return parsed.date()


def mentions_child_age_group(age_group: str) -> bool:
    """True when the CTIS age-group text names any of the child age groups."""
    for child_group in CHILD_AGE_GROUPS:
        if child_group in age_group:
            return True
    return False


def is_paediatric(age_group: str, has_pip: bool) -> bool:
    """Under the paediatric regulation: recruits children, or has a paediatric investigation plan."""
    if has_pip:
        return True
    return mentions_child_age_group(age_group)


def completion_date(end_date: date | None, global_end: date | None) -> date | None:
    """The later of the trial's end date and its global end; whichever exists if only one does."""
    known_dates = []
    if end_date is not None:
        known_dates.append(end_date)
    if global_end is not None:
        known_dates.append(global_end)
    if not known_dates:
        return None
    return max(known_dates)


def months_before(day: date, months: int) -> date:
    """Same day of the month, `months` earlier; 31 August minus 6 months is the end of February."""
    # Count months from year 0 (January = 0) so going back across a year boundary is one subtraction.
    months_since_year_zero = day.year * 12 + day.month - 1 - months
    year = months_since_year_zero // 12
    month = months_since_year_zero % 12 + 1
    # The same day may not exist in the earlier month (31 August -> February): use its last day.
    days_in_month = calendar.monthrange(year, month)[1]
    day_of_month = min(day.day, days_in_month)
    return date(year, month, day_of_month)


def is_due(completion: date | None, paediatric: bool, as_of: date) -> bool:
    """Results were due on `as_of`: the trial completed more than 12 (paediatric: 6) months before it."""
    if completion is None:
        return False
    deadline_start = months_before(as_of, MONTHS[paediatric])
    return completion < deadline_start


def verdict(*, cancelled: bool, extended: bool, reported_fully: bool, days_to_results: int | None,
            paediatric: bool) -> str:
    """Bruckner et al.'s legal_compliance_assessment for a due trial, in their order of precedence.

    `reported_fully` means a full scientific results document and a layperson summary; for now it
    comes from their reviewers (our evidence check will produce it later). `extended`
    comes from non-public CTIS data, so it can only be taken as given.
    """
    if cancelled:
        return "cancelled"
    if extended:
        return "extended"
    if reported_fully:
        if days_to_results is None:
            raise ValueError("a trial reported fully needs days_to_results")
        if days_to_results <= DAYS[paediatric]:
            return "reported fully on time"
        return "reported fully late"
    return "not fully reported"
