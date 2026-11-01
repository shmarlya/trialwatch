# Glossary

Abbreviations and terms used in this project, roughly grouped. Add new ones as they appear.

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
| **Early termination** | Trial stopped before planned, with a reason (e.g. sponsor decision). Still has to report results. |
| **Temporary halt** | Trial paused; may restart. |
| **Paediatric trial** | Trial in children. Shorter results deadline (6 months instead of 12). |
| **PIP** | Paediatric Investigation Plan: an EMA-agreed plan for testing a medicine in children. |
| **SmPC** | Summary of Product Characteristics: the official prescribing information of an approved medicine. |

## Results

| Term | Meaning |
|---|---|
| **Summary of results** | The structured scientific results the sponsor must upload to CTIS (usually a PDF). The core thing we check. |
| **Lay (layperson) summary** | Plain-language results summary for the public, also required. |
| **CSR** | Clinical Study Report: the full, long report submitted to regulators; published in CTIS only in some cases. |
| **Due** | A trial whose results deadline has passed. |
| **On time / late** | Results posted before / after the deadline. |
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

## Project and tech

| Term | Meaning |
|---|---|
| **TrialWatch** | This project; published at ivyar.com. |
| **Snapshot** | A stored copy of a trial's CTIS data at one point in time; a new one is saved only when the data changes. |
| **Gold labels** | Hand-checked answers (from Bruckner, COMPare) we compare our results against. Stored in the gold schema. |
| **Evals** | Automated tests that score our checks against gold labels; a release is blocked if the score drops. |
| **Reference data** | Small, fixed lookup lists (status codes, document types, countries) loaded by migrations. |
| **Seed** | Data loaded once from files (e.g. the Bruckner benchmark), not fetched live. |
| **Dev seed** | Optional migrations in db/dev/ with Bruckner et al.'s data (the 2025-11-10 export and the final verdicts), for development and tests only. Git-ignored: the authors' OSF files carry no licence, so it isn't published until they agree. Build it with `uv run python scripts/dev_seed.py`, apply it with `docker compose run --rm migrate-dev`; it is recorded in its own dev_schema_migrations table and kept out of db/schema.sql. |
| **Migration** | A numbered SQL file that changes the database schema; applied in order by dbmate. Everything up to 2026-11-01 is squashed into one file, db/migrations/20261030143522_init.sql; new changes get new files. |
| **LLM** | Large language model (here: Claude), used to read PDFs and match publications. |
| **MCP** | Model Context Protocol: a standard way for AI assistants to call tools; we plan an MCP server exposing our findings. |
| **CI** | Continuous Integration: automated checks run on every code change. |
| **JSONB** | Postgres column type for storing and querying JSON documents. |
| **Search export** | The CSV the CTIS public search lets you download: one row per trial, human-readable labels. Bruckner et al.'s 2025-11-10 export is stored row by row in ctis.search_export_snapshot. |
| **ADR** | Architecture Decision Record: one page per decision (question, options, trade-offs, choice, when to revisit, lab). Ours are Excalidraw pages in docs/adr/. |
| **Verdict confidence** | Our model's own certainty (0–100%) about one trial's verdict. At 99% or above a verdict may be published; below that it needs manual approval (ADR-001, proposed). |
| **Manual approval** | A person confirms a verdict before it is published. |
| **Docs check** | tests/test_docs.py: fails when the docs name a file, table or command that doesn't exist, or leave one out. Runs with pytest and as a Stop hook. |
| **Answer key** | Bruckner et al.'s final verdict per trial (on time, late, not fully reported, extended, cancelled) and what its results document contained (full, incomplete, none), in gold.bruckner_verdict. ADR-001: our checks must reach the same verdicts. |
