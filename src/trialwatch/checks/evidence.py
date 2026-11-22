"""The evidence check (ADR-004): judge one trial against a requirement set, using every current
document linked to it: retrieve → extract → verify → judge, logging every step.

1. Questions come from the requirement set (checks/requirement_sets/*.json) and the trial's
   registered information, whatever source gave it: e.g. one question per registered primary
   endpoint. The model does not decide what to look for.
2. Retrieve (checks/retrieval.py): full-text search over the page text of the trial's documents (of
   the kinds the set reads) picks the pages each question needs; small documents are sent whole.
3. Extract: one model call per trial returns, per question, whether it found evidence, a verbatim
   quote, its document and page, and what kind of evidence it is. The model does not judge.
   Questions it could not settle from retrieved pages are asked again with every page, so a
   retrieval miss can never turn into "not reported".
4. Verify (checks/verify.py): code checks that each quote really occurs on the cited page.
5. Judge: the set's rules (checks/requirements.py) turn verified evidence into parts and a label.
   Anything they can't settle is undecided (a person decides, ADR-001), never a guess.

Each judgement records its inputs at that moment (the trial record and document versions read); each
run records the prompt and requirement set it used. On the autonomy ladder (card 02) this is rung 3,
a fixed workflow with one LLM step inside. A run adds rows; nothing is updated.
"""

import base64
import html
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import anthropic
import psycopg

from trialwatch import costs as pricing
from trialwatch.checks import publishing, requirements
from trialwatch.checks.model import FALLBACK_BETA, MessagesClient, Prompt, first_text, uses_fallbacks
from trialwatch.checks.requirements import Question, RequirementSet
from trialwatch.checks.retrieval import pages_to_send_first, search, search_query_for
from trialwatch.checks.settings import Settings
from trialwatch.checks.verify import every_page_readable, has_readable_text, some_page_readable, verify
from trialwatch.db import checks as checks_db
from trialwatch.db import core
from trialwatch.db import costs as costs_db
from trialwatch.db.core import Page
from trialwatch.progress import Progress, report
from trialwatch.sources import stages

CHECK_NAME = "evidence"
# What its model calls are for, in core.api_call
OPERATION = "check"
# What a quote is evidence of; requirement sets say which of these mean "not reported"
KINDS = ("numeric_result", "narrative_only", "chart_only", "redacted", "not_analysed", "other")
# The model's three answers to "did you find it?": "dont_know" is always allowed and never a guess
FOUND_ANSWERS = ("yes", "no", "dont_know")
# A request may be 32 MB; files travel base64-encoded (4/3 larger), plus room for the questions
MAX_FILES_BYTES = 23 * 1024 * 1024


def trial_pages(conn: psycopg.Connection, trial_id: int, requirement_set: RequirementSet) -> list[Page]:
    """Every page of the current version of each document of the set's kinds linked to the trial."""
    return core.trial_pages(conn, trial_id, requirement_set.document_kinds)


# --- extraction ------------------------------------------------------------------------------

# Text in a document or a title that looks like one of the tags the request fences data with
FENCE_TAG = re.compile(r"<(/?\s*(?:trial|documents|document|page)\b)", re.IGNORECASE)


def fenced(text: str) -> str:
    """Data text that can't close or open the tags around it: a "<" starting one of our tag names
    becomes "‹" (a PDF or title could otherwise end its <page> and talk outside it)."""
    return FENCE_TAG.sub("‹\\1", text)


def render_pages(pages: list[Page]) -> str:
    parts = []
    current = None
    for page in pages:
        if page.doc != current:
            if current is not None:
                parts.append("</document>")
            title = html.escape(page.title, quote=True)
            parts.append(f'<document number="{page.doc}" title="{title}">')
            current = page.doc
        parts.append(f'<page number="{page.page_no}">\n{fenced(page.text)}\n</page>')
    if current is not None:
        parts.append("</document>")
    return "\n".join(parts)


def answer_schema(keys: list[str]) -> dict:
    item = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "enum": keys},
            "found": {"type": "string", "enum": list(FOUND_ANSWERS)},
            "quote": {"type": "string"},
            "document": {"type": "integer"},
            "page": {"type": "integer"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "note": {"type": "string"},
        },
        "required": ["key", "found", "quote", "document", "page", "kind", "note"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"answers": {"type": "array", "items": item}},
            "required": ["answers"], "additionalProperties": False}


def question_keys(questions: list[Question]) -> list[str]:
    keys = []
    for question in questions:
        keys.append(question.key)
    return keys


