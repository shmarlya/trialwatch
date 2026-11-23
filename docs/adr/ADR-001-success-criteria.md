# ADR-001: Match Bruckner's reviewers (first: publish at 99% verdict confidence)

**Status:** accepted · **Date:** 2026-11-01 · **Session:** S1 · **Cards:** 01 · **Exam:** P1 · P6 · **Supersedes:** none · **Publication rule replaced by:** ADR-007 (2026-11-22)

> **Publication, as decided since (ADR-007, 2026-11-22).** The 99% line and the manual approval below it are
> gone. Every judgement without an error is published automatically when its run finishes, whatever its label
> (full, not full or undecided), with its agreement with the gold datasets shown next to it: the share of gold
> items that judgements of the same kind agreed with, once at least 20 were compared, and in words how many
> agreed of how many, from which datasets. Nobody approves or publishes by hand. Staff (moderators and admins)
> may only withdraw a published judgement, with a reason; it stays public, marked withdrawn, with that reason.
> ADR-008 amends ADR-007 for recruiting trials: their pages are public whether or not they have a judgement.
>
> **Still standing from this ADR:** our results are measured against Bruckner et al.'s reviewers, and every
> disagreement is explained (our bug, data changed since their review, or a likely labelling error). That gate
> now runs as the gold evaluations in CI, against `evals/gates.json`. The text below is the decision as taken
> on 2026-11-01.

## Question

For the results check, when are our verdicts good enough, and which verdicts may be published without a person approving them?

## Context

Bruckner et al. reviewed CTIS as exported on 2025-11-10 (7,547 Phase II–IV trials, now seeded in ctis.search_export_snapshot). Of 234 trials legally required to report: 116 reported fully on time, 20 late, 98 not fully reported. Of the 234, 140 had a complete results document, 47 an incomplete one, 47 none. Two people checked each of 206 results documents (participants reported, outcomes reported, cancelled); they first agreed on 163 and resolved the rest. Their "extended deadline" flags came from non-public CTIS data. A wrong "not fully reported" accuses a named sponsor in public. An LLM's own confidence number is not calibrated out of the box.

## Options

### A. Match the reviewers, then publish at ≥ 99% confidence (chosen)

How it works: run the workflow on the 2025-11-10 snapshot until every disagreement with the reviewers is explained. Each verdict carries an internal confidence; below 99% it needs manual approval.

**Pros:**

- a published, human-made bar to measure against
- unsure verdicts wait for a person
- the 99% line is set on real labels

**Cons:**

- results documents must be fetched as they were
- 47 incomplete documents is a small test set

### B. A person approves every verdict

How it works: nothing is published until someone has approved it, whatever the confidence.

**Pros:**

- no unreviewed verdict is ever published

**Cons:**

- review work grows with every trial
- confidence is never put to the test

### C. One threshold, no manual approval

How it works: publish verdicts at or above a threshold; drop everything below it.

**Pros:**

- no review work at all

**Cons:**

- borderline cases vanish silently
- one wrong threshold puts errors in public

## Criteria × options

| Criterion | Option A | Option B | Option C |
|---|---|---|---|
| Quality: verdicts we can stand behind | + measured against human reviewers | + every verdict seen by a person | – nothing checked by a person |
| Cost: my review time per week | + only verdicts under 99% | – every verdict | + none |
| Risk: harm from a wrong verdict | + low; unsure verdicts wait | + low, if review keeps up | – wrong verdicts go public |
| Effort: to build and maintain | 0 comparison + approval queue | 0 approval queue | + one threshold |
| Undo: how hard to change later | + the 99% line is a config value | 0 dropping approval is a rework | 0 adding approval is a rework |
| Learning: what it teaches us | + calibration on real labels | – confidence never measured | – no feedback loop |

## Decision

We choose A. Development gate: run our workflow on Bruckner's 2025-11-10 snapshot and compare with the reviewers' final labels, per trial and per part (participants, outcomes, cancelled), until every disagreement is explained as our bug (fixed), data that changed after their review (set aside) or a likely labelling error (noted). Extended deadlines come from Bruckner's flag, because they used non-public data.

Each verdict carries a verdict confidence from 0 to 100%: our model's internal certainty about that one trial's verdict. At 99% or above it may be published; below 99% it needs manual approval. The 99% line is set during this development: verdicts we score at 99% or above must match the reviewers at least 99% of the time. Every verdict stores its decision record: verdict; confidence; per-part results with quote and page; threshold and config version; model id and prompt version; a short reason.

## Consequences

**Easier:** We measure progress against one published, human-made answer key. Moving the publish line is a config change. Review time goes only to verdicts under 99%. Any verdict can be explained from its own record.

**Harder:** Results documents must be fetched as they were when the reviewers read them. With 47 incomplete documents, Bruckner's data alone can't prove the 99% line. Confidence must be re-checked whenever the model or prompt changes.

## Revisit when · Lab

**Revisit when:** Verdicts at 99% or above match the reviewers or manual approvals less than 99% of the time. Or a published verdict turns out wrong. Or a second labelled dataset disagrees with Bruckner's reviewers. Or the publication policy (S13) changes who approves what.

**Done:** their export, PIP list, final verdicts and per-part reviews loaded (ctis seed, gold bruckner, gold bruckner-reviews); our due-date rules reproduce all 277 verdicts (gold compare-due). Accepted 2026-11-01. The per-part comparison and setting the 99% line on real confidence scores move to S8's lab, once the results-document check exists.

## Session notes

The reviewers first agreed on 79% of documents, so 100% raw agreement isn't expected; the 43 they argued about are good tests for low confidence. Rule of three: showing ≤ 1% error takes about 300 correct high-confidence verdicts in a row, more than Bruckner's set, so manual approvals keep adding labels until then. How the confidence is produced is decided in S5 and S7.
