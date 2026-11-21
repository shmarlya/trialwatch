# Bruckner et al.'s benchmark data

The four files TrialWatch reads from the study we measure ourselves against. They are a fixed
snapshot: the two CTIS exports were downloaded on **2025-11-10**, and CTIS only serves today's data,
so this copy cannot be reproduced by downloading it again.

## Source and permission

Bruckner T, Dike CE, Caquelin L, Freeman A, Aspromonti DA, DeVito NJ, Song Z, Karam G, Nilsonne G.
*Assessing Compliance with Reporting Requirements in European Phase II–IV Clinical Trials: A
Cross-Sectional Observational Study.* medRxiv preprint, 2026.
doi:[10.64898/2026.04.03.26350111](https://www.medrxiv.org/content/10.64898/2026.04.03.26350111v1) ·
protocol, data and code on [OSF](https://osf.io/sn4j2/overview).

The preprint is CC BY 4.0. The OSF files state no licence, and the authors gave their permission on
**2026-11-02** for us to publish the files we use, with credit. Please credit them the same way if
you reuse these files. The rows in the two CTIS exports are EMA data from the public
[CTIS](https://euclinicaltrials.eu) register, whose reuse is allowed when the EMA is credited.

## The files

| File | What it is | Size | Read by |
|---|---|---|---|
| `CTIS_trials_Phases_II-IV_20251110.csv` | CTIS public search export, every Phase II–IV trial: **7,547 trials**, 24 columns | 13 MB | the due-date check |
| `CTIS_trials_with_PIP_20251110.csv` | Same day's export of the trials with a paediatric investigation plan: **677 trials**. Decides paediatric status, which sets the 6- or 12-month results deadline | 1.5 MB | the due-date check |
| `2026-03-16_ctis_study_final_dataset.csv` | Their answer key: the final verdict for each of the **277 due trials**, 40 columns | 500 KB | the gold dataset, the due-date check |
| `ctis_merged_reviews_consolidated_with_comments.csv` | Each reviewer's answers per results document: **206 documents**, two reviewers each and a third for 37 | 396 KB | the gold dataset, the due-date check (posting dates, cancellations) |

The full 37 MB OSF deposit also holds their R pipeline, a Phase I export and intermediate outputs. We
don't read those, so they are not in git; get them from OSF if you need them.

The merged reviews file is not valid UTF-8. Our code reads its 749 broken byte sequences as U+FFFD
(�); only free-text comments are affected.

## Where each came from in the OSF deposit

Paths inside `CTIS data and code final/Data/Data and code/`, flattened into this folder:

| This file | OSF path |
|---|---|
| `CTIS_trials_Phases_II-IV_20251110.csv` | `ctis_project_step1_assessing_trial_registration_data/1_Input_datasets/` |
| `CTIS_trials_with_PIP_20251110.csv` | `ctis_project_step2_identifying_due_trials_from_public_ctis_data/1_Input_datasets/` |
| `2026-03-16_ctis_study_final_dataset.csv` | `ctis_project_step4_generating_final_results_dataset/2_Output/` |
| `ctis_merged_reviews_consolidated_with_comments.csv` | `ctis_project_step4_generating_final_results_dataset/1_Input_datasets/` |

SHA-256, so you can check this copy against the deposit:

```
f61346faf723d8dbaf7dca536b3c5bf6de1edbdeda980c2ba75cf1ca6bba8373  2026-03-16_ctis_study_final_dataset.csv
311152db50fa8f961946c66f25e511b3d036cbd1f165ed3dd43729cbd3b94667  CTIS_trials_Phases_II-IV_20251110.csv
328514c849f55c93e74f7dd2177f2e9ff9d4b3a9a3a16d3ab418c01a3c13f2ab  CTIS_trials_with_PIP_20251110.csv
840c646505c06ec82d28db35a24d59557624335d7c03781aa3569f50eb9786dc  ctis_merged_reviews_consolidated_with_comments.csv
```

## How TrialWatch reads them

Nothing loads these files into the database: src/trialwatch/gold/bruckner.py reads them where they
are, for two things.

**The gold dataset.** The two answer files are converted into the gold dataset version
`bruckner/2026-03-16`, written to data/gold/bruckner/2026-03-16/: one item per due trial (277), of
which the 206 with a reviewed results document carry the reviewers' consensus per part and a label.
The web interface's Gold page searches it, and `trialwatch gold compare --run N` scores a check run
against it. Build it on the host (the app container sees data/ read-only):

```bash
uv run trialwatch gold build bruckner
```

The version is locked in data/gold/versions.lock: rebuilding it from these files gives the same
bytes (a test checks that), and a released version is never overwritten. Their reviewers read the
summaries of results posted before the exports of 2025-11-10 (`evidence.as_of` in its
dataset.json), so `gold compare` sets a trial aside when our check read a document posted on or after
that day: ours and theirs may then be different documents.

**The due-date check.** tests/test_due_date.py runs our due-date rules
(src/trialwatch/checks/due_date.py) over all 7,547 trials of the export, with the PIP export
deciding which are paediatric, and checks that they reproduce all 277 of their verdicts, trial by
trial (ADR-001).