def describe_trial(context: dict) -> str:
    """What the model is told about the trial and its documents, from data (no source is assumed)."""
    lines = [f"Trial: {context['name']}"]
    if context.get("title"):
        lines.append(f"Title: {context['title']}")
    if context.get("source"):
        lines.append(f"Registered information from: {context['source']}")
    for document in context.get("documents", []):
        lines.append(f"Document {document['doc']}: {document['title']} ({document['kind']})")
    return fenced("\n".join(lines))


def build_request(settings: Settings, prompt: Prompt, pages: list[Page], questions: list[Question],
                  context: dict) -> dict:
    question_lines = []
    for question in questions:
        line = f"- {question.key}: {question.text}"
        if question.guidance:
            line = f"{line}\n  ({question.guidance})"
        question_lines.append(line)
    asked = "\n".join(question_lines)
    content = (f"<trial>\n{describe_trial(context)}\n</trial>\n\n"
               f"<documents>\n{render_pages(pages)}\n</documents>\n\n"
               f"Answer each of these questions once, by its key:\n{asked}")
    schema = answer_schema(question_keys(questions))
    request: dict[str, Any] = {
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "system": prompt.text,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": settings.effort,
                          "format": {"type": "json_schema", "schema": schema}},
        "messages": [{"role": "user", "content": content}],
    }
    _add_fallbacks(settings, request)
    return request


def _add_fallbacks(settings: Settings, request: dict) -> None:
    if uses_fallbacks(settings.model, settings.fallbacks):
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"


def is_pdf(file: core.TrialFile) -> bool:
    if file.media_type == "application/pdf":
        return True
    return file.content.startswith(b"%PDF")


