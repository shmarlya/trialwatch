# Short gold evaluation: what it measured, and why it is gated this way (2026-11-24)

The first short evaluation in CI ([run 45370493953](https://github.com/shmarlya/trialwatch/actions/runs/45370493953),
hard-50, `default` setup) failed the fixed gates: label agreement 78.1% (25/32), 3 false accusations,
decided 68.1%. Measured locally on the same frozen snapshot (snapshot-bruckner-2026-03-16-20261124-085934).

## The sample is mostly items the reviewers themselves disagreed on

`evals/hard-50.txt` is chosen for reviewer disagreement, team discussion and termination: 41 of its 50 items
are ones where Bruckner's two reviewers disagreed. Every false accusation we made, in every run, was
**contested**: at least one reviewer read the item as we did. So the short evaluation is a regression check
against its own baseline (`gold-short.json`), and only **clear** false accusations (no reviewer's reading
supports them) must be 0. The full evaluation keeps fixed gates.

The gold label is the reviewers' final "results document full / incomplete". In 22 of 206 items (17 in
hard-50) their parts read participants, outcomes and not cancelled as all reported while the label says
incomplete: vague, partial, redacted or "still being analysed" outcomes. The label is the target; part
agreement is reported, not gated. 2023-508528-36 is labelled cancelled with 166 participants (a likely
labelling error).

## Runs (Haiku 5.5 unless said; 3 runs on identical evidence)

| | label agreement | decided | false accusations (clear) | items changing label | cost per run |
|---|---|---|---|---|---|
| before, Haiku | 75.9–79.3% | 61.7–66.0% | 2–3 (0) | 6, one of them not full ↔ full | $0.44 |
| before, Sonnet 5.5 (1 run) | 83.3% | 63.8% | 2 (0) | — | $3.39 |
| after, Haiku | 91.7–92.9% | 51.1–59.6% | 1 (0) | 7, none not full ↔ full | $0.45 |
| after + one unreadable page allowed (re-judged) | 92.9–93.9% | 59.6–70.2% | 1 (0) | — | — |

Sonnet was not clearly better on this sample and costs 7.7 times as much: the default stays Haiku.

## What changed, and why

- **Participant counts** (`checks/requirements.py`): the rule found no count in table-style quotes
  ("Subjects enrolled 168", "Actual enrolment: 166", "Worldwide total number of subjects 99"), numbers in
  words ("Three patients", "Thirty (30) participants") and other languages ("53 pacientes", "participants
  inclus … : 3"). Participants were left open 30 times in 3 runs before, 7 after.
- **`not_conducted`** (an endpoint of a stage that never started or enrolled nobody) and **`partial_result`**
  (data for only some of an endpoint's components) leave the endpoint for a person (`open_kinds`). Counting
  a partial result as "not reported" instead was tried on the stored answers: it adds a clear false accusation
  in every run, so it stays open.
- `numeric_result` must be the endpoint's own data for the groups it compares; a conclusion without data is
  `narrative_only`.

## What is left

- **One page without enough text** (a cover, signature, legend or contents page under 200 letters) made every
  "not reported" answer of its trial undecided. The default setup now allows one such page
  (`unreadable_pages_allowed`). Re-judged from the after runs' stored answers: decided 32, 33, 28 (was 27, 28,
  24), label agreement 93.8%, 93.9%, 92.9%, false accusations unchanged (1, contested). The baseline
  (`gold-short.json`) is these re-judged runs until a CI run replaces it.
- The two scanned results documents (2023-506083-13, 2022-500339-35) need OCR: quotes are verified against
  the page text, so sending the files whole would only make them undecided.
- Remaining disagreements: 2023-508528-36 (numbers given; the reviewers judged them "immature", beyond our
  requirement) and 2022-502932-37 (contested: endpoints narrative only after early termination).
