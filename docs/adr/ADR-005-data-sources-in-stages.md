# ADR-005: Data sources in stages: registry, list, evidence

**Status:** accepted · **Date:** 2026-11-21 · **Session:** S3 · **Cards:** — · **Exam:** — · **Supersedes:** none · **Superseded by:** none

## Question

How do trials get from a registry into TrialWatch so that a person chooses what to read, every source works the same way, and a check never runs on evidence that only partly arrived?

## Context

- A sync took EU CT numbers a person typed and fetched each trial's record and documents in one step. CTIS was the only source with code; which documents to fetch was a command-line flag.
- CTIS lists 12,905 trials (POST /search, up to 500 per request): the whole list takes about 30 requests. The evidence does not: a record and its PDFs are several requests per trial at one per second, then the page text must be read.
- A check that reads a trial whose documents did not all arrive, or could not be read, would call missing evidence "not reported": a false accusation.
- Other registries (ClinicalTrials.gov) and trials entered by hand must fit the same flow. Nothing is in production: one current config per source, runs log the one they used (ADR-004's rule).

## Options

### A. One step, typed numbers

How it works: a person pastes EU CT numbers; a sync fetches the record and the documents and reads their pages; checks run on whatever has pages.

**Pros:**

- simple
- already built

**Cons:**

- no view of what a source holds
- CTIS-only code and flags
- a partly failed sync still looks ready for a check

Mark (current state)

### B. Registered sources, two stages

How it works: core.source names the adapter that reads a source and holds its config (what to list, what evidence to transfer). Syncing the list makes every listed trial a trial here (key, title, identifiers). Transferring evidence fetches a chosen trial's record and configured documents and reads their pages, recorded as complete or failed. Checks run only on complete trials.

**Pros:**

- browse and choose before paying for downloads
- one flow for every source
- no check on missing evidence
- stale evidence is visible

**Cons:**

- the whole list is stored before any trial is read
- more tables and states

Mark (proposal)

### C. Live search, sync what is ticked

How it works: the web interface searches CTIS live; the trials ticked there are synced in one step, as in A.

**Pros:**

- nothing stored until chosen

**Cons:**

- a CTIS-only screen
- no "changed since" or "no longer listed"
- still no completeness check before a check

## Criteria × options

| Criterion | Option A | Option B | Option C |
|---|---|---|---|
| Quality: checks read all the evidence | – partial syncs look ready | + checks only on complete | – as A |
| Risk: false accusations | – a missing PDF reads as absent | + not checked until complete | – as A |
| Effort: to build and to maintain | + nothing now | – registry, two stages, pages | 0 one page |
| Traceability: why a trial came in | – typed numbers only | + list and transfer runs, config | 0 the search is not kept |
| Undo: how hard to reverse later | 0 | + tables drop, the flow stays | 0 |

## Decision

We choose B: every source is registered with an adapter and a config, and trials come in in two stages; a check runs only on a trial whose evidence came in completely. B is the only option that keeps checks off missing evidence (risk, quality) and records why each trial came in (traceability).

1. Registry: core.source gains adapter (code in src/trialwatch/sources/registry.py), enabled and config (jsonb, checked by the adapter). CTIS: listing.criteria (its search fields) and page_size; evidence.document_kinds. Manual is a source with no list.
2. Sync the list (`source sync`): page through the source's list in key order; each trial is matched by its key and identifiers, or created; core.source_listing keeps its title, the day the source last changed it and the listed row. A complete pass marks the trials no longer listed (kept).
3. Transfer evidence (`source transfer`): for chosen trials (keys, or pending / failed / stale / listed): the record, the configured documents and their page text. core.evidence_transfer says complete, or failed and why. Stale: the source changed the trial since; still checkable, and shown.
4. Only complete trials are checked: evidence.run, the CLI and the web interface refuse the others (core.ready_trials). A trial entered by hand is complete once its uploads are read.
5. Both stages are ingest runs (kind list or transfer) that log the config they used; a job can be stopped between trials. `ctis sync` is gone.

## Consequences

**Easier:** choosing trials from the whole registry; a new registry is one adapter class; seeing per trial what is missing or stale; trusting that a checked trial had all its evidence.

**Harder:** the list brings ~13,000 trials in before any is read (pages are paged); one PDF that fails blocks the trial's check until a transfer gets it.

## Revisit when · Lab

**Revisit when:** a source has no list (list it from a file); lists should sync on a schedule; one unreadable PDF should not block a check of the others.

**Lab:** tests sync a fake CTIS list, transfer, stop, go stale, fail a download and refuse a check; a real sync of CTIS's whole list is next.

## Session notes

Decided with the user on 2026-11-21: syncing a source means getting its list of trials (id, name) mapped into our trials; evidence is transferred as each source's config says; checks run only on trials whose evidence was transferred (downloaded and read). The live-search page (C) was proposed first and dropped.

Amended 2026-11-21 after a code review: stale evidence no longer counts as checkable (a sponsor's corrected document would be judged by its old copy). A check reads a trial only when every source that holds it has a complete, current transfer that brought the document kinds the check reads; a stale trial is transferred again first (now trials/readiness.py, not_ready).
