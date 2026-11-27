# ADR roadmap for the AI decisions

The plan of 2026-11-01 for working through the [AI decision map](ai-decision-map.md): one session = one decision
record (ADR) + the lab that tests it. Session Sn writes `docs/adr/ADR-00n-….md`; card numbers refer to the map.
As of 2026-11-01: ADR-001 accepted (8 proposed · 11 open · 1 later · 1 accepted; the 8 proposed are the old
"decided" boxes of the architecture diagram).

> **Kept as a record.** Decisions taken since are in the ADRs, which win over this plan. In particular the 99% line
> of S1 and S8 is gone: every judgement without an error is published with its gold agreement, and staff may only
> withdraw one ([ADR-007](ADR-007-public-and-staff-data.md)). Sessions S3–S5 and S9 were built without their own
> ADRs (gold datasets and gates: ADR-003; the generic check and quote verification: ADR-004).

## How one ADR session runs

1. Read the card on the map; learn its TERMS.
2. Write the question in one sentence.
3. List 2–3 real options. The current proposal is only one of them.
4. Compare them against S1's targets: quality, cost, risk, how hard to undo.
5. Decide, and write what evidence would change your mind.
6. Do the lab: code that tests the decision.
7. Mark the ADR accepted; turn the card green on the map.

You write the reasoning. Claude asks, challenges, explains terms and reviews the lab. NEEDS lists hard
dependencies (the arrows on the old diagram showed the suggested order).

**ADR page layout:** title · status · date · cards · exam tags; Question; Context (what forces the decision);
Options A, B, C (pros · cons); Criteria × options grid (quality · cost · risk · effort · how hard to undo);
Decision (one paragraph); Consequences (easier · harder); Revisit when …; Lab (code / eval run). File:
`docs/adr/ADR-NNN-name.md`, copied from [ADR-template.md](ADR-template.md). A superseded ADR stays, naming its
replacement.

**Status lifecycle:** Open (no answer yet) → ADR session → Proposed (draft answer) → Accepted (ADR + lab done) →
new evidence → Superseded (by a newer ADR).

**Ground rules:**

- Re-derive, don't erase: the current proposal is one option, not the default.
- Change a decision only when you can say what was wrong or what changed.
- Confirming the old answer is a good outcome.
- No ADR is accepted before its lab has run.
- One session, one ADR. Then stop.
- Not up for reset: CTIS client, snapshots, Postgres, compose. Built, tested, not AI.
- Each accepted ADR updates the map: card colour and decision log.

## M1 Frame

Exit: numeric targets and a labelled sample.

### S1 Success criteria

Exam: P1 · P6 · cards 01

- **Question:** When are our verdicts good enough, and which may be published without a person approving them?
- **Options:**
    - A. match Bruckner's reviewers, publish at ≥ 99% confidence (accepted)
    - B. a person approves every verdict
    - C. one threshold, no manual approval
- **Needs:** Bruckner data (OSF)
- **Lab:** data loaded; due-date rules match all 277 verdicts (trialwatch gold compare-due).
- **Done when:** answer key loaded; due-date rules reproduce all 277 verdicts. The 99% line moved to S8's lab.

### S2 Workflow or agent, per step

Exam: F1 · P1 · cards 02

- **Question:** For each box in the architecture diagram: plain code, one LLM call, workflow, or agent?
- **Options:**
    - A. code + single calls + one workflow, no agent (current proposal)
    - B. one agent with tools runs the whole check
    - C. plain code and regex only, no LLM
- **Needs:** S1
- **Lab:** label every box in the architecture diagram with its rung and one line why.
- **Done when:** you can defend every rung against the next one up.

## M2 Measure first

Exit: a baseline score to beat, before any LLM call.

### S3 Evaluation strategy

Exam: P4 · cards 11

- **Question:** How is each check scored, and what blocks a release?
- **Options:**
    - A. gold labels + code graders + CI gate (current proposal)
    - B. LLM-as-judge grades free-text answers
    - C. manual spot checks before each report
- **Needs:** S1
- **Lab:** eval harness + a rules-only baseline (no LLM) on the 20 trials, run from pytest.
- **Done when:** the baseline score is in the ADR: the number every later change must beat.

## M3 First LLM check

Exit: the results check beats the baseline on 20 trials.

### S4 Finding schema

Exam: F4 · cards 05

- **Question:** What exact shape does every check's answer have?
- **Options:**
    - A. claim + quote + page via structured outputs (current proposal)
    - B. a strict tool whose input is the schema
    - C. free text, parsed afterwards
- **Needs:** S3
- **Lab:** Pydantic Finding model + tests for each verdict, including "uncertain" and "not_found".
- **Done when:** the schema is in code, versioned, and linked from the ADR.

### S5 Evidence & grounding

Exam: F5 · P3 · cards 09

