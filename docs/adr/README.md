# Decisions

Architecture decision records: one page per decision, with its options, trade-offs and when to revisit it. They
are kept in the repo only, not on the docs site. A superseded or amended ADR stays, naming what replaced it. New
ones start from the [template](ADR-template.md).

| ADR | Decision | Status |
|---|---|---|
| [ADR-001](ADR-001-success-criteria.md) | Match Bruckner's reviewers (its 99% publication line is replaced by ADR-007) | accepted |
| [ADR-002](ADR-002-workflow-or-agent.md) | Workflow or agent, per step | proposed, not written |
| [ADR-003](ADR-003-sources-and-gold-datasets.md) | Source-agnostic trials, documents and gold datasets | accepted |
| [ADR-004](ADR-004-generic-check-requirements-as-data.md) | A generic check; requirements are data | accepted |
| [ADR-005](ADR-005-data-sources-in-stages.md) | Data sources in stages: registry, list, evidence | accepted |
| [ADR-006](ADR-006-auth-and-roles.md) | Sign-in and an action → role chart (narrowed by ADR-007) | accepted |
| [ADR-007](ADR-007-public-and-staff-data.md) | What the public sees: every judgement published with its gold agreement; staff only withdraw (amended by ADR-008) | accepted |
| [ADR-008](ADR-008-patient-search.md) | Patient search over trials that recruit now | accepted |
| [ADR-009](ADR-009-model-calls-in-batches.md) | Model calls in Message Batches, at half price | proposed |

The [AI decision map](ai-decision-map.md) lists the 21 decisions every production AI system has to make, each
tied to a TrialWatch case and a lab; the [roadmap](roadmap.md) of 2026-11-01 orders them into ADR sessions.
