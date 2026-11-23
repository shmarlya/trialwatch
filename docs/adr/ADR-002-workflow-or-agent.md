# ADR-002: Workflow or agent, per step

**Status:** proposed · **Date:** 2026-11-13 · **Session:** S2 · **Cards:** 02 · **Exam:** F1 · P1 · **Supersedes:** none · **Superseded by:** none

Not written yet: the Excalidraw page held only the title and the empty template. The question and its options
are in the [roadmap](roadmap.md#s2-workflow-or-agent-per-step) (session S2) and on the
[AI decision map](ai-decision-map.md#02-workflow-or-agent) (card 02).

What is built answers it in practice: due dates are plain code (`checks/due_date.py`); the evidence check is a
fixed workflow of single model calls whose answers code verifies and rules decide (ADR-004); no step is an agent.
