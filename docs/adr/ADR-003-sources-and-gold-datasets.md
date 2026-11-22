# ADR-003: Source-agnostic trials, documents and gold datasets

**Status:** accepted · **Date:** 2026-11-18 · **Session:** S3 · **Cards:** 03 · **Exam:** F1 · P1 · **Supersedes:** none · **Superseded by:** none

## Question

How do we store trials, documents and gold labels so that sources can change, nothing is stored twice, every change is traceable, and a check can be evaluated against any gold dataset?

## Context

- Everything is CTIS-shaped today: `ctis.*` tables, CTIS document types, CTIS snapshots. A trial from ClinicalTrials.gov, a journal publication or a document added by hand has nowhere to go.
- Checks read only the CTIS summary of results, but a trial's results can be in any document linked to it.
- Bruckner et al.'s labels sit in the working database (`gold.bruckner_*`) and the workflow selects trials with `--reviewed`. They are our benchmark, to be used only in tests, CI and after changes to retrieval, prompts, model settings or code, and the benchmark itself may grow or be replaced.
- The same PDF is already stored under several documents (e.g. a lay summary and a summary of results with the same bytes); the same trial may come from several registries.
- Nobody can tell who added a trial, a document or a link, or when. Judgements by a person (ADR-001: undecided goes to a person) have no home.
- Comparing our check with reviewers who read different documents produces false 'false accusations', the costliest error (ADR-001).

## Options

### A. A schema per source

How it works: keep `ctis.*`, add `ctgov.*`, `pubmed.*`, `manual.*`, `gold.<dataset>_*`; checks and scoring join across them.

**Pros:**

- no redesign now
- each source keeps its own shape

**Cons:**

- every check, page and score grows a branch per source
- duplicates across sources stay invisible
- each gold dataset needs its own scoring code

Mark (current state)

### B. Central model + source adapters

How it works: central trial, document and gold tables; one adapter per source fills them; sources keep their raw records beside them; Postgres writes an audit log.

**Pros:**

- checks, pages and scoring see one shape
- identity and duplicates handled once
- a new source or gold set is an adapter, not a rewrite

**Cons:**

- a schema rebuild now
- identity matching must be designed carefully

Mark (proposal)

### C. Generic JSON store

How it works: one table of JSON documents keyed by (source, key, kind); views and code interpret them.

**Pros:**

- any source fits without migrations
- quick to start

**Cons:**

- no constraints: duplicates and broken links are allowed
- queries and checks parse JSON everywhere
- the audit and versioning rules live only in code

## Criteria × options

| Criterion | Option A | Option B | Option C |
|---|---|---|---|
| Quality: right trial, right documents | – per-source branches miss linked documents | + every linked document, one identity per trial | – nothing stops broken links |
| Cost: money, tokens, time per run | 0 | 0 | 0 |
| Risk: false accusations, lost data | – reviewers and check may read different documents | + evidence scope fixed per gold dataset; nothing deleted | – duplicates and silent overwrites possible |
| Effort: to build and to maintain | + now, – for every source later | – now, + for every source later | + now, – for every query later |
| Undo: how hard to reverse later | 0 | – schema rebuild (local data reset anyway) | 0 |
| Traceability: who added what, when | – none | + audit log by triggers, actors, versions | – only if code remembers |

## Decision

We choose B, central model + source adapters, because it is the only option that gives every check all documents linked to a trial (quality), lets reviewers and check read the same evidence (risk, ADR-001), and records who did what (traceability). Its cost is a one-off rebuild, which we take now while the local data is reset anyway.

1. Sources: a source table (registry, publication index, manual, gold); every stored record carries (source, the source's own key), unique, so re-fetching versions it instead of duplicating it.
2. Trials: trial = our id; trial_identifier (scheme, normalised value) is unique and points to one trial; trial_record = what one source says, versioned. Ingest matches all identifiers: none → new trial, one trial → attach, several → a possible duplicate for a person; merges are recorded and reversible.
3. Documents in three layers: file (bytes, by sha256, stored once) → document (logical, identity = source key) → document version (replaced files kept, dated). Same text in different bytes is flagged by a page-text fingerprint. Trial ↔ document links carry role, basis (listed by the registry, identifier in the text, by hand), source and confidence; a check reads the current version of every linked document and records which.
4. Audit: actors (people and processes); an append-only audit log written by Postgres triggers in the same transaction; no update in place or hard delete without an audit entry.
5. Gold datasets: dataset version (name, version, source, evidence scope) and items naming trials by identifier, with labels, comments and subsets (dev/test). An evaluation = dataset version + subset + pipeline configuration; it runs in its own database built from a bundle (trials, files, labels), so CI is reproducible and the working database never holds benchmark data. CI replays stored model answers on every push and makes real model calls when prompts or model settings change, or by hand. Bruckner et al. is the first gold source.

## Consequences

**Easier:** adding a registry, publications or manual entry (an adapter); a new or larger gold dataset (a converter + its evidence scope); fair evaluation; seeing who added what; CI on fixed inputs; manual judgements next to automatic ones.

**Harder:** one schema rebuild now and a local database reset; identity matching needs care and a person for conflicts; bundles (~300 MB of PDFs for Bruckner et al.) live outside git; every write path must set an actor.

## Revisit when · Lab

**Revisit when:** identity matching produces many conflicts a person can't keep up with; a source can't be expressed as trial records + documents; evaluation bundles grow too large to rebuild per CI run.

**Lab:** migrations for sources, actors, audit, trials and documents; the CTIS adapter; `trialwatch eval` on the Bruckner bundle with zero new false accusations on its test subset.

## Session notes

Decided with the user on 2026-11-18: back up the local database (pg_dump) and start from scratch; gold data only in evaluation databases; manual check = a person's judgement and starting an automatic check by hand; every automatic check keeps a step-by-step trace for the web interface. Rejected: merging duplicate trials automatically when identifiers conflict (a wrong merge mixes two trials' results).
