"""CTIS adapter (ADR-003, ADR-005): the EU Clinical Trials Information System as a registered source.

Its list is CTIS's public search (the one on euclinicaltrials.eu), read page by page in EU CT number
order with the criteria of the source's config; each listed trial is matched by its EU CT number.

Transferring a trial's evidence: fetch its record, find the trial by all its identifiers (EU CT number, NCT, WHO
UTN, EudraCT) or create it, store the record as a new version if it or our reading of it changed, and
read out the registered information checks ask about (`registered`). Then download every listed
document of the configured kinds (by default the result documents) that we don't have in that
version, store it with the day CTIS says its results were submitted, and link it to the trial as
listed by CTIS; a document of those kinds CTIS no longer lists is unlinked.

CTIS serves only the current file of each document, so each version is downloaded once and kept.
"""

import hashlib
import os
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg

from trialwatch.sources.ctis_client import CT_NUMBER, SEARCH_LIMIT, CtisClient, DownloadedFile
from trialwatch.hashing import json_hash, storable
from trialwatch.db import core
from trialwatch.db.core import Identifier, Listed
from trialwatch.sources.registry import Adapter, Fetched, ListingPage

SOURCE = "ctis"
# CTIS document types (its documents[].documentType) in our own kinds (core.document_kind); any other
# type is "other"
DOCUMENT_KINDS = {
    103: "results-summary",  # Summary of results (for publication)
    46: "lay-summary",  # Laypersons summary of results (for publication)
    45: "study-report",  # Clinical study report (for publication)
    104: "protocol",  # Protocol (for publication)
    7: "protocol",  # Synopsis of the protocol (for publication)
    14: "other",  # Recruitment arrangements
    15: "other",  # Subject information and informed consent form
    19: "other",  # Summary of Product Characteristics
}
# What a transfer brings by default: the kinds the evidence check reads that CTIS has. Lay summaries
# (often one per language) are only downloaded when a source's config asks for them.
DEFAULT_EVIDENCE_KINDS = ("results-summary", "study-report")
# Secondary identifiers in a record's secondaryIdentifyingNumbers, by their key there
SECONDARY_SCHEMES = {"nctNumber": "nct", "whoUniversalTrialNumber": "who-utn", "isrctnNumber": "isrctn"}
# The parts of a record listing results submissions; a document's associatedEntityId names one of them
RESULTS_LISTS = ("summaryResults", "laypersonResults")


# Top-level fields CTIS changes when it republishes, without the trial itself changing: kept in the
# stored payload, left out of the content hash
VOLATILE_FIELDS = frozenset({"publishDate"})


def content_hash(payload: dict) -> bytes:
    return json_hash(payload, VOLATILE_FIELDS)


# --- files on disk (optional copies for reading by hand) ---------------------------------------

def file_path(files_dir: Path, ct_number: str, entry: dict) -> Path:
    """data/ctis/documents/<CT>/<type>_<title>_<uuid8>_v<version>.<ext>: readable, and unique per version."""
    listed_title = entry.get("title")
    if not listed_title:
        listed_title = "document"
    # Anything but letters, digits, dots and dashes becomes "_", so the title is safe in a file name
    title = re.sub(r"[^\w.-]+", "_", str(listed_title))
    title = title.strip("_")
    title = title[:80]

    listed_file_type = entry.get("fileType")
    if not listed_file_type:
        listed_file_type = "bin"
    extension = re.sub(r"\W+", "", str(listed_file_type))
    extension = extension.lower()
    if not extension:
        extension = "bin"

    document_type = int(entry["documentType"])
    short_uuid = re.sub(r"[^\w-]+", "_", str(entry["uuid"])[:8])
    # The version comes from the API too: only word characters, dots and dashes, never a path
    version = re.sub(r"[^\w.-]+", "_", str(entry["systemVersion"]))
    version = version.strip(".")
    name = f"{document_type}_{title}_{short_uuid}_v{version}.{extension}"
    return files_dir / ct_number / name


