"""Runs our due-date rules on Bruckner et al.'s 2025-11-10 export and compares them with their
verdicts, trial by trial (ADR-001: develop until every disagreement is explained).

Inputs we can't compute yet are taken from them: whether the results document was full and had a
lay summary, and cancellations (their reviewers; our results-document check will replace these),
and extensions (non-public data, always taken as given).
"""

from dataclasses import dataclass, field
from datetime import date

import psycopg

from trialwatch.checks import due_date

SNAPSHOT_DAY = date(2025, 11, 10)
EXPORT_FILE = "CTIS_trials_Phases_II-IV_20251110.csv"
# Same day's export of trials with a paediatric investigation plan
PIP_FILE = "CTIS_trials_with_PIP_20251110.csv"


@dataclass
class Disagreement:
    ct_number: str
    field: str
    ours: object
    theirs: object


@dataclass
class DueComparison:
    trials: int = 0
    ended: int = 0
    due_ours: set[str] = field(default_factory=set)
    due_theirs: set[str] = field(default_factory=set)
    # field -> [trials compared, trials that agree]
    fields: dict[str, list[int]] = field(default_factory=dict)
    disagreements: list[Disagreement] = field(default_factory=list)

    def check(self, ct_number: str, name: str, ours: object, theirs: object) -> None:
        counts = self.fields.setdefault(name, [0, 0])
        counts[0] += 1
        if ours == theirs:
            counts[1] += 1
        else:
            self.disagreements.append(Disagreement(ct_number, name, ours, theirs))


def _export_rows(conn: psycopg.Connection, source_file: str) -> dict[str, dict]:
    return dict(conn.execute(
        "SELECT ct_number, payload FROM ctis.search_export_snapshot WHERE captured_on = %s AND source_file = %s",
        (SNAPSHOT_DAY, source_file),
    ).fetchall())


def compare_due(conn: psycopg.Connection) -> DueComparison:
    export, pip = _export_rows(conn, EXPORT_FILE), _export_rows(conn, PIP_FILE)
    theirs = {row[0]: row[1:] for row in conn.execute(
        """
        SELECT v.ct_number, v.verdict, v.results_document = 'full' AND v.lay_summary, v.days_to_results,
               v.payload->>'regulatory_status', v.payload->>'completion_date', v.payload->>'extended_not_due_yet' = '1',
               coalesce(d.payload->>'cancelled_final_data' = '1', false), d.results_posted_on
        FROM gold.bruckner_verdict v LEFT JOIN gold.bruckner_document d USING (ct_number)
        """
    )}
    if not (export and pip and theirs):
        raise ValueError(
            f"needs {EXPORT_FILE} and {PIP_FILE} (`trialwatch ctis seed`) and their verdicts "
            "(`trialwatch gold bruckner`, `gold bruckner-reviews`), or the dev seed"
        )

    result = DueComparison(trials=len(export), due_theirs=set(theirs))
    ours = {}
    for ct_number, row in export.items():
        if row["Overall trial status"] != "Ended":
            continue
        result.ended += 1
        paediatric = due_date.is_paediatric(row["Age group"], ct_number in pip)
        completion = due_date.completion_date(
            due_date.parse_date(row["End date"]), due_date.parse_date(row["Global end of the trial"])
        )
        if due_date.is_due(completion, paediatric, SNAPSHOT_DAY):
            result.due_ours.add(ct_number)
            ours[ct_number] = (paediatric, completion)

    for ct_number in sorted(result.due_ours | result.due_theirs):
        result.check(ct_number, "due", ct_number in result.due_ours, ct_number in result.due_theirs)
        if ct_number not in ours or ct_number not in theirs:
            continue
        paediatric, completion = ours[ct_number]
        (verdict, reported_fully, days_theirs, status, completion_theirs, extended, cancelled,
         posted_on) = theirs[ct_number]
        result.check(ct_number, "paediatric", paediatric, status == "paediatric")
        result.check(ct_number, "completion date", completion.isoformat(), completion_theirs)
        days = (posted_on - completion).days if posted_on else None
        if days_theirs is not None:
            result.check(ct_number, "days to results", days, days_theirs)
        result.check(ct_number, "verdict", due_date.verdict(
            cancelled=cancelled, extended=extended, reported_fully=reported_fully,
            days_to_results=days, paediatric=paediatric,
        ), verdict)
    return result
