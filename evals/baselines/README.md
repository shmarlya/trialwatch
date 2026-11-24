# Baselines

Outputs recorded before the refactor (phase 0, 2026-11-22). Later phases compare against them: a
difference must be explained (a fix) or it is a regression.

- `search-eval.txt`: `uv run trialwatch search eval`, run on a local database with 4,747 recruiting
  trials, GeoNames and MeSH loaded. Counts depend on that data, so compare against the same data:
  restore `data/backups/trialwatch-2026-11-22-phase0-baseline.dump` (not in git) with `pg_restore`
  first. Known quirk: when nothing has every word, `near` is the relaxed search's count (any of the
  words), while `country` and `anywhere` stay strict (e.g. "type 1 diabetes child Dublin": near 8,
  country 0).
- `gold-short.json`: the accepted baseline of the short gold evaluation (hard-50, 3 runs, `default` setup),
  which `evals/gates/short.json` gates against: a run fails when more than 2 more items disagree with the gold
  label, or 2 fewer are decided. Record a new one with `gold eval … --repeat 3 --write-baseline
  evals/baselines/gold-short.json` (or commit a CI run's `gold-eval-baseline.json` artifact) when a change is
  accepted. `gold-short.md` says why the short evaluation is gated this way and what it measured.
