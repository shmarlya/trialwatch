# Why a third of the checks are undecided, and what would fix it

Research on the prod snapshot of 2026-11-25 (`data/pg-dump-all-1795643511-…gz`). It covers check
run 1: claude-haiku-5-5, text method, requirement set `eu-ctr-results`. The queries are in
[undecided-2026-11.sql](undecided-2026-11.sql). Gold is Bruckner 2026-03-16; 245 of its trials
were checked in run 1.

## The problem

| Label | Trials |
|---|---|
| full | 836 |
| not full | 291 |
| **undecided** | **597 (34.6% of labelled trials)** |
| failed (error) | 61 |

The gate (`evals/gates/full.json`) allows at most 20% undecided. On the gold trials we decide
71% of them, and the label agrees with gold on 89.9% of those we decide.

Which part is left open in the 597 undecided trials:
- **outcomes**: 493
- **participants**: 214
- **cancelled**: 95 (with participants also open in all but 3 of these)

Of the 56 gold-labelled trials we leave undecided, **gold says `full` for 44**. We abstain mostly
on trials that are in fact fully reported.

## Causes, by trials they block

An undecided trial has one or more open questions (1217 in all). In 313 trials a single rule
leaves every open question open. Fixing that rule alone decides the trial.

| Rule (question) | Trials affected | Trials it alone blocks | What it really is |
|---|---|---|---|
| verified partial_result: left for a person (endpoint) | 231 | **168** | composite endpoints, mostly safety: AE counts given, labs/vitals/ECG "no clinically meaningful findings" in words |
| the model doesn't know (endpoint) | 141 | 65 | retrieval misses in huge reports; broken text extraction |
| verified quote without a count (participants) | 111 | **69** | parser misses real counts |
| quote not on the cited page (endpoint, participants) | 68 + 39 | 48 + 15 | page off by 1–2, table text broken mid-number |
| cancelled: not found in the retrieved pages | 84 | 0 | document never "read whole" (see below) |
| verified not_reported kind in the retrieved pages only (endpoint) | 42 | 22 | same, never "read whole" |
| verified not_conducted: left for a person (endpoint) | 39 | 17 | stage never started |
| no registered primary endpoint | 11 | 9 | registry data |

The rule "no answer" (the model leaving out a question) never fires. That hypothesis is ruled out.

### 1. `partial_result` is left for a person, but gold calls it reported

The model's notes are consistent. A composite primary endpoint (typically "safety and
tolerability: AEs, labs, vital signs, ECG") has numbers for AEs. The other components are only
described in words, or are missing. Some PK endpoints have most parameters but not all.

In every gold trial that has an outcomes verdict where we said partial_result, **gold says outcomes
were reported (19 of 19)**. The Bruckner reviewers count a partial result as reported.

### 2. The participants count parser misses common forms

Of the 111 verified participant quotes with "no count":
- 30 are the EU CTR results form line **"Total: worldwide 31"**.
- 21 are "Label: N" forms ("Analyzed: 40", "Exposed in phase 1a: 21", "Belgium: 16").
- 13 use hyphenated number words ("Seventy-seven subjects", "Fifty-four subjects").
- 6 are tables with "n (%) 92 (100%)".
- 3 are text that lost its spaces in extraction ("Atotalof98participants").

All 13 gold trials here say participants were reported. Fixing `participant_numbers`
(`checks/requirements.py:333`) is safe: on gold it decides 9 more trials, all of them correctly.

### 3. Quotes fail verification for mechanical reasons

Of the 116 quotes rejected as "not on the cited page", in the cited file:
- 50 are on a page **1–2 away** from the one cited.
- 36 are on the cited page itself, but only when punctuation and spaces are ignored. These are
  mostly tables whose extracted text breaks numbers across lines ("469.\n49 (300\n.51"), which
  defeats the whole-word match in `checks/verify.py:80`.
- 13 are too short (bare numbers such as "354" or "33.3", quoted without their label).
- 17 diverge or cannot be found.

### 4. Huge documents are never "read whole"

241 checked trials have more than 10 pages under 200 letters. These are mostly clinical study
reports, averaging about 3,500 pages with appendices and listings; the largest has 64,141 pages.

`read_enough` allows 1 unreadable page, so these trials can never settle "not found" or
not_reported. On top of that, 124 trials exceed `max_text_chars` (1.6M), so widening is skipped
altogether; the median trial among them has 3,695 pages. 91 of the "don't know" endpoint
answers are in documents over 1,000 pages that were never widened.

The model's notes often name the table holding the result ("Table 14.2-10 Analysis of
Recurrence-Free Survival") that retrieval did not send.

Even in normal documents, two unreadable pages are common (a cover page and a full-page figure).
They switch off "read whole" for the entire trial.

### 5. Text extraction loses tables

The answer notes complain about blank pages, table cells without values, or "font-encoded glyph
codes" in 62 undecided trials, against 9 decided ones. pypdf (`trials/pages.py`) drops the values
of some CTIS/CT.gov results tables and image-only pages.

## What the fixes would do (re-applying the rules to the stored answers)

