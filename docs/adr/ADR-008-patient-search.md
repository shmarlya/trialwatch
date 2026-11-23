# ADR-008: Patient search over trials that recruit now

**Status:** accepted · **Date:** 2026-11-22 · **Session:** — · **Cards:** — · **Exam:** — · **Amends:** ADR-007 · **Superseded by:** none

## Question

How does a person find a trial they could join in our data (“diabetes Paris”), among only the trials that recruit now, and what must the data and the public pages gain for it?

## Context

- We held CTIS's list of all 12,905 trials but the full record of one: sites, MedDRA terms, translations and eligibility were not in our data.
- 4,747 trials recruit now (CTIS status 3 or 4), by country: CTIS gives a status per member state, not per site. All are medicine trials (EU 536/2014).
- People type short queries, condition first, often a place, in lay words and other languages, often misspelled; synonyms and “did you mean” rescue most failed searches. Text alone misses: 789 recruiting trials say “cancer”, 956 more only “tumour”, “carcinoma”, “lymphoma”…
- Exclusion criteria name diseases too (“patients with diabetes are excluded”): matching them would mislead.
- “Paris” must be a distance, not a city's name: Gustave Roussy is in Villejuif. CTIS gives each site's city and postcode, no coordinates.
- A public trial page needed a published judgement (ADR-007); a recruiting trial has none. Search queries are about people's health.
- Anthropic only, every call priced; MedDRA's dictionary is licensed; MeSH (NLM) and GeoNames are free to use with credit.

## Options

### A. Send people to CTIS's own search

How it works: a link to euclinicaltrials.eu with the words filled in.

- **+** nothing to build
- **–** no synonyms or distance; what we read is unused

### B. Own index: rules, a gazetteer, AI tags offline

How it works: each transfer reads a profile from the record and builds a search document: conditions, translations, MedDRA and MeSH concepts with the tree above them, categories; sites get points from GeoNames. Rules read a query and show their reading. A model tags each recruiting trial once, every tag with a quote checked against its field.

- **+** instant, free per search, the same every time
- **+** nothing about a search leaves the server
- **–** rules miss sentences (“my son has CF near Lyon”)
- **–** GeoNames and MeSH to load and keep current

### C. A model reads every query

How it works: each search asks a model for filters.

- **+** understands sentences
- **–** cost and delay on every search; abuse
- **–** health questions sent out with every search

## Criteria × options

| Criterion | Option A | Option B | Option C |
|---|---|---|---|
| Quality: the right trials found | – no synonyms or distance | + synonyms, concepts, km | + reads sentences too |
| Privacy: what people search for | – sent to CTIS | + stays here, never stored | – sent to a model |
| Cost and speed per search | + none | + none (tags: ~$5 once) | – a call each, seconds |
| Transparency of the reading | 0 | + shown; tags quoted | – a model's reading varies |
| Undo: how hard to reverse later | + nothing to undo | + drop the search schemas | 0 |

## Decision

We choose B. Every trial's full record is synced, recruiting ones first; a transfer stores the trial's profile (core.trial_profile) and builds its search document (search.document) at once, so a trial is findable as it arrives. A query is read by rules and GeoNames, never a model; the page shows the reading (“diabetes · near Paris, France · within 50 km”) and lets the reader change it. Conditions match by word beginnings (accents aside), CTIS's translations, MedDRA names, MeSH concepts and the tree above them, and AI synonyms once tagged; exclusion criteria never. AI tags: one model call per recruiting trial, each tag kept only if its quote is in its field. Amends ADR-007: the page of a trial that recruits now is public, judged or not (view.recruiting). Searches are private: never stored, no referrer, not cached or indexed, no query string in the access log. Decided by quality and privacy at no cost per search.

## Consequences

**Easier:** people find recruiting trials by condition and place in any EU language, with distances, without an account.

**Harder:** GeoNames and MeSH to load and refresh; rules to tune per language; the first sync of every record takes hours.

## Revisit when · Lab

**Revisit when:** people type sentences the rules can't read; the search eval shows a gap a model would close; a MedDRA licence comes; CTIS publishes a status per site.

**Lab:** tests/test_search_web.py (diabetes Paris finds Villejuif, not Lyon), test_search_query.py, test_search_index.py, test_geo.py, test_profile.py, test_annotate.py.

## Session notes

Decided with the user on 2026-11-22: sync every trial's record and search our own data; a trial needs no judgement to be found; rules and a gazetteer read a query, no model per search; AI tags with checked quotes, no AI-written summary yet. Asked how a condition's codes are matched (diabetes): CTIS publishes MedDRA codes at mixed levels and none for a fifth of trials, so MeSH (free, with its hierarchy) is the concept layer.
