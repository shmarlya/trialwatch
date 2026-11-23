"""What a person looking for a trial to join sees about one trial (ADR-008), on its page."""

from dataclasses import dataclass
from datetime import date

import psycopg

from trialwatch.db import core, overview
from trialwatch.db import search as search_db
from trialwatch.sources import profile, registry
from trialwatch.search.constants import AGE_GROUP_WORDS, PHASE_WORDS


@dataclass
class PatientView:
    """What a person looking for a trial to join sees about it (ADR-008): its profile, the MeSH concepts
    its conditions name with the words they were found in, and its page at the source."""

    profile: profile.TrialProfile
    recruiting: bool
    concepts: list
    source_url: str | None
    source_name: str
    updated_on: date | None
    phase_words: str | None
    age_words: list[str]
    # the AI tags its search document was built with (search/annotate.py), or None
    annotation: search_db.Annotation | None = None


def patient_view(conn: psycopg.Connection, trial_id: int) -> PatientView | None:
    stored = profile.current(conn, trial_id)
    if stored is None:
        return None
    read = stored.read
    source = core.registered_source(conn, stored.source)
    url = None
    source_name = stored.source
    if source is not None:
        source_name = source.name
        keys = core.source_keys_of_trial(conn, stored.source, trial_id)
        if keys and source.adapter:
            url = registry.adapter_class(source.adapter)().public_url(keys[0])
    phase_words = None
    if read.phase in PHASE_WORDS:
        phase_words = PHASE_WORDS[read.phase]
    age_words = []
    for group in read.age_groups:
        age_words.append(AGE_GROUP_WORDS[group])
    return PatientView(
        read,
        search_db.public_recruiting(conn, trial_id),
        search_db.trial_concepts(conn, trial_id),
        url,
        source_name,
        overview.listing_updated_on(conn, trial_id),
        phase_words,
        age_words,
        search_db.document_annotation(conn, trial_id),
    )
