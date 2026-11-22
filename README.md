# TrialWatch
AI watchdog tracking missing and incomplete clinical-trial results in the EU register

TrialWatch reads the EU Clinical Trials Information System (CTIS) and does two things in the open:

- **Patient search** over the trials that recruit now: type a condition and a place in any EU language
  ("diabetes Paris") and find the trials recruiting near it.
- **Automated checks** that a trial's results were reported completely. A model finds and quotes the evidence,
  code checks every quote, and rules decide. Every judgement is published with its full trace and its agreement
  with a gold dataset of hand-checked answers.

Nothing runs in production yet.

## Quick start

Needs Docker and [uv](https://docs.astral.sh/uv/). Copy `.env.example` to `.env` and set a password (and, for the
web interface, the first admin's email and password), then start the database and apply the migrations:

```bash
docker compose run --rm migrate
```

```bash
uv run trialwatch web --reload
```

The web interface opens on http://127.0.0.1:8010. Load some data from the terminal (or from the Sources page):

```bash
uv run trialwatch source sync ctis
```

```bash
uv run trialwatch source transfer ctis --which recruiting --limit 20
```

Model calls need `ANTHROPIC_API_KEY` in `.env`. Run the tests (they make their own database and fake CTIS and the
model) and the linter:

```bash
uv run pytest
```

```bash
uv run ruff check
```

## Docs

- [Patient search](docs/search.md): how a query is read, the index, places and concepts, privacy.
- [Checks](docs/checks.md): the evidence check, publication, the trace, the due-date rules.
- [Sources and gold datasets](docs/sources-and-gold.md): CTIS and other sources, the central model, the gold
  dataset and the gold evaluations in CI.
- [Operations](docs/operations.md): running it, sign-in and roles, jobs and the weekly refresh, spend, the database,
  tests and CI.
- [Glossary](docs/glossary.md) · [Code style](STYLE.md) · [Decisions (ADRs)](docs/adr/README.md) ·
  [Benchmark data](data/bruckner/README.md)

The same pages build the docs site, https://shmarlya.github.io/trialwatch/ (the decisions stay in the repo only).

## Sources & acknowledgements

TrialWatch builds on the work of **Till Bruckner** ([TranspariMED](https://www.transparimed.org), Karolinska Institutet) and colleagues. Their published data is our evaluation benchmark (the gold dataset and the gold evaluations above), and we credit them in everything we publish. TrialWatch is an independent project and is not affiliated with or endorsed by the authors.

### Primary sources

- Bruckner T, Dike CE, Caquelin L, Freeman A, Aspromonti DA, DeVito NJ, Song Z, Karam G, Nilsonne G.
  *Assessing Compliance with Reporting Requirements in European Phase II–IV Clinical Trials: A Cross-Sectional Observational Study.*
  medRxiv preprint, 2026. doi:[10.64898/2026.04.03.26350111](https://www.medrxiv.org/content/10.64898/2026.04.03.26350111v1)
  — preprint under CC BY 4.0, not yet peer-reviewed · [protocol, data & code (OSF)](https://osf.io/sn4j2/overview): no licence stated, but the authors gave their permission on 2026-11-02, so the four files we read are in [data/bruckner/](data/bruckner/README.md) with credit
- Ahnström L, Bruckner T, Aspromonti DA, Caquelin L, Cummins J, DeVito NJ, Axfors C, Ioannidis JPA, Nilsonne G.
  *TrialScout links published results to trial registrations using a large language model.*
  medRxiv preprint, 2026. doi:[10.64898/2026.03.15.26348383](https://www.medrxiv.org/content/10.64898/2026.03.15.26348383v1.full.pdf)
  — [code (MIT)](https://github.com/lahnstrom/trialscout) · [web tool](https://metaresearch.se/trialscout)
- Country follow-ups: [Germany](https://bukopharma.de/wp-content/uploads/2026/06/CTIS-legal-violations-in-Germany_2026.pdf) · [France](https://formindep.fr/wp-content/uploads/2026/07/CTIS-legal-violations-in-France_20250615.pdf) · [TranspariMED summary](https://www.transparimed.org/single-post/half-of-clinical-trials-of-medicines-in-europe-violate-new-transparency-law-new-study)

### Related work

- **EU TrialsTracker** (Bennett Institute, University of Oxford) — tracks the older EU register (EudraCT): [site](https://eu.trialstracker.net) · [code (MIT)](https://github.com/ebmdatalab/euctr-tracker-code)
- **COMPare** (CEBM, University of Oxford) — outcome switching in published trials: [summary](https://s4be.cochrane.org/blog/2016/02/04/compare-the-cebm-outcome-monitoring-project)
- **ctrdata** — R package for retrieving data from public trial registers, including CTIS: [docs](https://rfhb.github.io/ctrdata/)

### Data

- [EU Clinical Trials Information System (CTIS)](https://euclinicaltrials.eu), operated by the European Medicines Agency
- [Europe PMC](https://europepmc.org) — publications
- [GeoNames](https://www.geonames.org) — places and postal codes, CC BY 4.0
- [MeSH](https://www.nlm.nih.gov/mesh/), U.S. National Library of Medicine — medical concepts and their synonyms

Third-party data and code keep their own licenses and terms.
