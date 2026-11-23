# Glossary

Abbreviations and terms used in this project, roughly grouped. Each part's page explains its terms in context.

## Regulation and registers

| Term | Meaning |
|---|---|
| **EU CTR** | EU Clinical Trials Regulation (EU) No 536/2014. Applies since 31 Jan 2022; replaced the old Clinical Trials Directive. Requires sponsors to post results. |
| **CTIS** | Clinical Trials Information System, the EU's single portal and public register for trials under the CTR, run by the EMA ([euclinicaltrials.eu](https://euclinicaltrials.eu)). Our main data source. |
| **EMA** | European Medicines Agency, the EU drug regulator that operates CTIS. |
| **EudraCT** | The older EU trials database (before the CTR). Trials still running in Jan 2025 had to move ("transition") to CTIS. |
| **EU CT number** | CTIS trial identifier, e.g. `2025-522750-39-00` (year, number, check digits, version). |
| **Transitioned trial** | A trial started under the old Directive/EudraCT and moved to CTIS. |
| **EEA** | European Economic Area: EU countries plus Iceland, Liechtenstein, Norway. CTIS marks trials as "EEA only" or "EEA and non-EEA". |
| **MSC** | Member State Concerned: an EU/EEA country where the trial runs. |
| **RMS** | Reporting Member State: the MSC that leads the assessment of the application. |
| **Part I / Part II** | The two halves of a CTIS application. Part I = scientific content assessed jointly (protocol, product, endpoints); Part II = national aspects per country (consent forms, recruitment, sites). |
| **Sponsor** | The organisation responsible for the trial (company, hospital, university). Legally responsible for posting results. |

## Trials

| Term | Meaning |
|---|---|
| **Phase I–IV** | Trial stages. I: first in humans/safety; II: does it work, dose finding; III: confirm benefit vs standard care; IV: after approval. Bruckner et al. studied Phases II–IV. |
| **IMP** | Investigational Medicinal Product: the drug being tested. |
| **Endpoint / outcome** | What a trial measures to judge the result (e.g. survival at 12 months). **Primary** = main question; **secondary** = extra questions. CTIS says "endpoint", publications usually say "outcome". |
| **End of trial** | When the trial finishes in a country. **Global end of trial** = finished everywhere; the results deadline counts from this date. |
| **Completion date** | The later of a trial's end date and its global end date in CTIS (Bruckner et al.'s rule); results deadlines count from it. |
| **Early termination** | Trial stopped before planned, with a reason (e.g. sponsor decision). Still has to report results. |
| **Temporary halt** | Trial paused; may restart. |
| **Paediatric trial** | A trial under the paediatric regulation: it recruits children (age group 0-17 years or in utero) or has a PIP. Shorter results deadline (6 months instead of 12). |
| **PIP** | Paediatric Investigation Plan: an EMA-agreed plan for testing a medicine in children. |
| **SmPC** | Summary of Product Characteristics: the official prescribing information of an approved medicine. |

## Results

| Term | Meaning |
|---|---|
| **Summary of results** | The structured scientific results the sponsor must upload to CTIS (usually a PDF). The core thing we check. |
| **Lay (layperson) summary** | Plain-language results summary for the public, also required. |
| **CSR** | Clinical Study Report: the full, long report submitted to regulators; published in CTIS only in some cases (document type 45, our kind study-report). |
| **Due** | A trial whose results deadline has passed. Bruckner et al.: completed more than 12 months (paediatric: 6) before the export day. |
| **On time / late** | Results posted before / after the deadline. Bruckner et al. count days: within 365 (paediatric: 183) days of completion is on time. |
| **Reported fully** | Bruckner et al.'s definition: a full scientific results document AND a layperson summary posted. **Days to results** = results publication date minus completion date, for trials reported fully. |
| **Not fully reported** | Results missing, or posted but missing required parts (e.g. participant numbers or primary outcome results). |
| **Extended** | Deadline legitimately moved later (e.g. justified deferral). Not a violation. |
| **Outcome switching** | Publishing different outcomes than were registered (dropping or adding primary outcomes) without saying so. |

## Publications

