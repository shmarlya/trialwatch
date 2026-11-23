"""Converters (ADR-003): each builds gold dataset versions under data/gold/ from one source's own files.
The rest of TrialWatch reads any gold dataset the same way (gold/datasets.py); adding a benchmark means
adding a converter here."""

from trialwatch.gold.converters import bruckner

# A converter's name -> its build(data folder, ...) (`trialwatch gold build NAME`)
CONVERTERS = {"bruckner": bruckner.build}
