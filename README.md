# TrialWatch
AI watchdog tracking missing and incomplete clinical-trial results in the EU register

## Docs

The design docs are Excalidraw diagrams in `docs/` (open a `.excalidraw` file at [excalidraw.com](https://excalidraw.com) to edit it). Each one has a rendered image in `docs/img/`, so you can read them here:

- [Current system (as built)](docs/img/system-design.svg) · [High-level architecture](docs/img/architecture.svg) · [Database tables](docs/img/db.svg) · [External API interfaces](docs/img/external-apis.svg)
- [AI architecture decisions & learning map](docs/img/ai-architecture-decisions.svg) · [ADR roadmap](docs/img/adr-roadmap.svg)
- ADRs: [ADR-001 success criteria](docs/img/adr/ADR-001-success-criteria.svg) · [ADR-002 workflow or agent](docs/img/adr/ADR-002-workflow-or-agent.svg) · [ADR-003 sources and gold datasets](docs/img/adr/ADR-003-sources-and-gold-datasets.svg) · [ADR-004 generic check, requirements as data](docs/img/adr/ADR-004-generic-check-requirements-as-data.svg) · [ADR-005 data sources in stages](docs/img/adr/ADR-005-data-sources-in-stages.svg) · [ADR-006 sign-in and an action → role chart](docs/img/adr/ADR-006-auth-and-roles.svg) · [template](docs/img/adr/ADR-template.svg)

The images are generated, never edited by hand: `node tools/diagrams/render.mjs` renders every stale one with Excalidraw's own exporter in headless Chromium, and a pre-commit hook runs it whenever a commit touches a diagram. If an image changed, the commit stops so you can `git add docs/img` and commit again. `tests/test_docs.py` fails while any image is out of date. One-time setup (Node 24 and uv):

```bash
npm ci --prefix tools/diagrams
```

```bash
npm exec --prefix tools/diagrams -- playwright install chromium
```

```bash
uv run pre-commit install
```

The same docs also build a website with MkDocs Material: Home from this README, Glossary from `GLOSSARY.md`, and one page per diagram with its image and its text (so search finds what's in a diagram). `tools/site_hooks.py` generates the pages; `mkdocs.yml` holds the nav, where each new diagram needs an entry. `.github/workflows/docs.yml` runs the docs check and a strict build on every push and pull request, and every push to `main` publishes the site to https://shmarlya.github.io/trialwatch/. Preview it locally at http://127.0.0.1:8000/trialwatch/ with:

```bash
uv run --group docs mkdocs serve
```

## Web interface

A small local site for the workflow (ADR-005): on the Sources page, sync a registered source's list (every trial CTIS lists becomes a trial here, with its EU CT number and title), then transfer the evidence of the trials you tick or of all pending, failed or stale ones (each trial's record, the documents the source's config names, and their page text), or add a trial by hand with uploaded PDFs; then run the evidence check, as background jobs with live progress. A check reads a trial only when its evidence is ready: every source that holds the trial gave it completely (with the document kinds the check reads, where the source has them), and nothing changed at the source since. A stale trial is transferred again first, so a sponsor's corrected document is never judged by its old copy; the others are refused, with the reason. CTIS gives a trial's last change only as a day, so a trial changed on the day of its transfer stays stale until it is transferred again on a later day; and every transfer downloads each listed document again and compares its bytes, so a file replaced without a new version number is caught too. Browse every trial with its identifiers, documents and history (who added what, when), each check's judgement with a step-by-step trace from questions to rules (the prompt, retrieval, the model's raw answers, quote checks), and every run with its prompt and settings. A judgement whose documents or record changed since is marked outdated; a trial can be checked again, also with other settings to compare runs. The Gold page searches the gold datasets in data/gold/ (identifiers, titles, reviewers' comments; filters by gold label, reviewer disagreement and our result) and puts our newest judgement next to each item's expected answers, with false accusations flagged; a gold item whose evidence is not here yet can be transferred from its page, and a trial page shows the gold items that name it.

```bash
uv run trialwatch web --reload
```

Anyone may browse; every action needs a signed-in admin or moderator (ADR-006). What each role may do is one chart, src/trialwatch/auth/actions.py, shown at /admin/roles: moderators add trials, upload documents, edit sponsors, transfer evidence, run dry-run checks and stop their own jobs; only admins sync and configure sources, run checks that call the model, stop other people's jobs and manage users (/admin/users). The same rules hold for scripts calling the routes (401 when not signed in, 403 when the role may not), and writes are recorded as the signed-in person. People sign in with their email and a password (at least 12 characters). There is no sign-up. The first admin comes from `.env`: set `TRIALWATCH_ADMIN_EMAIL` and `TRIALWATCH_ADMIN_PASSWORD` (see `.env.example`), and `trialwatch web` adds that admin when it starts, if no user has that email yet. It never changes an existing user, so changing the password in `.env` later does nothing. Or add one from the terminal (it asks for the password):

```bash
uv run trialwatch user add you@example.org --role admin
```

`user list`, `user role EMAIL ROLE`, `user password EMAIL` and `user disable EMAIL` (`--enable` to undo) manage them; the command line itself is not limited by roles, since it holds the database password. Served on a network (`--host` with `TRIALWATCH_HOSTS`), put HTTPS in front: off this machine the session cookie is sent over HTTPS only.

It opens on http://127.0.0.1:8010 and reads the database and `ANTHROPIC_API_KEY` from `.env`. Model checks start as dry runs; untick "dry run" to send. Long jobs can be stopped between trials; what they did so far is kept. A job that dies with the server (a restart, `--reload`) leaves its run marked running, and the next run closes it as failed. The site answers only requests addressed to this machine (`TRIALWATCH_HOSTS` adds other names) and takes form posts only from its own pages; a stored PDF opens in the browser's viewer, any other file is downloaded.

The same steps from the terminal:

```bash
uv run trialwatch source list
```

```bash
uv run trialwatch source sync ctis
```

```bash
uv run trialwatch source transfer ctis --which pending --limit 20
```

```bash
uv run trialwatch check evidence --all --dry-run
```

`source transfer ctis 2025-522750-39-00 …` takes EU CT numbers instead; `source config ctis --config <file>` replaces a source's config (CTIS: `listing.criteria` are the fields of its search, e.g. `{"status": [8], "hasStudyResults": true}`; `evidence.document_kinds` the kinds to transfer, by default the summary of results and the clinical study report), `--disable` switches a source off, and `source register` adds a source read by another adapter. One process at a time syncs or transfers from a source. `check evidence --all` takes every trial whose evidence is ready; without `--dry-run` it calls the model. It runs with a named setup, `--config` (default `default`): a file in src/trialwatch/checks/configs/ that holds the model and its effort, the prompt, the requirement set and how documents reach the model (page text searched per question, or each PDF whole, as in `whole-files`). `check configs` lists the setups, the web forms offer them by name, and every run records the one it used, so runs made with the same setup can be compared; trying other settings means adding a setup file. The code is in src/trialwatch/sources/: registry.py, stages.py, and an adapter per source (sources/ctis.py with its HTTP client sources/ctis_client.py, sources/manual.py); the check is in src/trialwatch/checks/. Gold datasets are rebuilt from data/bruckner/ with `uv run trialwatch gold build bruckner` (on this machine, not in the container; a released version is never overwritten), listed with `uv run trialwatch gold datasets`, and `uv run trialwatch gold compare --run N` scores check run N against one (`--dataset bruckner/2026-03-16` picks which). The web interface's code is in src/trialwatch/web/ (app.py, jobs.py, templates/).

## Database and tests

Postgres runs in Docker Compose (compose.yaml): `db` holds the data and is reachable from this machine only, `migrate` applies db/migrations/*.sql with dbmate and writes db/schema.sql, and `app` runs the `trialwatch` command in a container. Copy `.env.example` to `.env` and set a password first. Every `trialwatch` command reads `.env`, so `uv run trialwatch …` on this machine uses the same database. This starts the database and applies the migrations:

```bash
docker compose run --rm migrate
```

The whole schema is one migration, db/migrations/20261121120000_init.sql, drawn in [Database tables](docs/img/db.svg). A database made with the migrations from before 2026-11-21 is not migrated: reset it with `docker compose down -v` (this deletes its data) and run `migrate` again.

The tests never touch that database: tests/conftest.py makes `<database>_test` on the same server from the migrations for every run, and CTIS and the model are faked. .github/workflows/tests.yml runs them on every push and pull request, with a Postgres of its own.

```bash
uv run pytest
```

## Code style

The Python is written to be read by people who don't read Python: plain loops and `if` statements instead of Python's shortcuts. The rules are in [STYLE.md](STYLE.md). All SQL lives in `src/trialwatch/db/`.

## Sources & acknowledgements

TrialWatch builds on the work of **Till Bruckner** ([TranspariMED](https://www.transparimed.org), Karolinska Institutet) and colleagues. We plan to use their published data as our evaluation benchmark and will credit them in everything we publish. TrialWatch is an independent project and is not affiliated with or endorsed by the authors.

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

Third-party data and code keep their own licenses and terms.