| Scenario | Undecided | Gold decided | Gold agreement |
|---|---|---|---|
| now | 597 (34.6%) | 139 | 89.9% |
| A: participant count parser | 521 (30.2%) | 148 | 90.5% |
| B: tolerant quote verification (upper bound: the 86 recoverable quotes count as verified) | 540 (31.3%) | 146 | 90.4% |
| A + B | 462 (26.8%) | 155 | 91.0% |
| A + B + partial_result counts as reported | **274 (15.9%)** | 175 | 89.7% |

A and B are code fixes that raise both decided share and agreement. Together with the
partial_result policy, the gate's 20% is met without any new model calls. The remaining
undecided trials then come mostly from huge reports and broken extraction (causes 4 and 5). Those
need a re-run to measure.

Agreement sits around 0.90 throughout. The 10 trials where we say `not full` and gold says `full`
were already there before any of these fixes. They are a separate problem, and they decide
whether the 0.90 gate passes.

## Recommended fixes, in order

1. **Participant count parser** (`checks/requirements.py` `participant_numbers`). Accept
   "Total: worldwide N", "<group label>: N" in a participants context, hyphenated number words,
   "n (%) N (100%)", and glued "total of N participants". Unit tests from the quotes above. Safe.
2. **Quote verification** (`checks/verify.py`):
   - Also try pages ±2 from the cited page, and accept the page where the quote is found.
   - Add a variant of the page text with line breaks inside numbers and words joined.
   - In `checks/prompts/evidence.md`, ask for the row or label with the number, so bare-number
     quotes stop happening.
3. **partial_result counts as reported**: this is a **policy decision for you**. It matches how
   the gold reviewers judged. Two ways to do it:
   - Move `partial_result` out of `open_kinds`, and treat a verified partial_result as reported
     (True) in `settle_question`.
   - Keep it open only when the missing part is the endpoint's main measure.

   `not_conducted` (17 trials alone) could follow the same reasoning: a stage that never started
   needs no result. Gold has too few cases (4) to judge it.
4. **"Read whole" for large documents**:
   - Count unreadable pages as a share of the pages rather than a fixed 1.
   - Widen in chunks when the text is over `max_text_chars`, instead of skipping.
   - Let a "don't know" that names a table trigger a lookup of the page carrying that table
     title, then re-ask.

   This needs model calls, so measure it on the gold short set first.
5. **Text extraction**: try a second extractor (pdfplumber or PyMuPDF) on pages where pypdf gives
   under 200 letters or broken tables. Use OCR only for image-only pages.

Each fix goes through the gold short eval and then the full gate, as `evals/baselines/gold-short.md` does.

## Not covered

- Fixes 4 and 5 change what the model sees. Their gain cannot be simulated from stored answers;
  they need a re-run on a sample (the gold trials plus about 200 prod undecided trials).
- The 61 failed judgements are errors, not undecided, and were not looked at here.
- The quote search covered only the cited file. Quotes that moved to another document of the
  same trial were not checked.

## Fixes made (branch `fix/fewer-undecided`), re-judged on run 1's stored answers

Fixes 1–3 above are made:
- **Count parser.** `participant_numbers` now reads "Total: worldwide N", table labels that don't
  name the people ("Analysed: 40", "Exposed in phase 1a: 21", "Started 149", "Full Analysis Set 14"),
  "n (%) 92", compound number words ("Seventy-seven"), French "personnes", and up to 4 words between
  the number and the people. 84 of the 164 prod quotes it missed are counted now, and none of the
  1,395 it counted are lost. Most of the rest correctly have no count ("no participants were enrolled").
- **Quote verification.** Spacing inside words and numbers is ignored, as long as the quote starts
  and ends on word edges. A quote 1–2 pages from the cited page in the same document counts, and
  the evidence then names that page. 73 of 123 rejected prod quotes verify now.
- **partial_result counts as reported.** It moves to a new `reported_kinds` field of the requirement
  set. `not_conducted` stays open.

To get these numbers, every judged trial of run 1 was re-judged with the new code: the stored answers,
the page text of the cited page ±2, and the rules, with no model calls. With the old code the
re-judge gives exactly the stored labels.

| | Before | After |
|---|---|---|
| full / not full / undecided | 836 / 291 / 597 | 1115 / 292 / 317 |
| undecided | 34.6% | **18.4%** (gate ≤ 20%) |
| gold trials decided | 139 / 195 (71.3%) | 173 / 195 (88.7%) (gate ≥ 80%) |
| label agreement with gold | 89.9% | 89.6% (gate ≥ 90%) |

279 trials move from undecided to full, and 1 to not full. None move between full and not full.

The new disagreements are 4 trials where we now say full and gold says not full. All four come from
partial_result. In each, gold's own parts say outcomes were reported (participants ✓, outcomes ✓,
cancelled ✗), but gold's label is the paper's overall verdict "not fully reported": a component given
only in words ("no significant changes in vital signs…"). The gold set is split on partial results,
about 17 full against 5 not full at label level. Counting them as reported is right far more often.
The converter taking the label from the verdict even where the parts say otherwise
(`gold/converters/bruckner.py`) is worth a look of its own.

Agreement was already just under 0.90 before these fixes. What decides that gate is the 10 trials
we call not full that gold calls full, which none of these fixes touch.

What still leaves trials undecided after the fixes (open questions in undecided trials):
- the model doesn't know: 319
- not found in the retrieved pages: 221
- not_conducted: 48
- quote not on the cited page: 44
- no count: 42

These are fixes 4 and 5 (large documents, text extraction), which need re-runs to measure.