def write_file(path: Path, content: bytes) -> bool:
    """Write once, atomically; an existing file is left alone. Returns True if it wrote one."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written under a temporary name and then renamed, so a half-written file never has the real name
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        temporary_path.write_bytes(content)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


# --- reading a CTIS record ---------------------------------------------------------------------

def _get(node: object, *path: str) -> object:
    """The value at path in nested dicts, or None where a step is missing or not a dict."""
    current = node
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _string(value: object) -> str | None:
    """A text value as given (stripped), or None for anything else: a number field holding a dict or a
    flag is not an identifier."""
    if not isinstance(value, str):
        return None
    return _text(value)


def identifiers(payload: dict) -> list[Identifier]:
    """Every identifier CTIS gives for the trial: its EU CT number, the EudraCT number of a trial that
    moved over from EudraCT, and the secondary ones (NCT, WHO UTN, ISRCTN, and an EudraCT number
    listed among other registries). A value that is not a valid identifier of its scheme (e.g.
    "N/A") is skipped when the record is stored."""
    found = [Identifier("eu-ct", str(payload["ctNumber"]))]
    eudract = _string(_get(payload, "authorizedApplication", "eudraCt", "eudraCtCode"))
    if eudract:
        found.append(Identifier("eudract", eudract))
    secondary = _get(payload, "authorizedApplication", "authorizedPartI", "trialDetails", "clinicalTrialIdentifiers",
                     "secondaryIdentifyingNumbers")
    if not isinstance(secondary, dict):
        return found
    for key, scheme in SECONDARY_SCHEMES.items():
        number = _string(_get(secondary, key, "number"))
        if number:
            found.append(Identifier(scheme, number))
    others = secondary.get("additionalRegistries")
    if isinstance(others, list):
        for registry in others:
            if not isinstance(registry, dict):
                continue
            name = _string(registry.get("otherRegistryName"))
            number = _string(registry.get("number"))
            if name and number and "eudract" in name.lower():
                found.append(Identifier("eudract", number))
    return found


def _primary_endpoints(payload: dict) -> list[dict]:
    endpoints = _get(payload, "authorizedApplication", "authorizedPartI", "trialDetails", "trialInformation",
                     "endPoint", "primaryEndPoints")
    found = []
    if not isinstance(endpoints, list):
        return found
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        text = _text(endpoint.get("endPoint"))
        if text is None:
            continue
        found.append({"id": endpoint.get("id"), "number": endpoint.get("number"), "text": text})
    return found


ORGANISATION_ID = re.compile(r"ORG-\d+")


def _country(sponsor: dict) -> str | None:
    addresses = sponsor.get("addresses")
    if not isinstance(addresses, list):
        return None
    for address in addresses:
        country = _text(_get(address, "address", "countryName"))
        if country:
            return country
    return None


def sponsors(payload: dict) -> list[core.Sponsor]:
    """The trial's sponsors as CTIS lists them: each an organisation, with EMA's organisation id
    (ORG-…) when CTIS gives one, and which of them is the primary sponsor."""
    listed = _get(payload, "authorizedApplication", "authorizedPartI", "sponsors")
    found = []
    if not isinstance(listed, list):
        return found
    for sponsor in listed:
        if not isinstance(sponsor, dict):
            continue
        organisation = sponsor.get("organisation")
        if not isinstance(organisation, dict):
            continue
        name = _text(organisation.get("name"))
        if name is None:
            continue
        found_identifiers = []
        business_key = _text(organisation.get("businessKey"))
        if business_key and ORGANISATION_ID.fullmatch(business_key):
            found_identifiers.append(Identifier("ema-org", business_key))
        commercial = sponsor.get("isCommercial")
        if not isinstance(commercial, bool):
            commercial = None
        found.append(core.Sponsor(
            name=name, primary=sponsor.get("primary") is True, identifiers=tuple(found_identifiers),
            kind=_text(organisation.get("type")), country=_country(sponsor), commercial=commercial,
        ))
    return found


def registered(payload: dict) -> dict:
    """The registered information checks ask about, in the shape every adapter fills."""
    trial_details = _get(payload, "authorizedApplication", "authorizedPartI", "trialDetails")
    identifiers_node = _get(trial_details, "clinicalTrialIdentifiers")
    duration = _get(trial_details, "trialInformation", "trialDuration")
    return {
        "public_title": _text(_get(identifiers_node, "publicTitle")),
        "full_title": _text(_get(identifiers_node, "fullTitle")),
        "short_title": _text(_get(identifiers_node, "shortTitle")),
        "status": _text(payload.get("ctStatus")),
        "start_date": _text(payload.get("startDateEU")),
        "end_date": _text(payload.get("endDateEU")),
        "estimated_global_end_date": _text(_get(duration, "estimatedGlobalEndDate")),
        "primary_endpoints": _primary_endpoints(payload),
    }


def document_language(entry: dict) -> str | None:
    """The document's ISO 639-1 language, if CTIS gives one; its numeric languageCode is CTIS's own list."""
    code = _text(entry.get("languageCode"))
    if code is not None and len(code) == 2 and code.isalpha():
        return code.lower()
    return None


def submission_dates(payload: dict) -> dict[str, datetime]:
    """Results submission id (as text) -> when it was submitted, from the record's results lists."""
    found = {}
    for list_name in RESULTS_LISTS:
        submissions = _get(payload, "results", list_name)
        if not isinstance(submissions, list):
            continue
        for submission in submissions:
            if not isinstance(submission, dict):
                continue
            submitted = _text(submission.get("submissionDate"))
            if submission.get("id") is None or submitted is None:
                continue
            try:
                # CTIS gives e.g. 2025-04-10T17:39:54, in UTC
                when = datetime.fromisoformat(submitted).replace(tzinfo=UTC)
            except ValueError:
                continue
            found[str(submission["id"])] = when
    return found