| Term | Meaning |
|---|---|
| **Europe PMC** | Free database of life-science papers ([europepmc.org](https://europepmc.org)); we search it for each trial's journal article. |
| **PMID / PMCID** | PubMed ID / PubMed Central ID: identifiers of a paper and of its free full text. |
| **DOI** | Digital Object Identifier: permanent link to a paper or dataset. |

## Research and related projects

| Term | Meaning |
|---|---|
| **Bruckner et al. 2026** | The study our benchmark comes from: manual check of CTIS reporting compliance (277 due trials, 234 legally required to report). |
| **OSF** | Open Science Framework ([osf.io](https://osf.io)), where Bruckner et al. published their data and code. |
| **TranspariMED** | Till Bruckner's campaign for clinical-trial transparency. |
| **TrialScout** | LLM tool that links publications to trial registrations (Ahnström et al. 2026). |
| **EU TrialsTracker** | Oxford tracker of results reporting in the old EudraCT register. |
| **COMPare** | Oxford project that checked published trials for outcome switching; its labels are our benchmark for that check. |
| **CC BY 4.0** | Creative Commons Attribution licence: reuse is allowed if the source is credited. |

## Patient search

See [search](search.md).

| Term | Meaning |
|---|---|
| **Recruiting** | A trial looking for participants now: CTIS overall status "Authorised, recruiting" (3) or "Ongoing, recruiting" (4). CTIS gives the status per member state, not per site, so a card says "recruiting in France", never "at this hospital". The list (re-read at every sync) wins over a stored record. |
| **Trial profile** | What someone looking for a trial needs, read from its record into one shape for every source (`core.trial_profile`): conditions with translations, MedDRA terms, MeSH areas, phase, who can join, products, status per country. Never people. |
| **Search document** | One per trial (`search.document`), built from its profile inside each transfer: its words weighted A (conditions) to D (inclusion criteria), its concepts, categories, ages, countries. Exclusion criteria are never indexed. |
| **Gazetteer** | A dictionary of places with their names in many languages and their coordinates: GeoNames' places and postal codes, loaded offline. |
| **Geocoding** | Giving a site address a point: by its postcode, else its city's name, else a city named alike; its **precision** says which. A site with none is never in a distance search. |
| **Reading (interpretation)** | How the search read a query: condition words, place and distance, age, sex, healthy volunteers, phase. Shown above the results so a person can change it. Made by rules, never a model. |
| **MeSH** | Medical Subject Headings, the U.S. National Library of Medicine's vocabulary: descriptors (D003920 Diabetes Mellitus) with entry terms (synonyms, many in lay words) and tree numbers (the hierarchy: a **tree code** such as C04 Neoplasms or C18.452.394.750 Diabetes Mellitus; CTIS files a trial's therapeutic areas by the top branch). Our concept layer. |
| **MedDRA** | The Medical Dictionary for Regulatory Activities, which CTIS codes conditions in (LLT, PT, HLT, HLGT, SOC levels). Licensed: we keep only the terms CTIS publishes per trial. |
| **Concept (of a trial)** | A MeSH descriptor a trial's conditions name, found without AI or by the AI tags from a shortlist, always with the words it was found in. A search for a concept also finds the trials under it in the tree. |
| **Lay category** | A plain-language area (cancer, heart and blood vessels, …) made of MeSH branches, with the words people type for it in eight languages. |
| **AI tag** | A tag a model gave a recruiting trial (category, kind of medicine, purpose, synonym, MeSH concept, age limit), kept only if it quotes the field it names word for word. Shown as AI-made. |
| **recall@k, nDCG** | Search quality measures: the share of the relevant trials found in the first k results; and how high the relevant ones rank. |

## Sources and gold

See [sources and gold](sources-and-gold.md).

| Term | Meaning |
|---|---|
| **Source** | Where data comes from (`core.source`): a registry such as CTIS, a publication index, or manual entry, with the adapter that reads it and its config. |
| **Central model** | Trials and documents stored the same way whatever their source, in schema `core` (ADR-003). Gold labels are not in it. |
| **Sync** | Reading a source's list of trials and matching each to a trial here (ADR-005). Nothing but the list is fetched. |
| **Source listing** | What a source's list says about a trial (`core.source_listing`), rewritten by each sync. |
| **Evidence transfer** | Bringing one trial's evidence in from a source: its record, the configured documents and their page text; recorded as complete or failed. |
| **Evidence state** | A trial's state at a source: not transferred, failed, stale (the source changed it since) or complete. A check reads only complete, current evidence. |
| **Ingest run** | One run of a source adapter (`core.ingest_run`): a list sync or a transfer, with its config, counts and errors. |
| **Manual source** | Trials and documents a person enters on the web interface; a trial without a registry identifier gets a TW-number. |
| **Trial identifier** | An id of a trial in some scheme (eu-ct, eudract, nct, isrctn, who-utn, trialwatch), normalised and unique, so it always points to one trial. |
| **Trial identity** | Which trial a source's record belongs to. A source's key always belongs to one trial, and a new key never joins a trial that already has another key at the same source: it becomes its own trial, plus a possible duplicate. |
| **Trial record** | What one source says about a trial: the payload as received plus `registered`, the adapter's normalised reading. A new row only when the content changes. |
| **Snapshot** | A stored copy of what a source said at one point in time: a trial record version. (For gold evaluations, see Frozen snapshot.) |
| **Possible duplicate** | Two trials whose identifiers turned up together in one record. Never merged automatically: a person decides. |
| **File, document, version** | Three layers: the bytes once by sha256 (`core.file`); the document as its source identifies it; which file it had from when, replaced ones kept. |
| **Document version** | CTIS keeps a document's uuid across versions and raises its systemVersion when the file is replaced. CTIS serves only the current one, so every version we download is kept, and each transfer compares the bytes again. |
| **Result document** | A document of a kind that reports results: a summary of results, a lay summary, a study report, a publication. |
| **Presigned link** | A temporary download URL with its permission built in. CTIS answers a document download with one (Amazon S3, about 5 minutes), so the file is fetched straight away. |
| **Page text** | The text of each page of a stored PDF (`core.page`), extracted once with pypdf; what retrieval searches and quotes are checked against. |
| **Organisation, trial sponsor** | A sponsor stored once however many trials it sponsors; each trial has one primary sponsor and any co-sponsors. |
| **Trial location** | Where a trial runs, as a source says: a site (organisation, city, postcode) in a country, or a country with no site. People are never stored. |
| **Actor** | Who changed data (`core.actor`): a person or a process. Actor 1 is `trialwatch` itself. |
| **Audit log** | `core.audit_log`: every change to the central tables, with actor, time and the row before and after, written by Postgres triggers; never changed. |
| **Advisory lock** | A lock Postgres keeps for the session that takes it. One process at a time reads a source or decides trial identities, and every run holds one while it is alive. |
| **Abandoned run** | A run still marked running whose process died; the next run of its kind closes it as failed, keeping what it did. |
| **Gold dataset** | A named, versioned set of hand-checked answers per trial, kept as files in `data/gold/<name>/<version>/` (ADR-003), never in the database. Bruckner et al. is the first. |
| **Gold item** | One trial in a gold dataset: its identifiers, the expected parts and label, each reviewer's answers and comments. |
| **Gold labels** | Hand-checked answers (from Bruckner, COMPare) we compare our results against. |
| **Version lock** | `data/gold/versions.lock`: the sha256 of every released gold dataset version, which never changes. The one exception to "no versioning before production". |
| **Gold evaluation** | Our whole workflow run on a gold dataset's labelled trials, passing or failing on the gates in `evals/gates.json`. |
| **Evals** | Scoring our checks against gold labels: `gold compare` scores one run; a gold evaluation runs the whole workflow. |
| **Frozen snapshot** | CTIS's raw answers for a gold dataset's trials, recorded once and replayed to the CTIS adapter, so every gold evaluation reads the same evidence. |
| **Set aside** | A gold item left out of the score because our check read a document posted on or after the day the reviewers read theirs. |
| **Answer key** | Bruckner et al.'s final verdict per trial and what its results document contained, plus each reviewer's answers per part. ADR-001: our checks must reach the same verdicts. |
| **Per-part review** | How Bruckner et al.'s reviewers judged each results document: participant numbers reported, primary outcome results given, trial cancelled. Two reviewers per document, a third for 37. |
| **Reviewer agreement** | The first two reviewers gave the same answers before any discussion for 163 of 206 documents (79%). |
| **Search export** | The CSV the CTIS public search lets you download. Bruckner et al.'s exports of 2025-11-10 are in `data/bruckner/`. |
| **U+FFFD** | The Unicode replacement character (�). Bruckner et al.'s merged reviews file is not valid UTF-8, so its 749 broken byte sequences are read as U+FFFD; only free text is affected. |

## Checks

See [checks](checks.md).

| Term | Meaning |
|---|---|
| **Evidence-based judgement** | The evidence check: questions from a requirement set, pages from every current document of the kinds it reads, the model only finds and quotes evidence, code checks every quote, and the set's rules decide. What they can't settle is undecided, never a guess. |
| **Requirement set** | What a trial must report and how "enough" is decided, as a data file (ADR-004; first: `eu-ctr-results.json`). |
| **Setup** | A named way of running the evidence check (`checks/configs/`): model, effort, prompt, requirement set, method, retrieval settings. |
| **Check method** | How documents reach the model: `text` (page text, searched per question) or `files` (each PDF whole). |
| **System prompt** | The fixed instructions sent with every call of the check (`checks/prompts/evidence.md`); not versioned, but every run stores the exact text with its sha256. |
| **Fenced page text** | Each document's pages sit inside `<document>` and `<page>` tags; text that looks like those tags is defused, so a document can't speak to the model as if it were our instructions. |
| **Effort** | How hard the model thinks before answering (low … max): more effort costs more tokens. |
| **Refusal fallback** | If the model's safety checks decline a request, the API can re-run it on another recommended model; the answer stores which model served it. |
| **Retrieval** | Picking the pages a question needs: full-text search per question; small documents are sent whole, and a miss is asked again with every page ("widening"). |
| **Verified quote** | Evidence whose quote really occurs, as whole words, on the page it cites (at least 10 characters). Only verified quotes count. Also called grounding. |
| **Readable page** | A page whose text layer holds at least 200 letters. On an unreadable (scanned) page "not found" can't mean "not reported". |
| **Don't know** | An answer the model may give when it can't tell; it never counts as "not reported". |
| **Judgement run** | One run of the evidence check (`checks.run`), with its model, effort, prompt and requirement set texts; each run is a version. |
| **Trace** | Every step of judging one trial in one run (`checks.step`): questions, pages, retrieval, each model call as sent and answered, quote checks, rules. |
| **Outdated judgement** | A current judgement whose inputs changed after it was made. It still shows, marked outdated. |
| **Published judgement** | A judgement the public sees (`checks.publication`, ADR-007). Every judgement without an error is one, published by its run with its gold agreement. |
| **Confidence (of a judgement)** | The share of gold items that judgements of the same kind agreed with (same model, prompt text, requirement set, label and evidence signs), measured over at least 20 items. Not the model's own certainty. |
| **Withdrawal** | A moderator or admin takes back a published judgement with a reason. It stays public, marked withdrawn; this is the only manual step. |
| **False accusation** | Our check says a trial's results are not fully reported where the reviewers found them full. The costliest error (ADR-001). |
| **Due-date rules** | Bruckner et al.'s rules for which trials are due and whether results came on time, in plain code (`checks/due_date.py`); they reproduce all 277 of their verdicts. |

## Operations

See [operations](operations.md).

| Term | Meaning |
|---|---|
| **TrialWatch** | This project; code at [github.com/shmarlya/trialwatch](https://github.com/shmarlya/trialwatch), docs at [shmarlya.github.io/trialwatch](https://shmarlya.github.io/trialwatch/). |
| **Web interface** | `trialwatch web`: the public pages, the staff pages and the admin pages, on port 8010. |
| **Role** | What a signed-in user is (ADR-006): `admin` or `moderator`. Anyone not signed in is "public". |
| **Action** | Something the web interface lets a person do, named in the action chart (`auth/actions.py`); every changing route names one. |
| **Session** | A signed-in browser: a random token in the `tw_session` cookie, stored only as its SHA-256; it ends after 12 hours unused or 7 days. |
| **Job** | A workflow step queued in `jobs.job` and run by the worker, as whoever created it, with retries. |
| **Worker** | `trialwatch worker`: runs queued jobs one at a time, starts the jobs schedules make, and releases held jobs when the cap allows. |
| **Schedule** | A recurring job in `jobs.schedule` (daily, weekly or monthly, at an hour UTC), switched off until an admin starts it. |
| **Weekly refresh** | The scheduled job that syncs, transfers what is new, stale or failed, tags recruiting trials and checks trials never judged or outdated. |
| **Spend** | What paid API calls cost: one `core.api_call` row per billed model attempt, priced from `costs/prices.json`. |
| **Prices** | `costs/prices.json`: what each model costs per million tokens; a new model needs a price before it runs. |
| **Monthly cap** | The most automatic checks may spend in a calendar month (UTC); at the cap they are held, not failed. |
| **Data-access layer** | `src/trialwatch/db/`: the only place with SQL. |
| **Migration** | A numbered SQL file that changes the schema, applied in order by dbmate, which then writes `db/schema.sql`. |
| **Test database** | `<database>_test` on the same server, made afresh from the migrations for every test run; CTIS and the model are faked. |
| **Reference data** | Small fixed lookup lists the migration loads: identifier schemes and document kinds. |
| **CI** | Continuous Integration: GitHub Actions run ruff and the tests on every push and pull request, build the docs site, and, by hand, the gold evaluations. |
| **Ruff** | The Python linter and formatter that checks what it can of STYLE.md, in CI and as a pre-commit hook. |
| **Pre-commit hook** | A check git runs before each commit (`.pre-commit-config.yaml`): ruff. |
| **Docs site** | MkDocs Material, built from these markdown pages and published to GitHub Pages from `main`. The decisions stay in the repo. |
| **ADR** | Architecture Decision Record: one page per decision (question, options, trade-offs, choice, when to revisit, lab), in `docs/adr/`, kept in the repo only. |
| **JSONB** | Postgres column type for storing and querying JSON documents. |
| **LLM** | Large language model (here: Claude). |
| **MCP** | Model Context Protocol: a standard way for AI assistants to call tools; we plan an MCP server exposing our findings. |
