# ADR-009: Model calls in Message Batches, at half price

**Status:** proposed · **Date:** 2026-11-27 · **Session:** S11 · **Cards:** 18 · **Exam:** F3 · P3 · **Supersedes:** none · **Superseded by:** none

## Question

Should TrialWatch's paid model calls (evidence checks, AI tags, gold evaluations) go through Anthropic's Message
Batches API instead of direct calls?

## Context

Every call went straight to `messages.create` at list price, trial by trial, inside a job. No one waits on these
answers: the refresh, schedules and a person's "check these trials" all run as background jobs. Anthropic's
Message Batches API bills every token at half price (cache reads and writes included) and answers within 24 hours,
most batches within an hour. A batch can't ask for the server-side refusal fallback. The worker runs one job at a
time and exits when a job stops moving for `TRIALWATCH_STUCK_MINUTES`. A text check makes a second call (widening)
that depends on the first call's answers. Measured cost per trial: $0.016 to $0.068 (evals/research/undecided-2026-11.md).

## Options

### A. Batches by default, rounds by replay (current proposal)

How it works: a check or tagging job sends its trials' calls as one batch and waits (job status `waiting`, run
status `waiting`); the worker does other jobs and asks Anthropic about the batch now and then. Once it has ended,
the job goes through its trials again with `llm.BatchReplay` in place of the client: a trial whose calls are all
answered is judged and saved by the same code as a direct run; a trial whose next call (widening) is unanswered goes
into the next batch. A refusal in a batch is asked again directly with the fallback. A person can still ask for a
check now, at list price (`mode: direct`).

- **+** half the cost of every check and tagging, with the same judgements, records and traces
- **+** one check code path: the replay drives the unchanged per-trial steps
- **–** answers arrive within hours, not minutes; two rounds for widening

### B. Direct calls, as before

- **+** answers at once
- **–** twice the cost of work nobody waits for (card 18's pitfall)

### C. Batches, with the check split into explicit steps

How it works: separate prepare / after-first-call / after-widening functions, their state serialised between rounds.

- **+** nothing is computed twice
- **–** a second copy of the check's control flow to keep in step with the first; more state to keep

## Criteria × options

| Criterion | A | B | C |
|---|---|---|---|
| Quality | 0 same model, prompt and rules | 0 | 0 |
| Cost | + half | – list price | + half |
| Risk | 0 a changed trial re-asks (its unused answer is recorded) | + none | – two flows can drift apart |
| Effort | + small: the check code is unchanged | + none | – large |
| Undo | + `mode: direct` in a setup | + | – |
| Latency | – up to 24 h (usually < 1 h), two rounds for widening | + minutes | – same as A |

## Decision

We choose A: batches are half the cost for work nobody waits on, and replaying the unchanged check keeps one code path
(Effort, Risk). Latency is acceptable for scheduled work; a person who needs answers now ticks "now, at list price".

## Consequences

**Easier:** the monthly cap covers twice the trials; tags and checks cost half; core.llm_call says which calls were
batched (`tier`, `llm_batch_id`, `custom_id`) and each attempt keeps the price it paid.

**Harder:** a check job ends hours after it starts; a round recomputes retrieval for trials it replays; a trial whose
evidence changed between rounds asks again (its first answer is recorded as unused, billed all the same); the cap is
checked against an estimate before each batch, since a batch's cost is known only when answered.

## Revisit when · Lab

**Revisit when:** prompt caching is added (it stacks with the batch discount); answers become needed within minutes;
Anthropic changes batch pricing or limits.

**Lab:** `tests/test_batches.py`, `tests/test_jobs.py` (waiting jobs): a batched check judges like a direct one at half
the cost. Pending: one live 3-trial run, batched against direct, labels and cost compared.

## Session notes

- Answers are fetched again from Anthropic on every round (kept 29 days there), so nothing large is stored here.
- A request's custom id is the sha256 of the request as sent: the same request asked again finds its answer.
- Errored or expired requests are sent again, three sends in all; then the trial fails with an error a later
  attempt may fix.
- Gold evaluations call directly by default (CI waits on them); `trialwatch gold eval --batch` for large local runs.
