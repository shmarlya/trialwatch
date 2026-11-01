# ivyar.com
AI watchdog tracking missing and incomplete clinical-trial results in the EU register

## Sources & acknowledgements

TrialWatch builds on the work of **Till Bruckner** ([TranspariMED](https://www.transparimed.org), Karolinska Institutet) and colleagues. We plan to use their published data as our evaluation benchmark and will credit them in everything we publish. TrialWatch is an independent project and is not affiliated with or endorsed by the authors.

### Primary sources

- Bruckner T, Dike CE, Caquelin L, Freeman A, Aspromonti DA, DeVito NJ, Song Z, Karam G, Nilsonne G.
  *Assessing Compliance with Reporting Requirements in European Phase II–IV Clinical Trials: A Cross-Sectional Observational Study.*
  medRxiv preprint, 2026. doi:[10.64898/2026.04.03.26350111](https://www.medrxiv.org/content/10.64898/2026.04.03.26350111v1)
  — preprint under CC BY 4.0, not yet peer-reviewed · [protocol, data & code (OSF)](https://osf.io/sn4j2/overview): no licence stated, so we use these files locally and don't republish them without the authors' permission
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