- **Question:** How does each claim prove itself, and who checks the proof?
- **Options:**
    - A. model quotes verbatim + page; code verifies (current proposal)
    - B. API citations on the PDF (can't be combined with structured outputs)
    - C. trust the verdict; sample-check by hand
- **Needs:** S4 · results PDFs downloaded
- **Lab:** verify_quote(pdf_text, quote, page); measure how often it fails on real PDFs.
- **Done when:** a verification rule is chosen, with a measured failure rate.

### S6 LLM gateway

Exam: P3 · cards 17

- **Question:** Do all model calls go through one module, and what does it own?
- **Options:**
    - A. own thin gateway over the Anthropic SDK (current proposal)
    - B. call the SDK directly in each check
    - C. a third-party LLM proxy or framework
- **Needs:** S4
- **Lab:** src/trialwatch/llm/gateway.py with a fake provider; one llm_call log row per call.
- **Done when:** tests exercise the gateway with no network and no API key.

### S7 Model, prompt & context

Exam: F4 · F5 · P2 · cards 03 · 04 · 06

- **Question:** Which model and effort, which prompt, and which context for the results check?
- **Options:**
    - A. strongest model, whole PDF, checklist prompt
    - B. cheaper model or lower effort, trimmed context
    - C. cheap model first, strong model on hard cases
- **Needs:** S5 · S6
- **Lab:** prompt v1 + context builder; run 2 models × 2 effort levels on the 20 trials.
- **Done when:** the first LLM score beats the S3 baseline, or you know why not.

## M4 Trust it

Exit: every finding traceable, reviewable, injection-tested.

### S8 Uncertainty & human review

Exam: F5 · P4 · P5 · cards 12 · 13

- **Question:** When does a person decide, and how do their verdicts flow back?
- **Options:**
    - A. manual approval below 99% confidence (ADR-001) (current proposal)
    - B. a person approves every verdict
    - C. publish, then review a random sample
- **Needs:** S7
- **Lab:** an "uncertain" verdict, review table, approve / reject CLI; set the 99% line (ADR-001).
- **Done when:** every finding has a path to a person; verdicts feed the gold set; the 99% line holds.

### S9 Audit trail

Exam: F5 · P6 · cards 16

- **Question:** Can we trace any finding back to its data, prompt, model and run?
- **Options:**
    - A. full lineage in Postgres, never overwrite (current proposal)
    - B. keep only the latest findings, plus logs
    - C. store raw LLM transcripts as files
- **Needs:** S6 · S8
- **Lab:** migration for llm_call and finding, with FKs to trial_snapshot and fetch_run.
- **Done when:** one SQL query goes from a finding to its PDF hash, prompt version and model id.

### S10 Untrusted documents

Exam: P5 · P3 · cards 14

- **Question:** What could a hostile PDF make the system do?
- **Options:**
    - A. defence in depth: read-only LLM, code checks, review
    - B. screen documents with a classifier first
    - C. rely on the model resisting injection
- **Needs:** S5 · S8
- **Lab:** an injected test document in the eval set; assert the verdict does not flip.
- **Done when:** the injection case runs in CI and passes.

### Ingestion work these sessions need

Plain engineering, not ADR sessions: the "not built yet" list of the system design diagram. Do each just before
the session that needs it.

| Work | Needed by |
|---|---|
| Bruckner benchmark seed | S1 |
| Results PDF download | S5 |
| Full CTIS list (source sync) | S11 |
| Weekly scheduler | S11 |
| Europe PMC connector | check 2 |

## M5 Run weekly

Exit: known cost per run; upgrades go through evals.

### S11 Cost & batching

Exam: F3 · P3 · cards 18

- **Question:** What may a weekly run cost, and which levers do we pull?
- **Options:**
    - A. Batch API + prompt caching + skip unchanged trials
    - B. real-time calls on the cheapest model
    - C. real-time calls on the strongest model, no budget
- **Needs:** S7 · full CTIS crawl
- **Lab:** cost estimate from S7's run data; a batch path in the gateway (built: ADR-009, `llm.py` batches and `batching.py`).
- **Done when:** cost per trial and per run are written down, with an alert threshold.

### S12 Observability & safe change

Exam: P3 · P4 · P6 · cards 19 · 20

- **Question:** What do we watch, and how do prompt and model changes ship?
- **Options:**
    - A. llm_call table + SQL views + eval-gated shadow runs
    - B. a hosted LLM observability service
    - C. logs only; change when needed
- **Needs:** S9 · S11
- **Lab:** SQL view of cost and verdict mix per run; model ids pinned in config.
- **Done when:** a model upgrade has a written, eval-gated procedure.

## M6 Publish

Exit: a first defensible report; findings open to AI agents.

### S13 Publication policy

Exam: P5 · P6 · cards 15

- **Question:** What do we publish about sponsors, and how do we stay fair and lawful?
- **Options:**
    - A. methods page + error rates + right of reply first
    - B. publish raw findings with a disclaimer
    - C. send findings to sponsors privately, publish later
- **Needs:** S8 · S12
- **Lab:** docs/methods.md + error rates per sponsor type from the eval.
- **Done when:** a first report could be defended line by line.

### S14 MCP & tools

Exam: F2 · P3 · cards 07 · 08

- **Question:** How do other AI apps use our findings?
- **Options:**
    - A. read-only MCP server over published findings (current proposal)
    - B. a plain REST API with docs
    - C. both
- **Needs:** S13
- **Lab:** stdio MCP server with list_findings + get_finding, connected via .mcp.json.
- **Done when:** Claude Code answers "which sponsors in Poland are late?" from our data.

## Later, only if evals demand

Exit: evidence first, autonomy second.

### S15 Search agent?

Exam: F1 · P1 · cards 10

- **Question:** Does publication matching need an agent at all?
- **Options:**
    - A. keep the fixed workflow: search → match → verify
    - B. a bounded agent with search tools, max turns
    - C. add vector search instead of an agent
- **Needs:** an eval showing missed papers
- **Lab:** hand-written agent loop with logged turns, scored on the same eval.
- **Done when:** evidence shows the gain pays for the extra cost and risk.

### After the first check

S1–S14 take one check end to end: the results-document check. The other two checks reuse most of it.

- Publication finder (Europe PMC): short ADRs only for search and matching; S15 decides whether it needs an agent.
- Outcome-switch detector: a short ADR for the COMPare labels and the comparison rule.
- Schema, gateway, review, audit trail, cost and publication policy carry over unchanged.
