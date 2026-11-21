# TrialWatch
AI watchdog tracking missing and incomplete clinical-trial results in the EU register

## Docs

The design docs are Excalidraw diagrams in `docs/` (open a `.excalidraw` file at [excalidraw.com](https://excalidraw.com) to edit it). Each one has a rendered image in `docs/img/`, so you can read them here:

- [Current system (as built)](docs/img/system-design.svg) · [High-level architecture](docs/img/architecture.svg) · [Database tables](docs/img/db.svg) · [External API interfaces](docs/img/external-apis.svg)
- [AI architecture decisions & learning map](docs/img/ai-architecture-decisions.svg) · [ADR roadmap](docs/img/adr-roadmap.svg)
- ADRs: [ADR-001 success criteria](docs/img/adr/ADR-001-success-criteria.svg) · [ADR-002 workflow or agent](docs/img/adr/ADR-002-workflow-or-agent.svg) · [ADR-003 sources and gold datasets](docs/img/adr/ADR-003-sources-and-gold-datasets.svg) · [ADR-004 generic check, requirements as data](docs/img/adr/ADR-004-generic-check-requirements-as-data.svg) · [ADR-005 data sources in stages](docs/img/adr/ADR-005-data-sources-in-stages.svg) · [ADR-006 sign-in and an action → role chart](docs/img/adr/ADR-006-auth-and-roles.svg) · [ADR-007 what the public sees](docs/img/adr/ADR-007-public-and-staff-data.svg) · [template](docs/img/adr/ADR-template.svg)

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

## Gold evaluations (CI)

Two GitHub Actions pipelines run our whole workflow on the Bruckner gold dataset and compare the results with the reviewers' labels (ADR-001): they transfer each trial's evidence, run the evidence check with the real model, score the run, and fail when a gate in `evals/gates.json` is missed (label agreement at least 90% where we decided, no false accusations, at most 20% left undecided, at most 10% not compared). Both are started by hand from the Actions tab and cost API money; they need the `ANTHROPIC_API_KEY` repository secret. The report is on the run's summary page and kept as an artifact. It also measures retrieval (no gate): how many trials were searched rather than sent whole, and of the questions asked again with every page, how many were then answered with a verified quote on a page the search had not shown (a search miss), as a search hit rate.


The evidence comes from a **frozen snapshot**, not live CTIS, so every run reads the same records and documents and a score changes only when our code, prompt or model does. The snapshot is CTIS's raw answers (each trial's record and document files), recorded once while transferring the gold trials and replayed to the same CTIS adapter: the transfer, page text extraction, retrieval and check run exactly as for a live trial, with only the network left out (src/trialwatch/sources/snapshot.py). It is made by the **Gold snapshot** workflow (`.github/workflows/gold-snapshot.yml`, by hand; it calls CTIS, not the model), which uploads the `.tar.gz` to a new GitHub release and prints the lock to commit as `evals/snapshot.lock.json`. The pipelines download the release file the lock names and refuse it if its sha256 differs. Until a snapshot is committed, they stop with that message. The snapshot is today's CTIS, not the reviewers' 2025-11-10 view: trials whose documents were posted after that day are still set aside.

- **Gold evaluation · short** (`.github/workflows/eval-short.yml`): the 50 hardest labelled items, listed in `evals/hard-50.txt` (reviewers disagreed, the team discussed it, the trial was terminated, … ; at least 15 of each label).
- **Gold evaluation · full** (`.github/workflows/eval-full.yml`): every labelled item (206), up to six hours.

Both call `.github/workflows/eval-shared.yml`, which runs the same command you can run locally (it calls the model):

```bash
uv run trialwatch gold eval --dataset bruckner/2026-03-16 --sample evals/hard-50.txt --snapshot snapshot.tar.gz
```

Without `--snapshot` it reads live CTIS. `trialwatch gold snapshot --dataset bruckner/2026-03-16 --out FILE --release TAG` makes a snapshot locally (it writes the lock too). With `--files-from data/ctis/documents --copied-on DATE` it takes each document's file from an earlier `source transfer --files-dir` copy when that copy holds the same document and version, and asks CTIS only for the others; the records always come from CTIS at that moment, and the snapshot's index says where each file came from.

The sample is made by `trialwatch gold sample --dataset bruckner/2026-03-16 > evals/hard-50.txt`; a test fails if the file no longer matches.

## Web interface

A small local site for the workflow (ADR-005): on the Sources page, sync a registered source's list (every trial CTIS lists becomes a trial here, with its EU CT number and title), then transfer the evidence of the trials you tick or of all pending, failed or stale ones (each trial's record, the documents the source's config names, and their page text), or add a trial by hand with uploaded PDFs; then run the evidence check, as background jobs with live progress. A check reads a trial only when its evidence is ready: every source that holds the trial gave it completely (with the document kinds the check reads, where the source has them), and nothing changed at the source since. A stale trial is transferred again first, so a sponsor's corrected document is never judged by its old copy; the others are refused, with the reason. CTIS gives a trial's last change only as a day, so a trial changed on the day of its transfer stays stale until it is transferred again on a later day; and every transfer downloads each listed document again and compares its bytes, so a file replaced without a new version number is caught too. Browse every trial with its identifiers, documents and history (who added what, when), each check's judgement with a step-by-step trace from questions to rules (the prompt, retrieval, the model's raw answers, quote checks), and every run with its prompt and settings. A judgement whose documents or record changed since is marked outdated; a trial can be checked again, also with other settings to compare runs. The Gold page searches the gold datasets in data/gold/ (identifiers, titles, reviewers' comments; filters by gold label, reviewer disagreement and our result) and puts our newest judgement next to each item's expected answers, with false accusations flagged; a gold item whose evidence is not here yet can be transferred from its page, and a trial page shows the gold items that name it.

```bash
uv run trialwatch web --reload
```

Anyone may browse what is public; every action needs a signed-in admin or moderator (ADR-006). What is public (ADR-007): trials, sponsors and their documents, the published judgements (each answer with its verified quote, not the trace), runs, prompts and requirement sets; people appear only as "TrialWatch staff". Unpublished judgements and their traces, sources and transfers, the gold datasets, the workflow page and names are for staff; a published judgement can be withdrawn (kept, marked withdrawn), and a file a person uploaded stays with staff until a published judgement quotes it. A judgement is published either by a moderator or admin from the trial page, or by its run as it finishes when its confidence is at least 90%: the share of gold items that judgements of the same kind (model, prompt text, requirement set, label, quotes verified, asked again with every page, a question unanswered) agreed with, over at least 20 items (src/trialwatch/checks/publishing.py). Below that, with too few gold items, or undecided, it waits for a person: /review lists what waits, with each kind's measured share. What each role may do is one chart, src/trialwatch/auth/actions.py, shown at /admin/roles: moderators add trials, upload documents, edit sponsors, transfer evidence, run dry-run checks and stop their own jobs; only admins sync and configure sources, run checks that call the model, stop other people's jobs and manage users (/admin/users). The same rules hold for scripts calling the routes (401 when not signed in, 403 when the role may not), and writes are recorded as the signed-in person. People sign in with their email and a password (at least 12 characters). There is no sign-up. The first admin comes from `.env`: set `TRIALWATCH_ADMIN_EMAIL` and `TRIALWATCH_ADMIN_PASSWORD` (see `.env.example`), and `trialwatch web` adds that admin when it starts, if no user has that email yet. It never changes an existing user, so changing the password in `.env` later does nothing. Or add one from the terminal (it asks for the password):

```bash
uv run trialwatch user add you@example.org --role admin
```

`user list`, `user role EMAIL ROLE`, `user password EMAIL` and `user disable EMAIL` (`--enable` to undo) manage them; the command line itself is not limited by roles, since it holds the database password. Served on a network (`--host` with `TRIALWATCH_HOSTS`), put HTTPS in front: off this machine the session cookie is sent over HTTPS only.

It opens on http://127.0.0.1:8010 and reads the database and `ANTHROPIC_API_KEY` from `.env`. Model checks start as dry runs; untick "dry run" to send.

### Jobs, the worker and the weekly refresh

A form on the site does not run its step itself: it queues a job (table jobs.job), and the worker runs the queued jobs one at a time. Start it next to the web interface (compose runs it as the `worker` service):

```bash
uv run trialwatch worker
```

Jobs survive restarts. A failed attempt is tried again after 1 min, 5 min, 30 min, 2 h, 6 h (sync and transfer 5 attempts, a check 3); a retry redoes only what is left (the keys not transferred yet, the trials whose model call failed for a passing reason such as an overload). An error that a retry can't fix (an unknown source, an invalid setup, a bad API key) fails the job at once. A job whose worker died (no heartbeat for 5 minutes) goes back to the queue. Jobs can be cancelled; a running one stops after the trial or page it is on, and what it did so far is kept. /admin/jobs lists them by status, with the failed and held ones first (every staff page shows a banner while there are any), their errors and their progress per attempt; an admin can try one again (`trialwatch jobs list --status failed`, `trialwatch jobs retry N` from the terminal).

Every Monday at 03:00 UTC (the weekly-refresh schedule, changeable on /admin/jobs) TrialWatch itself starts a refresh: it syncs every switched-on source's list, transfers the trials that are new, changed at the source (stale) or failed last time, in batches of 50, then checks, in batches of 20 with the default setup, the trials that were never judged or whose judgement is outdated. Nothing changed, nothing is checked and nothing is spent. `trialwatch jobs refresh` queues one now. Everything it does, and what TrialWatch publishes by itself, is recorded as the actor `trialwatch` (core.actor 1), not as a person.

### Spend

Every billed model call is a row in core.api_call with its tokens and its cost in US dollars, priced at Anthropic's list prices in src/trialwatch/costs/prices.json (each row keeps the prices it was charged at; a model missing from the list is shown as unpriced, never as $0). Runs, judgements, trace steps and jobs show their cost to staff, and /admin/spend shows the totals: this month, the last 30 days, all time, by month, model, operation and actor, and the costliest runs. An admin sets a monthly cap there: once this month's spend reaches it, the automatic checks of the weekly refresh are held (they go on by themselves next month or when the cap is raised); checks a person starts always run. The gold evaluation reports show the cost too. The site answers only requests addressed to this machine (`TRIALWATCH_HOSTS` adds other names) and takes form posts only from its own pages; a stored PDF opens in the browser's viewer, any other file is downloaded.

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

`source transfer ctis 2025-522750-39-00 …` takes EU CT numbers instead; `source config ctis --config <file>` replaces a source's config (CTIS: `listing.criteria` are the fields of its search, e.g. `{"status": [8], "hasStudyResults": true}`; `evidence.document_kinds` the kinds to transfer, by default the summary of results and the clinical study report), `--disable` switches a source off, and `source register` adds a source read by another adapter. One process at a time syncs or transfers from a source. `check evidence --all` takes every trial whose evidence is ready; without `--dry-run` it calls the model. It runs with a named setup, `--config` (default `default`): a file in src/trialwatch/checks/configs/ that holds the model and its effort, the prompt, the requirement set and how documents reach the model (page text searched per question, or each PDF whole, as in `whole-files`). `check configs` lists the setups, the web forms offer them by name, and every run records the one it used, so runs made with the same setup can be compared; trying other settings means adding a setup file. The code is in src/trialwatch/sources/: registry.py, stages.py, and an adapter per source (sources/ctis.py with its HTTP client sources/ctis_client.py, sources/manual.py); the check is in src/trialwatch/checks/. Gold datasets are rebuilt from data/bruckner/ with `uv run trialwatch gold build bruckner` (on this machine, not in the container; a released version is never overwritten), listed with `uv run trialwatch gold datasets`, and `uv run trialwatch gold compare --run N` scores check run N against one (`--dataset bruckner/2026-03-16` picks which). The web interface's code is in src/trialwatch/web/ (app.py, jobs.py, templates/); the worker's in src/trialwatch/jobs/ (worker.py, steps.py, refresh.py, retry.py).

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
