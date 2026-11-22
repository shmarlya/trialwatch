# ADR-004: A generic check; requirements are data

**Status:** accepted · **Date:** 2026-11-19 · **Session:** S3 · **Cards:** 03 · **Exam:** F1 · P1 · **Supersedes:** none · **Superseded by:** none

## Question

How does the check decide what a trial must report and whether it is enough, without naming a registry, a document type or a regulation in its code or prompt?

## Context

- ADR-003 made trials and documents source-agnostic, but the check still assumed CTIS and Bruckner et al.: its prompt names CTIS and the "Summary of results (for publication)"; its questions (participants, cancelled, one per primary endpoint), their English search words and the rules for "enough" are code.
- A trial's documents can now come from any source: a registry's results summary, a study report, a journal publication, an upload. Other standards (US FDAAA, CONSORT for publications) ask for different things.
- We want every check to record what was used at that moment: the trial record and document versions, the prompt, the requirements, every call and decision.
- Nothing is in production yet: there is one current version of each prompt, rule and requirement set; only gold datasets are versioned (their labels must stay comparable). Runs log the exact texts they used.

## Options

### A. Keep requirements in code

How it works: questions, search words and rules stay in evidence.py; a new standard is new code.

**Pros:**

- no change now
- rules are plain Python

**Cons:**

- the check is tied to one standard
- changing "what counts" needs a programmer
- the prompt keeps naming the source

Mark (current state)

### B. Requirement sets as data files

How it works: `checks/requirements/<name>.json` lists what must be reported (questions, search hints, which documents to read) and how "enough" is decided, using a small vocabulary of rule types the code provides; the prompt is generic and the facts come from data.

**Pros:**

- one generic check for any standard
- readable, reviewable in git like prompts
- logged per run with its fingerprint

**Cons:**

- the rule vocabulary must cover each standard

Mark (proposal)

### C. Requirements in the database

How it works: tables of requirements edited in the web interface.

**Pros:**

- editable without a deploy

**Cons:**

- changes bypass code review and CI
- harder to keep in step with the code and tests
- another editor to build

## Criteria × options

| Criterion | Option A | Option B | Option C |
|---|---|---|---|
| Quality: any trial, any document | – one standard only | + any standard as a file | + any standard |
| Risk: false accusations | 0 | + rules reviewed and tested like code | – edits skip review and CI |
| Effort: to build and to maintain | + now, – per standard | 0 now, + per standard | – editor and sync |
| Traceability: what decided a label | – only the code version | + set and rule logged per run | 0 |
| Undo: how hard to reverse later | 0 | + files in git | – data migrations |

## Decision

We choose B: the evidence check is generic code, and what a trial must report is a requirement set, a data file in src/trialwatch/checks/requirements/. Option B keeps rules under review and tests (risk) while removing every registry and regulation from the code and the prompt (quality), and every run logs the exact set it used (traceability).

1. A requirement set names the document kinds to read, its requirements and how they combine into parts and a label. A requirement is fixed (one question) or one per item of the registered information (e.g. each primary endpoint), with a question template, search hints and a rule type from a small vocabulary in code (count stated, statement present, numeric result), plus the evidence kinds that mean "not reported".
2. The prompt is generic: find, for each requirement, the passage in these documents that answers it, quote it, say where and what kind of evidence it is. The trial, its documents (titles, kinds, sources) and the questions are given per call, from data.
3. Each judgement records its inputs at that moment: the trial record and the document versions read. Each run records the prompt and requirement texts it used, with their fingerprints. Parts are stored by name (jsonb), not as columns of one standard.
4. No versioning before production: one current prompt, rule set and requirement set (no .v1 files, no rules version). Gold datasets stay versioned. Runs keep the exact texts, so nothing is lost when a file changes. The first set, eu-ctr-results, reproduces today's questions and rules (Bruckner et al.'s reading of the EU Clinical Trials Regulation).

## Consequences

**Easier:** a new standard or document type is a file; reading what decided a label (set, rule, quote, page); comparing runs that used different sets; a prompt free of any source.

**Harder:** a standard whose rule doesn't fit the vocabulary needs a new rule type in code; until production, a changed file changes what the next run does (the runs before keep their own copy).

## Revisit when · Lab

**Revisit when:** we deploy to production (then version prompts and requirement sets); a standard needs rules the vocabulary can't express; non-English documents need search hints per language.

**Lab:** eu-ctr-results gives the same judgements as the code rules it replaces (tests); a run on real trials logs the set, prompt and inputs on its trace.

## Session notes

Decided with the user on 2026-11-19: remove versioning from prompts, rules and requirements while building v1; keep it for gold datasets. The words a quote is searched with come from each requirement's hints and question.