def build_files_request(settings: Settings, prompt: Prompt, files: list[core.TrialFile], questions: list[Question],
                        context: dict) -> dict:
    """The files method's request: each document's file as a document block (numbered as in <trial>),
    then the trial and the questions as text."""
    content: list[dict] = []
    for file in files:
        encoded = base64.standard_b64encode(file.content).decode()
        content.append({"type": "document", "title": f"Document {file.doc}: {file.title}",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": encoded}})
    question_lines = []
    for question in questions:
        line = f"- {question.key}: {question.text}"
        if question.guidance:
            line = f"{line}\n  ({question.guidance})"
        question_lines.append(line)
    asked = "\n".join(question_lines)
    text = (f"<trial>\n{describe_trial(context)}\n</trial>\n\n"
            "The documents are the attached files, in the order of their numbers above; a page number is the "
            "file's own 1-based page.\n\n"
            f"Answer each of these questions once, by its key:\n{asked}")
    content.append({"type": "text", "text": text})
    request: dict[str, Any] = {
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "system": prompt.text,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": settings.effort,
                          "format": {"type": "json_schema", "schema": answer_schema(question_keys(questions))}},
        "messages": [{"role": "user", "content": content}],
    }
    _add_fallbacks(settings, request)
    return request


def request_for_trace(request: dict, files: list[core.TrialFile] | None = None) -> dict:
    """The request as sent, for the trace: without the system prompt (stored once with the run) and
    with each attached file's bytes replaced by its sha256 and size (the file is in core.file)."""
    by_title = {}
    if files:
        for file in files:
            by_title[f"Document {file.doc}: {file.title}"] = file
    sent = {}
    for name, value in request.items():
        if name == "system":
            continue
        if name != "messages":
            sent[name] = value
            continue
        messages = []
        for message in value:
            content = message["content"]
            if isinstance(content, str):
                messages.append(message)
                continue
            blocks = []
            for block in content:
                if block.get("type") == "document":
                    file = by_title.get(block.get("title"))
                    stored = {"type": "document", "title": block.get("title"), "source": {"type": "file"}}
                    if file is not None:
                        stored["source"] = {"type": "file", "sha256": file.sha256.hex(), "bytes": file.size_bytes,
                                            "media_type": "application/pdf"}
                    blocks.append(stored)
                else:
                    blocks.append(block)
            messages.append({"role": message["role"], "content": blocks})
        sent[name] = messages
    return sent


@dataclass
class CallResult:
    answers: dict[str, dict] = field(default_factory=dict)
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    # each billed attempt priced (more than one after a fallback); the provider's request id
    costs: list[pricing.CallCost] = field(default_factory=list)
    request_id: str | None = None
    # for the trace: the model that answered (after a fallback, not the one asked), why it stopped,
    # its answer as sent, and how long the call took
    served_by: str | None = None
    stop_reason: str | None = None
    raw_text: str | None = None
    duration_ms: int = 0


def token_count(usage: Any, name: str) -> int:
    """A token count from the response's usage, every attempt included: when the model declined and a
    fallback model answered (server-side fallback), the declined attempt is billed too, and only the
    usage's iterations list it. 0 when the API did not report it."""
    iterations = getattr(usage, "iterations", None)
    if iterations:
        total = 0
        for iteration in iterations:
            count = getattr(iteration, name, 0)
            if count:
                total += count
        return total
    count = getattr(usage, name, 0)
    if not count:
        return 0
    return count


def ask(client: MessagesClient, request: dict) -> CallResult:
    started = time.monotonic()
    try:
        # the prepared request dictionary is passed as keyword arguments to the SDK call
        response = client.beta.messages.create(**request)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        # the same for every trial: the run stops
        raise
    except anthropic.APIError as exc:  # e.g. overloaded, too large, a timeout: recorded per trial, the run goes on
        return CallResult(error=f"{type(exc).__name__}: {exc}", duration_ms=_milliseconds_since(started))
    # anything else (no credentials at all, a bug) is the same for every trial too: the run stops
    served_by = getattr(response, "model", None)
    priced_as = served_by
    if not priced_as:
        priced_as = request.get("model", "")
    result = CallResult(input_tokens=token_count(response.usage, "input_tokens"),
                        output_tokens=token_count(response.usage, "output_tokens"),
                        cache_write_tokens=token_count(response.usage, "cache_creation_input_tokens"),
                        cache_read_tokens=token_count(response.usage, "cache_read_input_tokens"),
                        costs=pricing.price_usage(response.usage, priced_as),
                        request_id=getattr(response, "_request_id", None),
                        served_by=served_by, stop_reason=response.stop_reason,
                        duration_ms=_milliseconds_since(started))
    text = first_text(response.content, "")
    result.raw_text = text
    if response.stop_reason in ("refusal", "max_tokens"):
        result.error = f"stop_reason {response.stop_reason}"
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        if category:
            result.error = f"{result.error} ({category})"
        return result
    try:
        parsed = json.loads(text)
        for answer in parsed["answers"]:
            key = answer["key"]
            if key not in result.answers:
                result.answers[key] = answer
            elif _differs(result.answers[key], answer):
                # two different answers to one question: neither is settled by its position, a person looks
                result.answers[key] = {"key": key, "found": "dont_know", "quote": "", "document": 0, "page": 0,
                                       "kind": "other", "note": "the model answered this question twice, differently"}
    except (ValueError, KeyError, TypeError) as exc:
        result.error = f"invalid answer: {type(exc).__name__}: {exc}"
    return result


def _differs(first: dict, second: dict) -> bool:
    """Two answers to one question that say different things."""
    for name in ("found", "kind", "quote", "document", "page"):
        if first.get(name) != second.get(name):
            return True
    return False


def _milliseconds_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# --- rules -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    key: str
    # True: found; False: not found; None: the model said it doesn't know
    found: bool | None
    kind: str | None
    quote: str
    page: Page | None
    note: str
    verified: bool
    # the model saw every page of the documents when it gave this answer, and every page had
    # readable text (a scanned page without a text layer counts as not seen)
    whole: bool


@dataclass
class TrialResult:
    label: str | None = None
    error: str | None = None
    widened: int = 0
    evidence: int = 0
    verified: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    # the model calls made for this trial, by their number (1: first call, 2: widening), for core.api_call
    calls: list[tuple[int, CallResult]] = field(default_factory=list)
    served_by: list[str] = field(default_factory=list)
    # the inputs read at this moment: the trial record and the document versions
    record_id: int | None = None
    document_versions: list[int] = field(default_factory=list)


def count_call(result: TrialResult, number: int, call: CallResult) -> None:
    """Add a model call's tokens and the model that answered to the trial's totals."""
    result.input_tokens += call.input_tokens
    result.output_tokens += call.output_tokens
    result.cache_write_tokens += call.cache_write_tokens
    result.cache_read_tokens += call.cache_read_tokens
    result.calls.append((number, call))
    if call.served_by:
        result.served_by.append(call.served_by)


def trial_costs(result: TrialResult) -> list[pricing.CallCost]:
    found = []
    for numbered in result.calls:
        call = numbered[1]
        for cost in call.costs:
            found.append(cost)
    return found


def record_calls(conn: psycopg.Connection, run_id: int, trial_id: int, result: TrialResult) -> None:
    """Add the trial's billed model attempts to core.api_call, made for the session's actor."""
    for numbered in result.calls:
        number = numbered[0]
        call = numbered[1]
        for cost in call.costs:
            costs_db.record_call(conn, cost, operation=OPERATION, check_run_id=run_id, trial_id=trial_id,
                                 call_number=number, request_id=call.request_id)


class Trace:
    """The steps of judging one trial, in order, for checks.step: what was asked, searched, sent,
    answered, verified and decided. Pages are referenced by file and page number, not copied."""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def add(self, name: str, data: dict, duration_ms: int | None = None) -> None:
        self.steps.append({"name": name, "at": datetime.now(timezone.utc), "duration_ms": duration_ms, "data": data})


def _questions_data(questions: list[Question], requirement_set: RequirementSet, context: dict, record) -> dict:
    listed = []
    for question in questions:
        listed.append({"key": question.key, "text": question.text, "part": question.part, "rule": question.rule,
                       "guidance": question.guidance})
    record_data = None
    if record is not None:
        record_data = {"id": record.id, "source": record.source, "fetched_at": record.fetched_at.isoformat()}
    return {"requirements": requirement_set.name, "requirements_sha256": requirement_set.sha256.hex(),
            "trial": context, "record": record_data, "questions": listed}


def _pages_data(pages: list[Page], unread: list[str], readable: bool) -> dict:
    documents = {}
    for page in pages:
        if page.doc not in documents:
            documents[page.doc] = {"doc": page.doc, "title": page.title, "kind": page.kind,
                                   "file": page.file.hex(), "pages": 0, "unreadable_pages": []}
        documents[page.doc]["pages"] += 1
        if not has_readable_text(page):
            documents[page.doc]["unreadable_pages"].append(page.page_no)
    listed = []
    for document in documents.values():
        listed.append(document)
    return {"documents": listed, "total_pages": len(pages), "every_page_readable": readable,
            "documents_without_text": unread}


def _call_data(call_number: int, settings: Settings, pages: list[Page], questions: list[Question],
               request: dict, call: "CallResult", files: list | None = None) -> dict:
    """One model call for the trace: what was asked, the request exactly as sent (the system prompt is
    the run's, kept once in checks.run.prompt_text), and the answer exactly as received."""
    keys = []
    asked = []
    for question in questions:
        keys.append(question.key)
        asked.append({"key": question.key, "text": question.text})
    sent = request_for_trace(request, files)
    files_sent = []
    if files:
        for file in files:
            files_sent.append({"doc": file.doc, "title": file.title, "file": file.sha256.hex(), "bytes": file.size_bytes})
    return {"call": call_number, "method": settings.method, "model": settings.model, "effort": settings.effort,
            "questions": keys, "asked": asked, "pages": page_refs(pages), "files": files_sent, "request": sent,
            "served_by": call.served_by,
            "stop_reason": call.stop_reason, "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
            "cache_write_tokens": call.cache_write_tokens, "cache_read_tokens": call.cache_read_tokens,
            "cost_usd": _cost_for_trace(call), "error": call.error, "answer": call.raw_text}


def _cost_for_trace(call: "CallResult") -> str | None:
    """The call's cost as a decimal string (JSON has no exact decimals), None when nothing was billed or
    a model it ran on is not in the price list."""
    if not call.costs or pricing.unpriced_count(call.costs):
        return None
    return str(pricing.total_cost(call.costs))


def _verification_data(evidence: dict[str, Evidence]) -> dict:
    checked = []
    for key, item in evidence.items():
        cited = None
        if item.page is not None:
            cited = {"doc": item.page.doc, "file": item.page.file.hex(), "page": item.page.page_no}
        checked.append({"key": key, "found": item.found, "kind": item.kind, "quote": item.quote, "cited": cited,
                        "verified": item.verified, "whole_document": item.whole})
    return {"evidence": checked}


def page_ref(page: Page) -> dict:
    return {"file": page.file.hex(), "page": page.page_no}


def page_refs(pages: list[Page]) -> list[dict]:
    refs = []
    for page in pages:
        refs.append(page_ref(page))
    return refs


def found_value(raw: object) -> bool | None:
    """The model's "found" answer: True, False, or None for "dont_know" (and anything unreadable)."""
    if raw == "yes" or raw is True:
        return True
    if raw == "no" or raw is False:
        return False
    return None


def questions_to_widen(questions: list[Question], first_answers: dict[str, dict], whole_first: bool,
                       not_reported_kinds: frozenset[str] = frozenset()) -> list[Question]:
    """Questions to ask again with every page (none if all were sent): those not found in the
    retrieved pages or that the model didn't know about there, and endpoints found there only as not
    reported, whose numeric result may sit on a page retrieval missed."""
    retry = []
    if whole_first:
        return retry
    for question in questions:
        answer = first_answers.get(question.key)
        found = None
        kind = None
        if answer is not None:
            found = found_value(answer.get("found"))
            kind = answer.get("kind")
        if found is not True:
            retry.append(question)
        elif question.rule == "numeric_result" and kind in not_reported_kinds:
            retry.append(question)
    return retry


def total_text_length(pages: list[Page]) -> int:
    total = 0
    for page in pages:
        total += len(page.text)
    return total


def text_or_empty(value: str | None) -> str:
    if not value:
        return ""
    return value


def evidence_from_answer(key: str, answer: dict, whole: bool, pages_by_ref: dict[tuple, Page]) -> Evidence:
    """One model answer as Evidence, with its quote checked against the page it cites."""
    found = found_value(answer.get("found"))
    quote = text_or_empty(answer.get("quote"))
    note = text_or_empty(answer.get("note"))
    page = None
    if found is True:
        page = pages_by_ref.get((answer.get("document"), answer.get("page")))
    verified = False
    if found is True:
        verified = verify(quote, page)
    return Evidence(key, found, answer.get("kind"), quote, page, note, verified, whole)


def preview_trial(conn: psycopg.Connection, trial_id: int, requirement_set: RequirementSet
                  ) -> tuple[list[Question], list[Page]]:
    """The questions and pages a check of this trial would use now, without calling anything (dry runs)."""
    record = core.trial_current_record(conn, trial_id)
    pages = trial_pages(conn, trial_id, requirement_set)
    questions = []
    if record is not None:
        questions = requirements.questions_for(requirement_set, record.registered)
    return questions, pages


def trial_context(conn: psycopg.Connection, trial_id: int, record, pages: list[Page]) -> dict:
    """The trial and its documents as the model is told about them, and as the trace records them."""
    name = core.trial_names(conn, [trial_id])[trial_id]
    title = None
    source = None
    if record is not None:
        title = record.registered.get("public_title")
        if not title:
            title = record.registered.get("full_title")
        source = record.source
    documents = []
    seen = set()
    for page in pages:
        if page.doc not in seen:
            seen.add(page.doc)
            document_title = page.title
            if not document_title:
                document_title = "untitled"
            documents.append({"doc": page.doc, "title": document_title, "kind": page.kind})
    return {"name": name, "title": title, "source": source, "documents": documents}


def _ask_with_text(conn: psycopg.Connection, client: MessagesClient, settings: Settings, prompt: Prompt,
                   requirement_set: RequirementSet, questions: list[Question], context: dict, pages: list[Page],
                   readable: bool, result: "TrialResult", trace: "Trace") -> tuple[dict, list[Page], set] | None:
    """The text method: send page text (retrieved pages, or every page of a small document set), then
    ask again with every page what retrieved pages could not settle. Returns (answers with whether the
    model read the whole document, pages of the first call, widened keys), or None on a failed call
    (result.error says why)."""
    per_question = {}
    if len(pages) <= settings.whole_pages:
        for question in questions:
            per_question[question.key] = pages
        trace.add("retrieval", {"whole": True, "reason": f"{len(pages)} pages, at most {settings.whole_pages}"})
    else:
        searched = []
        for question in questions:
            found = search(conn, pages, search_query_for(question), settings.top_k)
            per_question[question.key] = found.chosen
            searched.append({"key": question.key, "terms": found.terms, "ranked": found.ranked,
                             "chosen": page_refs(found.chosen)})
        trace.add("retrieval", {"whole": False, "top_k": settings.top_k, "questions": searched})
    first = pages_to_send_first(pages, per_question)
    whole_first = len(first) == len(pages)
    first_request = build_request(settings, prompt, first, questions, context)
    call = ask(client, first_request)
    trace.add("call", _call_data(1, settings, first, questions, first_request, call), call.duration_ms)
    count_call(result, 1, call)
    if call.error:
        result.error = call.error
        return None
    # each answer is kept with whether the model read the whole document when it gave it
    first_whole = whole_first and readable
    answers = {}
    for key, answer in call.answers.items():
        answers[key] = (answer, first_whole)

    # widening: questions not settled from retrieved pages are asked again with every page
    retry = questions_to_widen(questions, call.answers, whole_first, requirement_set.not_reported_kinds)
    widened_keys = set()
    fits = total_text_length(pages) <= settings.max_text_chars
    if retry and not fits:
        trace.add("widening", {"skipped": "the text of every page is too long to send", "questions":
                               question_keys(retry)})
    if retry and fits:
        second_request = build_request(settings, prompt, pages, retry, context)
        again = ask(client, second_request)
        trace.add("call", _call_data(2, settings, pages, retry, second_request, again), again.duration_ms)
        count_call(result, 2, again)
        if again.error:
            # recorded, not dropped: judging on the first answers alone would pass a failed call off as a
            # retrieval miss, so the trial is stored as failed and can be run again
            result.error = f"widening: {again.error}"
            return None
        for question in retry:
            if question.key in again.answers:
                answers[question.key] = (again.answers[question.key], readable)
                widened_keys.add(question.key)
        result.widened = len(widened_keys)

    return answers, first, widened_keys


def _ask_with_files(conn: psycopg.Connection, client: MessagesClient, settings: Settings, prompt: Prompt,
                    requirement_set: RequirementSet, trial_id: int, questions: list[Question], context: dict,
                    pages: list[Page], result: "TrialResult", trace: "Trace") -> tuple[dict, list[Page], set] | None:
    """The files method: send every document's file whole in one call. The model reads every page of
    every file sent (scanned ones too), so its answers count as the whole document unless a document
    could not be sent. Quotes are still verified against the extracted page text."""
    files = core.trial_files(conn, trial_id, requirement_set.document_kinds)
    sendable = []
    not_sent = []
    size = 0
    for file in files:
        if is_pdf(file):
            sendable.append(file)
            size += file.size_bytes
        else:
            not_sent.append({"doc": file.doc, "title": file.title, "reason": f"not a PDF ({file.media_type})"})
    documents = []
    for file in sendable:
        documents.append({"doc": file.doc, "title": file.title, "kind": file.kind})
    context = dict(context)
    context["documents"] = documents
    trace.add("retrieval", {"whole": True, "method": "files", "reason": f"whole files sent: {len(sendable)}",
                            "not_sent": not_sent})
    if not sendable:
        result.error = "no PDF file among the documents the requirements read"
        return None
    if size > MAX_FILES_BYTES:
        result.error = f"too large to send in one request: {size / 2**20:.1f} MB of files"
        return None
    # the files sent are the inputs read, whether their page text was extracted or not
    for file in sendable:
        if file.document_version_id not in result.document_versions:
            result.document_versions.append(file.document_version_id)
    request = build_files_request(settings, prompt, sendable, questions, context)
    call = ask(client, request)
    trace.add("call", _call_data(1, settings, pages, questions, request, call, sendable), call.duration_ms)
    count_call(result, 1, call)
    if call.error:
        result.error = call.error
        return None
    whole = not not_sent
    answers = {}
    for key, answer in call.answers.items():
        answers[key] = (answer, whole)
    return answers, pages, set()


def check_trial(conn: psycopg.Connection, client: MessagesClient, run_id: int, trial_id: int,
                settings: Settings, prompt: Prompt, requirement_set: RequirementSet) -> TrialResult:
    """Judge one trial of the central model against a requirement set, and store its questions,
    evidence, judgement, inputs and trace."""
    started = time.monotonic()
    result = TrialResult()
    trace = Trace()
    # what the check reads at this moment: the latest trial record and the current document versions
    record = core.trial_current_record(conn, trial_id)
    pages = trial_pages(conn, trial_id, requirement_set)
    unread = core.unread_documents(conn, trial_id, requirement_set.document_kinds)
    if record is not None:
        result.record_id = record.id
    for page in pages:
        if page.document_version_id not in result.document_versions:
            result.document_versions.append(page.document_version_id)
    questions = []
    if record is not None:
        questions = requirements.questions_for(requirement_set, record.registered)
    context = trial_context(conn, trial_id, record, pages)
    trace.add("questions", _questions_data(questions, requirement_set, context, record))
    # a page without readable text, or a linked document not extracted yet, can't have been read, so
    # then no answer counts as "the whole document"
    readable = every_page_readable(pages) and not unread
    trace.add("pages", _pages_data(pages, unread, readable))
    if record is None:
        result.error = "no registered information (no source has given a record for this trial)"
    elif settings.method == "files":
        # the files method reads the files themselves (scanned pages too); it checks them when it sends them
        result.error = None
    elif not pages:
        kinds = ", ".join(requirement_set.document_kinds)
        result.error = f"no page text in documents of the kinds the requirements read ({kinds})"
    elif not some_page_readable(pages):
        # a scanned PDF without a text layer: nothing to quote, so no call; a person decides
        result.error = "no readable page text in its documents (scanned PDF?)"
    if result.error:
        _finish_failed(conn, run_id, trial_id, result, trace, started)
        return result

    if settings.method == "files":
        asked = _ask_with_files(conn, client, settings, prompt, requirement_set, trial_id, questions, context, pages,
                                result, trace)
    else:
        asked = _ask_with_text(conn, client, settings, prompt, requirement_set, questions, context, pages, readable,
                               result, trace)
    # what was billed is recorded at once, whatever happens to the trial next
    record_calls(conn, run_id, trial_id, result)
    if asked is None:
        _finish_failed(conn, run_id, trial_id, result, trace, started)
        return result
    answers = asked[0]
    first = asked[1]
    widened_keys = asked[2]

    pages_by_ref = {}
    for page in pages:
        pages_by_ref[(page.doc, page.page_no)] = page
    evidence: dict[str, Evidence] = {}
    for key, answer_and_whole in answers.items():
        answer = answer_and_whole[0]
        whole = answer_and_whole[1]
        evidence[key] = evidence_from_answer(key, answer, whole, pages_by_ref)
    trace.add("verification", _verification_data(evidence))
    judged, rules_trace = requirements.judge(requirement_set, questions, evidence)
    trace.add("rules", {"requirements": requirement_set.name, "parts": judged["parts"], "label": judged["label"],
                        "trace": rules_trace})
    result.label = judged["label"]
    for item in evidence.values():
        if item.found is True:
            result.evidence += 1
        if item.verified:
            result.verified += 1

    with conn.transaction():
        for question in questions:
            widened = question.key in widened_keys
            if widened:
                sent_pages = pages
            else:
                # the first call showed the model every page any question needed
                sent_pages = first
            checks_db.save_question(conn, run_id, trial_id, question.key, question.item_text,
                                    page_refs(sent_pages), widened)
            item = evidence.get(question.key)
            if item is None:
                continue
            # empty text and a missing page are stored as NULL
            quote = None
            if item.quote:
                quote = item.quote
            note = None
            if item.note:
                note = item.note
            version_id = None
            file_sha256 = None
            page_no = None
            if item.page:
                version_id = item.page.document_version_id
                file_sha256 = item.page.file
                page_no = item.page.page_no
            evidence_id = checks_db.save_evidence(
                conn, run_id, trial_id, question.key, found=item.found, kind=item.kind, quote=quote,
                document_version_id=version_id, file_sha256=file_sha256, page_no=page_no, note=note,
                verified=item.verified,
            )
            if question.key in rules_trace:
                rules_trace[question.key]["evidence_id"] = evidence_id
        _save_judgement(conn, run_id, trial_id, judged, rules_trace, result, started)
        checks_db.save_steps(conn, run_id, trial_id, trace.steps)
    return result


def _finish_failed(conn: psycopg.Connection, run_id: int, trial_id: int, result: TrialResult, trace: Trace,
                   started: float) -> None:
    trace.add("error", {"error": result.error})
    with conn.transaction():
        _save_judgement(conn, run_id, trial_id, None, None, result, started)
        checks_db.save_steps(conn, run_id, trial_id, trace.steps)


def _save_judgement(conn: psycopg.Connection, run_id: int, trial_id: int, judged: dict | None, trace: dict | None,
                    result: TrialResult, started: float) -> None:
    # a trial that failed before judging is stored with no parts and no label
    parts = {}
    label = None
    if judged:
        parts = judged["parts"]
        label = judged["label"]
    # a trial with no call cost nothing; one whose calls ran on a model missing from the price list
    # has no known cost
    cost = None
    billed = trial_costs(result)
    if not billed:
        cost = pricing.total_cost(billed)
    elif pricing.unpriced_count(billed) == 0:
        cost = pricing.total_cost(billed)
    checks_db.save_judgement(conn, run_id, trial_id, parts=parts, label=label, trace=trace,
                             served_by=result.served_by, record_id=result.record_id,
                             document_versions=result.document_versions, input_tokens=result.input_tokens,
                             output_tokens=result.output_tokens, cache_write_tokens=result.cache_write_tokens,
                             cache_read_tokens=result.cache_read_tokens, cost_usd=cost,
                             duration_ms=_milliseconds_since(started), error=result.error)


# --- a run -----------------------------------------------------------------------------------

@dataclass
class RunSummary:
    run_id: int
    labels: dict[str, int] = field(default_factory=dict)
    # trial name (EU CT number where there is one) -> error
    failed: dict[str, str] = field(default_factory=dict)
    evidence: int = 0
    verified: int = 0
    widened: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    # what the run's model calls cost (priced ones), and how many ran on a model not in the price list
    cost_usd: Decimal = Decimal(0)
    unpriced_calls: int = 0
    # trials whose judgement the run published by itself (checks/publishing.py)
    published: list[int] = field(default_factory=list)

    def summary(self) -> str:
        label_counts = []
        for label, count in sorted(self.labels.items()):
            label_counts.append(f"{label} {count}")
        return (f"run {self.run_id}: {', '.join(label_counts)}; failed {len(self.failed)}; evidence quotes "
                f"{self.evidence}, verified on the page {self.verified}; questions widened to every page "
                f"{self.widened}; tokens in {self.input_tokens:,}, out {self.output_tokens:,}; "
                f"cost {pricing.dollars(self.cost_usd)}{self._unpriced()}")

    def _unpriced(self) -> str:
        if not self.unpriced_calls:
            return ""
        return f" (+{self.unpriced_calls} calls on models not in the price list)"


class NotReady(ValueError):
    """Some trials' evidence is not complete and current at every source that holds them (ADR-005,
    sources/stages.py not_ready)."""

    def __init__(self, reasons: dict[str, str]) -> None:
        listed = []
        for name, reason in reasons.items():
            listed.append(f"{name} ({reason})")
        super().__init__(f"not checked, their evidence is not complete: {'; '.join(listed)}")
        self.reasons = reasons


def refuse_unready(conn: psycopg.Connection, trial_ids: list[int], requirement_set: RequirementSet) -> None:
    """Raise NotReady when one of these trials may not be checked against the set yet."""
    reasons = stages.not_ready(conn, trial_ids, requirement_set.document_kinds)
    if not reasons:
        return
    names = core.trial_names(conn, list(reasons))
    by_name = {}
    for trial_id, reason in reasons.items():
        by_name[names[trial_id]] = reason
    raise NotReady(by_name)


def run(conn: psycopg.Connection, client: MessagesClient, trial_ids: Iterable[int], settings: Settings,
        prompt: Prompt, progress: Progress = None, requirement_set: RequirementSet | None = None,
        should_stop: Callable[[], bool] | None = None) -> RunSummary:
    """Judge each trial (core.trial ids, each once) in one run, as the session's actor, with a named
    setup's settings (checks/settings.py), against its requirement set (or the one given). Raises NotReady, before anything is sent, when a
    trial's evidence is not complete and current (sources/stages.py not_ready). `should_stop` is asked
    between trials."""
    if requirement_set is None:
        requirement_set = requirements.load(settings.requirements)
    wanted = []
    for trial_id in trial_ids:
        if trial_id not in wanted:
            wanted.append(trial_id)
    refuse_unready(conn, wanted, requirement_set)
    names = core.trial_names(conn, wanted)
    # runs whose process died are closed first, so they never look as if they were still going
    checks_db.close_abandoned_runs(conn)
    run_id = checks_db.start_run(
        conn, check_name=CHECK_NAME, model=settings.model, effort=settings.effort, prompt=prompt.name,
        prompt_sha256=prompt.sha256, prompt_text=prompt.text, requirements=requirement_set.name,
        requirements_sha256=requirement_set.sha256, requirements_text=requirement_set.text, params=settings.params(),
    )
    summary = RunSummary(run_id)
    report(progress, f"run {run_id} started")
    status = "stopped"
    stop_error = None
    try:
        stopped = False
        for number, trial_id in enumerate(wanted, start=1):
            if should_stop is not None and should_stop():
                stopped = True
                report(progress, "stopped: the trials judged so far are kept")
                break
            name = names[trial_id]
            trial_result = check_trial(conn, client, run_id, trial_id, settings, prompt, requirement_set)
            if trial_result.error:
                report(progress, f"{number}/{len(wanted)} {name}: error: {trial_result.error}")
            else:
                report(progress, f"{number}/{len(wanted)} {name}: {trial_result.label}")
            summary.input_tokens += trial_result.input_tokens
            summary.output_tokens += trial_result.output_tokens
            summary.cache_write_tokens += trial_result.cache_write_tokens
            summary.cache_read_tokens += trial_result.cache_read_tokens
            billed = trial_costs(trial_result)
            summary.cost_usd += pricing.total_cost(billed)
            summary.unpriced_calls += pricing.unpriced_count(billed)
            summary.evidence += trial_result.evidence
            summary.verified += trial_result.verified
            summary.widened += trial_result.widened
            if trial_result.error:
                summary.failed[name] = trial_result.error
            else:
                if trial_result.label not in summary.labels:
                    summary.labels[trial_result.label] = 0
                summary.labels[trial_result.label] += 1
        if stopped:
            status = "stopped"
        elif summary.failed:
            status = "failed"
        else:
            status = "succeeded"
    except BaseException as exc:
        # e.g. bad credentials or Ctrl-C: the run is closed as stopped, so it never looks complete
        stop_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        judged = 0
        for count in summary.labels.values():
            judged += count
        trials = judged + len(summary.failed)
        checks_db.finish_run(conn, run_id, status=status, error=stop_error, trials=trials,
                             errors=len(summary.failed), input_tokens=summary.input_tokens,
                             output_tokens=summary.output_tokens, cache_write_tokens=summary.cache_write_tokens,
                             cache_read_tokens=summary.cache_read_tokens)
        # every judgement it made goes public now, also when the run stopped part-way (ADR-007)
        try:
            summary.published = publishing.publish_run(conn, run_id)
        except Exception:
            if stop_error is None:
                raise
            # the run is already failing: its own error is the one to raise
    report(progress, f"published {len(summary.published)} of {judged} judgements, each with how judgements "
                     f"of its kind agreed with the gold datasets")
    return summary
