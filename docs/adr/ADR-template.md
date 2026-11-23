# ADR-NNN: <decision in a few words>

**Status:** proposed · **Date:** YYYY-MM-DD · **Session:** S? · **Cards:** ?? · **Exam:** F? · P? · **Supersedes:** none · **Superseded by:** none

How to use: copy to `docs/adr/ADR-NNN-name.md` and replace the placeholders. Status: open (no answer yet),
proposed (draft answer), accepted (ADR and lab done), superseded (by a newer ADR, which it names; it stays). One
page: if it doesn't fit, the decision is too big, so split it.

## Question

*One sentence.* E.g. For the results check, what precision and recall must we reach?

## Context

*What forces this decision now: facts and constraints, no solutions.* Why decide now? Facts and constraints:
earlier ADRs, data, law, budget, people. What goes wrong if we don't decide? 3–6 lines.

## Options

*2–3 real options; the current proposal is only one of them. Mark it "(current proposal)" if it is.*

### A. <option name>

How it works: …

- **+** …
- **–** …

### B. <option name>

How it works: …

- **+** …
- **–** …

### C. <option name>

How it works: …

- **+** …
- **–** …

## Criteria × options

*+ better · 0 no difference · – worse, and a few words why*

| Criterion | Option A | Option B | Option C |
|---|---|---|---|
| Quality: does it meet our targets? | + / 0 / – why | + / 0 / – why | + / 0 / – why |
| Cost: money, tokens, time per run | + / 0 / – why | + / 0 / – why | + / 0 / – why |
| Risk: what breaks, who gets hurt | + / 0 / – why | + / 0 / – why | + / 0 / – why |
| Effort: to build and to maintain | + / 0 / – why | + / 0 / – why | + / 0 / – why |
| Undo: how hard to reverse later | + / 0 / – why | + / 0 / – why | + / 0 / – why |
| <your own criterion> | + / 0 / – why | + / 0 / – why | + / 0 / – why |

## Decision

*What we chose and which criteria decided it.* We choose option _ because … One paragraph. Name the criteria
from the grid that decided it.

## Consequences

**Easier:** what gets simpler or becomes possible.

**Harder:** what gets more difficult, costs more, or is ruled out.

## Revisit when · Lab

**Revisit when:** evidence that would change this decision, e.g. a metric crossing a value, a new model, a new
law.

**Lab:** the code or eval run that tested this decision (file or commit) and its result in one line. No lab, no
acceptance.

## Session notes

*Optional.* Questions raised, ideas rejected and why, points for later ADRs.