def published_at(entry: dict, submitted: dict[str, datetime]) -> datetime | None:
    """When the results a document belongs to were submitted, if the record says."""
    entity = entry.get("associatedEntityId")
    if entity is None:
        return None
    key = str(entity)
    if key not in submitted:
        return None
    return submitted[key]


def document_kind(entry: dict) -> str:
    try:
        code = int(entry.get("documentType"))
    except (TypeError, ValueError):
        return "other"
    if code in DOCUMENT_KINDS:
        return DOCUMENT_KINDS[code]
    return "other"


# --- the adapter (ADR-005) --------------------------------------------------------------------

def listed(row: dict) -> Listed:
    """One row of CTIS's public list as a listed trial."""
    ct_number = str(row.get("ctNumber", "")).strip()
    if not CT_NUMBER.fullmatch(ct_number):
        raise ValueError(f"CTIS listed a trial without an EU CT number: {str(row)[:200]}")
    title = _text(storable(row.get("ctTitle")))
    if title is not None:
        title = " ".join(title.split())
    return Listed(key=ct_number, title=title, identifiers=[Identifier("eu-ct", ct_number)],
                  updated_on=_day(row.get("lastUpdated")), summary=storable(row))


def _day(value: object) -> date | None:
    """CTIS's dd/mm/yyyy as a date; None when missing or not a date."""
    text = _text(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        return None


# Config fields and what they hold
LISTING_FIELDS = {"criteria", "page_size"}
EVIDENCE_FIELDS = {"document_kinds"}
MAX_PAGE_SIZE = 500


class CtisAdapter(Adapter):
    """CTIS through its public API: the list from its search, a trial's record from /retrieve, its
    documents from /documents/…/download."""

    name = "ctis"
    label = "EU Clinical Trials Information System"
    kind = "registry"
    url = "https://euclinicaltrials.eu"
    lists = True
    transfers = True
    # every kind DOCUMENT_KINDS maps CTIS's document types to
    document_kinds = ("lay-summary", "other", "protocol", "results-summary", "study-report")
    listing_columns = (("Sponsor", "sponsor"), ("Phase", "trialPhase"), ("Results received", "resultsFirstReceived"))
    key_label = "EU CT number"

    def __init__(self, client=None) -> None:
        # a CtisClient, or a fake with the same search / retrieve / download_document methods
        self.client = client

    @classmethod
    @contextmanager
    def connected(cls) -> Iterator["CtisAdapter"]:
        with CtisClient() as client:
            yield cls(client)

    def default_config(self) -> dict:
        return {"listing": {"criteria": {}, "page_size": MAX_PAGE_SIZE},
                "evidence": {"document_kinds": list(DEFAULT_EVIDENCE_KINDS)}}

    def config_problems(self, config: dict) -> list[str]:
        problems = []
        for name in config:
            if name not in ("listing", "evidence"):
                problems.append(f"unknown field {name!r} (there are listing and evidence)")
        listing = config.get("listing", {})
        evidence = config.get("evidence", {})
        if not isinstance(listing, dict) or not isinstance(evidence, dict):
            return problems + ["listing and evidence must be objects"]
        for name in listing:
            if name not in LISTING_FIELDS:
                problems.append(f"unknown listing field {name!r} (there are {', '.join(sorted(LISTING_FIELDS))})")
        for name in evidence:
            if name not in EVIDENCE_FIELDS:
                problems.append(f"unknown evidence field {name!r} (there is document_kinds)")
        if not isinstance(listing.get("criteria", {}), dict):
            problems.append("listing.criteria must be an object of CTIS search fields, e.g. {\"status\": [8]}")
        page_size = listing.get("page_size", MAX_PAGE_SIZE)
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
            problems.append(f"listing.page_size must be a whole number from 1 to {MAX_PAGE_SIZE}")
        kinds = evidence.get("document_kinds", list(DEFAULT_EVIDENCE_KINDS))
        if kinds is not None:
            known = set(DOCUMENT_KINDS.values())
            if not isinstance(kinds, list) or not kinds:
                problems.append("evidence.document_kinds must be a list of kinds, or null for every kind")
            else:
                for kind in kinds:
                    if kind not in known:
                        problems.append(f"unknown document kind {kind!r} (CTIS documents are {', '.join(sorted(known))})")
        return problems

    def evidence_kinds(self, config: dict) -> list[str] | None:
        evidence = config.get("evidence", {})
        if "document_kinds" not in evidence:
            return list(DEFAULT_EVIDENCE_KINDS)
        return evidence["document_kinds"]

    def key_problem(self, key: str) -> str | None:
        if CT_NUMBER.fullmatch(key):
            return None
        return f"not an EU CT number: {key!r}"

    def list_pages(self, config: dict) -> Iterator[ListingPage]:
        """The list in EU CT number order. CTIS answers at most SEARCH_LIMIT rows per query, so a
        longer list is read from both ends (ascending, then descending until it comes down to the
        trials the ascending pass read): up to twice SEARCH_LIMIT. Each trial is given once; the
        stage compares how many came with the total CTIS gives. A trial seen twice in the same pass
        means the list shifted between two pages (a trial came or went while it was read): it is
        skipped, the pass goes on, and the count tells the stage whether anything was missed."""
        listing = config.get("listing", {})
        criteria = listing.get("criteria", {})
        size = listing.get("page_size", MAX_PAGE_SIZE)
        pages_per_query = max(1, SEARCH_LIMIT // size)
        # every trial given so far, and the ones the ascending pass read
        seen = set()
        ascending = set()
        total = None
        for direction in ("ASC", "DESC"):
            for page in range(1, pages_per_query + 1):
                answer = self.client.search(criteria, page, size, direction)
                pagination = answer.get("pagination", {})
                if total is None:
                    total = pagination.get("totalRecords")
                rows = []
                met_the_other_end = False
                for row in answer["data"]:
                    found = listed(row)
                    if direction == "DESC" and found.key in ascending:
                        met_the_other_end = True
                        continue
                    if found.key in seen:
                        continue
                    seen.add(found.key)
                    if direction == "ASC":
                        ascending.add(found.key)
                    rows.append(found)
                everything = total is not None and len(seen) >= total
                last_page = not pagination.get("nextPage") or not answer["data"]
                done = everything or met_the_other_end or (direction == "ASC" and last_page)
                ended = done or direction == "DESC" and (last_page or page == pages_per_query)
                yield ListingPage(rows=rows, total=total, more=not ended)
                if done:
                    return
                if last_page:
                    break
            if total is None or total <= len(seen):
                return

    def fetch(self, conn: psycopg.Connection, source: str, key: str, config: dict,
              files_dir: Path | None = None) -> Fetched:
        """The trial's record (a new version if it changed), its sponsors, and every listed document
        of the configured kinds not stored in that version yet; documents of those kinds CTIS no longer
        lists are unlinked."""
        kinds = self.evidence_kinds(config)
        try:
            # text fields can hold characters Postgres can't store (a NUL): cleaned before anything else
            payload = storable(self.client.retrieve(key))
            saved = core.save_trial_record(
                conn, source=source, source_key=key, identifiers=identifiers(payload), payload=payload,
                registered=registered(payload), content_hash=content_hash(payload),
            )
        except Exception as exc:  # recorded on the trial's transfer
            return Fetched(trial_id=None, errors={"record": f"{type(exc).__name__}: {exc}"})
        fetched = Fetched(trial_id=saved.trial_id, new_trial=saved.new_trial, new_record=saved.new_version,
                          possible_duplicates=len(saved.possible_duplicates))
        try:
            core.set_trial_sponsors(conn, saved.trial_id, source, sponsors(payload))
        except Exception as exc:  # the sponsors are not evidence: reported, the transfer can still complete
            fetched.warnings["sponsors"] = f"{type(exc).__name__}: {exc}"
        entries = payload.get("documents")
        if not isinstance(entries, list):
            # a record without its list of documents says nothing about them: nothing is unlinked
            fetched.errors["documents"] = "the record has no list of documents"
            return fetched
        submitted = submission_dates(payload)
        listed = []
        for entry in entries:
            if not isinstance(entry, dict) or not _wanted(entry, kinds):
                continue
            fetched.documents += 1
            listed.append(str(entry.get("uuid")))
            try:
                _fetch_document(conn, self.client.download_document, fetched, source, key, saved.trial_id, entry,
                                published_at(entry, submitted), files_dir)
            except Exception as exc:  # recorded per document, the others go on
                fetched.errors[f"document {entry.get('uuid')}"] = f"{type(exc).__name__}: {exc}"
        fetched.unlinked = core.unlink_documents_not_listed(
            conn, trial_id=saved.trial_id, source=source, kinds=kinds, listed=core.document_ids(conn, source, listed),
        )
        return fetched


def _wanted(entry: dict, kinds: Sequence[str] | None) -> bool:
    if kinds is None:
        return True
    return document_kind(entry) in kinds


def _check_file(entry: dict, file: DownloadedFile) -> None:
    if not file.content:
        raise ValueError("empty file")
    listed_as_pdf = str(entry.get("fileType", "")).upper() == "PDF"
    if listed_as_pdf and not file.content.startswith(b"%PDF"):
        raise ValueError(f"listed as PDF but is not one (starts {file.content[:20]!r})")


def _fetch_document(conn: psycopg.Connection, download: Callable[[str, str], DownloadedFile], fetched: Fetched,
                    source: str, ct_number: str, trial_id: int, entry: dict, published: datetime | None,
                    files_dir: Path | None) -> None:
    uuid = str(entry["uuid"])
    version = str(entry["systemVersion"])
    # Downloaded every time, even when we hold this version: CTIS could replace the file without
    # raising its systemVersion, and only the bytes would show it
    file = download(ct_number, uuid)
    _check_file(entry, file)
    sha256 = hashlib.sha256(file.content).digest()
    saved = core.save_document_version(
        conn, source=source, source_key=uuid, kind=document_kind(entry), title=_text(entry.get("title")),
        language=document_language(entry), version=version, content=file.content,
        media_type=file.media_type, sha256=sha256, published_at=published,
    )
    the_document = saved.document_id
    content = file.content
    if saved.new_version:
        fetched.new_versions += 1
    else:
        fetched.unchanged += 1
    core.link_document(conn, trial_id=trial_id, document_id=the_document, basis="listed-by-source", source=source)
    if files_dir is not None:
        path = file_path(files_dir, ct_number, entry)
        if not path.exists():
            try:
                if write_file(path, content):
                    fetched.written += 1
            except OSError as exc:
                # a copy to read by hand is not evidence: the document itself is stored
                fetched.warnings[f"copy {uuid}"] = f"{type(exc).__name__}: {exc}"
