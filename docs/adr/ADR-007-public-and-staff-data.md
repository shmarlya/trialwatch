# ADR-007: What the public sees, and what only staff see

**Status:** accepted · **Date:** 2026-11-21 · **Session:** — · **Cards:** — · **Exam:** — · **Supersedes:** ADR-001's publication rule · **Amended by:** ADR-008 · **Revised:** 2026-11-22

> **The rule (revised 2026-11-22).** Every judgement without an error is published automatically by its run as
> the run finishes, whatever its label (full, not full or undecided), with its agreement with the gold datasets
> shown: the share of gold items that judgements of the same kind agreed with, once at least 20 were compared,
> and its basis (how many agreed of how many, from which datasets) always. Nobody publishes by hand and nothing
> waits for review. Staff (moderators and admins) may only withdraw a published judgement, with a reason; it
> stays public, marked withdrawn, with the reason and the date. This replaces ADR-001's 99% line and the 90%
> gate first chosen here.
>
> **Amended by ADR-008 (2026-11-22):** the page of a trial that recruits now is public whether or not it has a
> judgement (`view.recruiting`): who can join, what is tested, where it recruits.

## Question

Which data does TrialWatch show to anyone, and which only to its admins and moderators, now that the web interface can be served to the public?

## Context

- ADR-006 let anyone browse every page: unreviewed model judgements, their traces (prompts, raw answers), sources and transfers, gold labels with false-accusation flags, and staff emails in the history.
- A judgement says a sponsor did not report fully: public, it is an accusation. A model can misread a page, so a wrong one harms a sponsor and our credibility.
- The methodology should stay open: how we check (requirement sets, prompts, run settings) is what makes a result trustworthy.
- Staff are people: their emails are personal data and nobody outside needs them.
- ADR-001 proposed manual approval below 99% confidence; we have no calibrated confidence yet.
- Revision 2026-11-22: transparency comes first. If our check decides the evidence is not enough, that is published as it is, with how far checks of its kind can be trusted, not held back until a person agrees.

## Options

### A. Everything public, mark drafts

How it works: all pages stay open; unreviewed judgements carry a "draft" badge.

- **+** nothing to build
- **–** a draft accusation is still public and indexed
- **–** traces and emails stay public

### B. Publish step and a staff view

How it works: checks.publication records what is public; a withdrawn one stays public, marked.
Revised 2026-11-22: a run publishes every judgement it made without an error, any label, with how often judgements of its kind agreed with gold labels (the share once ≥ 20 items, the basis always). The public sees each published judgement with its whole trace; staff also see sources, gold, run tallies, costs and names.

- **+** everything we decide is open, with its method
- **+** a withdrawal can't hide a result
- **–** a misread page is a public accusation until staff withdraw it

### C. Sign-in for everyone

How it works: a viewer role; nothing is public.

- **+** simplest to reason about
- **–** defeats a public watchdog
- **–** accounts to manage for every reader

## Criteria × options

| Criterion | Option A | Option B | Option C |
|---|---|---|---|
| Harm: wrong accusation public | – drafts are public | – all public, agreement shown | + nothing public |
| Openness: method and results | + everything | + results and the whole trace | – nothing without an account |
| Privacy: staff names | – emails public | + shown as staff | 0 readers see them |
| Effort: build and run | + none | + one table, filters, no review | – accounts for readers |
| Undo: how hard to reverse later | 0 | + drop one table and view | 0 |

## Decision

We choose B, revised 2026-11-22 for transparency. Every judgement without an error is published by its run as it finishes, any label, however often its kind agreed with gold. A kind (model, prompt text, requirement set, label, quotes verified, asked again with every page, a question unanswered) is compared with gold labels; the publication records the share once ≥ 20 items were compared, and its basis always (checks/publishing.py). Nobody publishes by hand. Staff may withdraw one with a reason (judgement.withdraw); it stays public, marked withdrawn with the reason. Public (view.published): published judgements with their whole trace (questions, pages read, prompt and message sent, raw answers, quote checks, rules), their runs, prompts, requirement sets and the files they quote, listed on the home page with filters. Staff only: the full trial and sponsor lists, judgements of running or failed checks, sources and transfers, gold, run tallies, costs, the workflow and names. Decided by openness over harm: gold agreement is shown with each result, not used as a gate.

## Consequences

**Easier:** no review queue; everything we decide is public with its method, and a withdrawal can't hide a result.

**Harder:** a misread page is a public accusation until staff withdraw it; readers must weigh the gold agreement shown with each result.

## Revisit when · Lab

**Revisit when:** a kind with low gold agreement keeps publishing accusations; sponsors should be told before publication; a public API or export is wanted.

**Lab:** tests/test_publication.py (a run publishes, the public trace, a withdrawal stays public with its reason, staff-only pages, names, files) and test_publishing.py (every label published, agreement recorded).

## Session notes

Decided with the user on 2026-11-21: the public is anyone; the public sees verdict and quotes, not the trace; gold and sources are staff only; runs, prompts and history stay public. Then: approve by hand below 90% confidence, measured on gold; above it any label, accusations included, publishes by itself (replacing ADR-001's 99% proposal).

Revised with the user on 2026-11-22: publish everything, even judgements whose kind disagrees with gold; show the public the whole trace; keep withdraw, shown with its reason; drop /review. Replaced: the 90% gate, publishing by hand (judgement.publish), and hiding the trace from the public.
