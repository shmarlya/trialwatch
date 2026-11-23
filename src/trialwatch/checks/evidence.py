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

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg

from trialwatch import llm
from trialwatch.checks import publishing, requirements
from trialwatch.checks import trace as tracing
from trialwatch.checks.ask import (MAX_FILES_BYTES, OPERATION, ask, build_files_request, build_request,
                                   found_value, is_pdf, question_keys)
from trialwatch.checks.prompts import Prompt, load_prompt
from trialwatch.checks.requirements import Question, RequirementSet
from trialwatch.checks.retrieval import pages_to_send_first, search, search_query_for
from trialwatch.checks.settings import Settings
from trialwatch.checks.verify import every_page_readable, has_readable_text, some_page_readable, verify
from trialwatch.db import checks as checks_db
from trialwatch.db import core
from trialwatch.db.core import Page
from trialwatch.llm import CallResult, MessagesClient
from trialwatch.progress import Progress, report
from trialwatch.trials import readiness

CHECK_NAME = "evidence"


def trial_pages(conn: psycopg.Connection, trial_id: int, requirement_set: RequirementSet) -> list[Page]:
    """Every page of the current version of each document of the set's kinds linked to the trial."""
    return core.trial_pages(conn, trial_id, requirement_set.document_kinds)


# --- extraction ------------------------------------------------------------------------------


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


def trial_costs(result: TrialResult) -> list[llm.CallCost]:
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
        llm.record(conn, call, operation=OPERATION, check_run_id=run_id, trial_id=trial_id, call_number=number)


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


@dataclass(frozen=True)
class TrialPreview:
    """What a check of one trial would use now, for a dry run."""

    name: str
    questions: list[Question]
    pages: list[Page]
    # pages with text to quote (a scanned page has none)
    readable: int


def preview(conn: psycopg.Connection, trial_ids: list[int], settings: Settings) -> list[TrialPreview]:
    """The questions and pages a check of each trial would use now, calling nothing."""
    requirement_set = requirements.load(settings.requirements)
    names = core.trial_names(conn, trial_ids)
    previews = []
    for trial_id in trial_ids:
        questions, pages = preview_trial(conn, trial_id, requirement_set)
        readable = len([page for page in pages if has_readable_text(page)])
        previews.append(TrialPreview(names[trial_id], questions, pages, readable))
    return previews


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
                   readable: bool, result: "TrialResult", trace: "tracing.Trace") -> tuple[dict, list[Page], set] | None:
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
                             "chosen": tracing.page_refs(found.chosen)})
        trace.add("retrieval", {"whole": False, "top_k": settings.top_k, "questions": searched})
    first = pages_to_send_first(pages, per_question)
    whole_first = len(first) == len(pages)
    first_request = build_request(settings, prompt, first, questions, context)
    call, call_answers = ask(client, first_request)
    trace.add("call", tracing.call_data(1, settings, first, questions, first_request, call), call.duration_ms)
    count_call(result, 1, call)
    if call.error:
        result.error = call.error
        return None
    # each answer is kept with whether the model read the whole document when it gave it
    first_whole = whole_first and readable
    answers = {}
    for key, answer in call_answers.items():
        answers[key] = (answer, first_whole)

    # widening: questions not settled from retrieved pages are asked again with every page
    retry = questions_to_widen(questions, call_answers, whole_first, requirement_set.not_reported_kinds)
    widened_keys = set()
    fits = total_text_length(pages) <= settings.max_text_chars
    if retry and not fits:
        trace.add("widening", {"skipped": "the text of every page is too long to send", "questions":
                               question_keys(retry)})
    if retry and fits:
        second_request = build_request(settings, prompt, pages, retry, context)
        again, again_answers = ask(client, second_request)
        trace.add("call", tracing.call_data(2, settings, pages, retry, second_request, again), again.duration_ms)
        count_call(result, 2, again)
        if again.error:
            # recorded, not dropped: judging on the first answers alone would pass a failed call off as a
            # retrieval miss, so the trial is stored as failed and can be run again
            result.error = f"widening: {again.error}"
            return None
        for question in retry:
            if question.key in again_answers:
                answers[question.key] = (again_answers[question.key], readable)
                widened_keys.add(question.key)
        result.widened = len(widened_keys)

    return answers, first, widened_keys


def _ask_with_files(conn: psycopg.Connection, client: MessagesClient, settings: Settings, prompt: Prompt,
                    requirement_set: RequirementSet, trial_id: int, questions: list[Question], context: dict,
                    pages: list[Page], result: "TrialResult", trace: "tracing.Trace") -> tuple[dict, list[Page], set] | None:
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
    call, call_answers = ask(client, request)
    trace.add("call", tracing.call_data(1, settings, pages, questions, request, call, sendable), call.duration_ms)
    count_call(result, 1, call)
    if call.error:
        result.error = call.error
        return None
    whole = not not_sent
    answers = {}
    for key, answer in call_answers.items():
        answers[key] = (answer, whole)
    return answers, pages, set()


@dataclass
class Inputs:
    """What a check of one trial reads at this moment."""

    # the trial's latest record (its registered information); None: no source gave one
    record: core.CurrentRecord | None
    # the page text of the current versions of its documents of the kinds the requirement set reads
    pages: list[Page]
    # documents of those kinds whose page text is not extracted yet
    unread: list[str]
    # what the model is told about the trial and its documents
    context: dict
    # every page has readable text and nothing is unread: an answer from every page counts as the whole document
    readable: bool


def check_trial(conn: psycopg.Connection, client: MessagesClient, run_id: int, trial_id: int,
                settings: Settings, prompt: Prompt, requirement_set: RequirementSet) -> TrialResult:
    """Judge one trial of the central model against a requirement set, in steps, each in its trace:
    gather the inputs and the questions, ask the model (page text or whole files; questions retrieval
    could not settle are asked again with every page), verify the quotes, judge with the rules, save
    the questions, evidence, judgement, inputs and trace."""
    started = time.monotonic()
    result = TrialResult()
    trace = tracing.Trace()
    inputs = _gather(conn, trial_id, requirement_set, result)
    questions = _questions(requirement_set, inputs, trace)
    result.error = _why_not_asked(settings, requirement_set, inputs)
    if result.error:
        _finish_failed(conn, run_id, trial_id, result, trace, started)
        return result
    asked = _ask(conn, client, settings, prompt, requirement_set, trial_id, questions, inputs, result, trace)
    # what was billed is recorded at once, whatever happens to the trial next
    record_calls(conn, run_id, trial_id, result)
    if asked is None:
        _finish_failed(conn, run_id, trial_id, result, trace, started)
        return result
    answers, first, widened_keys = asked
    evidence = _verify(answers, inputs.pages, trace)
    judged, rules_trace = _judge(requirement_set, questions, evidence, result, trace)
    _save(conn, run_id, trial_id, questions, evidence, inputs.pages, first, widened_keys, judged, rules_trace, result,
          trace, started)
    return result


def _gather(conn: psycopg.Connection, trial_id: int, requirement_set: RequirementSet, result: TrialResult) -> Inputs:
    """What the check reads at this moment: the latest trial record and the current document versions."""
    record = core.trial_current_record(conn, trial_id)
    pages = trial_pages(conn, trial_id, requirement_set)
    unread = core.unread_documents(conn, trial_id, requirement_set.document_kinds)
    if record is not None:
        result.record_id = record.id
    for page in pages:
        if page.document_version_id not in result.document_versions:
            result.document_versions.append(page.document_version_id)
    context = trial_context(conn, trial_id, record, pages)
    # a page without readable text, or a linked document not extracted yet, can't have been read, so
    # then no answer counts as "the whole document"
    readable = every_page_readable(pages) and not unread
    return Inputs(record, pages, unread, context, readable)


def _questions(requirement_set: RequirementSet, inputs: Inputs, trace: tracing.Trace) -> list[Question]:
    """The questions the requirement set asks of this trial's registered information; the trace's first
    two steps: the questions, and the pages read."""
    questions = []
    if inputs.record is not None:
        questions = requirements.questions_for(requirement_set, inputs.record.registered)
    trace.add("questions", tracing.questions_data(questions, requirement_set, inputs.context, inputs.record))
    trace.add("pages", tracing.pages_data(inputs.pages, inputs.unread, inputs.readable))
    return questions


def _why_not_asked(settings: Settings, requirement_set: RequirementSet, inputs: Inputs) -> str | None:
    """Why the model can't be asked about this trial, or None."""
    if inputs.record is None:
        return "no registered information (no source has given a record for this trial)"
    if settings.method == "files":
        # the files method reads the files themselves (scanned pages too); it checks them when it sends them
        return None
    if not inputs.pages:
        kinds = ", ".join(requirement_set.document_kinds)
        return f"no page text in documents of the kinds the requirements read ({kinds})"
    if not some_page_readable(inputs.pages):
        # a scanned PDF without a text layer: nothing to quote, so no call; a person decides
        return "no readable page text in its documents (scanned PDF?)"
    return None


def _ask(conn: psycopg.Connection, client: MessagesClient, settings: Settings, prompt: Prompt,
         requirement_set: RequirementSet, trial_id: int, questions: list[Question], inputs: Inputs,
         result: TrialResult, trace: tracing.Trace) -> tuple[dict, list[Page], set] | None:
    """The model's answers, by the setup's method: page text (retrieved, widened) or whole files."""
    if settings.method == "files":
        return _ask_with_files(conn, client, settings, prompt, requirement_set, trial_id, questions, inputs.context,
                               inputs.pages, result, trace)
    return _ask_with_text(conn, client, settings, prompt, requirement_set, questions, inputs.context, inputs.pages,
                          inputs.readable, result, trace)


def _verify(answers: dict, pages: list[Page], trace: tracing.Trace) -> dict[str, Evidence]:
    """Each answer as evidence, its quote checked against the page it cites."""
    pages_by_ref = {}
    for page in pages:
        pages_by_ref[(page.doc, page.page_no)] = page
    evidence: dict[str, Evidence] = {}
    for key, answer_and_whole in answers.items():
        answer = answer_and_whole[0]
        whole = answer_and_whole[1]
        evidence[key] = evidence_from_answer(key, answer, whole, pages_by_ref)
    trace.add("verification", tracing.verification_data(evidence))
    return evidence


def _judge(requirement_set: RequirementSet, questions: list[Question], evidence: dict[str, Evidence],
           result: TrialResult, trace: tracing.Trace) -> tuple[dict, dict]:
    """The requirement set's rules on the verified evidence: its parts and label, and how each was set."""
    judged, rules_trace = requirements.judge(requirement_set, questions, evidence)
    trace.add("rules", {"requirements": requirement_set.name, "parts": judged["parts"], "label": judged["label"],
                        "trace": rules_trace})
    result.label = judged["label"]
    for item in evidence.values():
        if item.found is True:
            result.evidence += 1
        if item.verified:
            result.verified += 1
    return judged, rules_trace


def _save(conn: psycopg.Connection, run_id: int, trial_id: int, questions: list[Question],
          evidence: dict[str, Evidence], pages: list[Page], first: list[Page], widened_keys: set, judged: dict,
          rules_trace: dict, result: TrialResult, trace: tracing.Trace, started: float) -> None:
    """The questions with the pages shown for each, the evidence, the judgement and the trace, at once."""
    with conn.transaction():
        for question in questions:
            widened = question.key in widened_keys
            if widened:
                sent_pages = pages
            else:
                # the first call showed the model every page any question needed
                sent_pages = first
            checks_db.save_question(conn, run_id, trial_id, question.key, question.item_text,
                                    tracing.page_refs(sent_pages), widened)
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


def _finish_failed(conn: psycopg.Connection, run_id: int, trial_id: int, result: TrialResult, trace: tracing.Trace,
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
        cost = llm.total_cost(billed)
    elif llm.unpriced_count(billed) == 0:
        cost = llm.total_cost(billed)
    checks_db.save_judgement(conn, run_id, trial_id, parts=parts, label=label, trace=trace,
                             served_by=result.served_by, record_id=result.record_id,
                             document_versions=result.document_versions, input_tokens=result.input_tokens,
                             output_tokens=result.output_tokens, cache_write_tokens=result.cache_write_tokens,
                             cache_read_tokens=result.cache_read_tokens, cost_usd=cost,
                             duration_ms=_milliseconds_since(started), error=result.error)


# --- a run -----------------------------------------------------------------------------------

@dataclass
class RunSummary:
    # None when no trial was left to check (no run was started)
    run_id: int | None
    labels: dict[str, int] = field(default_factory=dict)
    # trials left out because their evidence is not complete and current: name -> why (unready=SKIP)
    skipped: dict[str, str] = field(default_factory=dict)
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
        if self.run_id is None:
            return f"no run: no trial left to check ({len(self.skipped)} skipped, their evidence not complete)"
        label_counts = []
        for label, count in sorted(self.labels.items()):
            label_counts.append(f"{label} {count}")
        return (f"run {self.run_id}: {', '.join(label_counts)}; failed {len(self.failed)}; evidence quotes "
                f"{self.evidence}, verified on the page {self.verified}; questions widened to every page "
                f"{self.widened}; tokens in {self.input_tokens:,}, out {self.output_tokens:,}; "
                f"cost {llm.dollars(self.cost_usd)}{self._unpriced()}")

    def _unpriced(self) -> str:
        if not self.unpriced_calls:
            return ""
        return f" (+{self.unpriced_calls} calls on models not in the price list)"


class NotReady(ValueError):
    """Some trials' evidence is not complete and current at every source that holds them (ADR-005,
    trials/readiness.py not_ready)."""

    def __init__(self, reasons: dict[str, str]) -> None:
        listed = []
        for name, reason in reasons.items():
            listed.append(f"{name} ({reason})")
        super().__init__(f"not checked, their evidence is not complete: {'; '.join(listed)}")
        self.reasons = reasons


def _checkable(conn: psycopg.Connection, trial_ids: Iterable[int], requirement_set: RequirementSet, unready: str,
               summary: RunSummary, progress: Progress) -> list[int]:
    """The trials to check, each once: every one, when all are ready; else NotReady (REFUSE), or the ready
    ones with the others in the summary's `skipped` (SKIP)."""
    wanted = []
    for trial_id in trial_ids:
        if trial_id not in wanted:
            wanted.append(trial_id)
    if unready == REFUSE:
        refuse_unready(conn, wanted, requirement_set)
        return wanted
    reasons = readiness.not_ready(conn, wanted, requirement_set.document_kinds)
    if not reasons:
        return wanted
    names = core.trial_names(conn, list(reasons))
    ready = []
    for trial_id in wanted:
        if trial_id in reasons:
            summary.skipped[names[trial_id]] = reasons[trial_id]
            report(progress, f"skipped {names[trial_id]}: {reasons[trial_id]}")
        else:
            ready.append(trial_id)
    return ready


def refuse_unready(conn: psycopg.Connection, trial_ids: list[int], requirement_set: RequirementSet) -> None:
    """Raise NotReady when one of these trials may not be checked against the set yet."""
    reasons = readiness.not_ready(conn, trial_ids, requirement_set.document_kinds)
    if not reasons:
        return
    names = core.trial_names(conn, list(reasons))
    by_name = {}
    for trial_id, reason in reasons.items():
        by_name[names[trial_id]] = reason
    raise NotReady(by_name)


# What run() does with a trial whose evidence is not complete and current (trials/readiness.py)
# a person asked for it: nothing is sent, NotReady says why (the command line, a person's job)
REFUSE = "refuse"
# chosen by TrialWatch or for an evaluation: left out, the summary says why (the refresh, gold eval)
SKIP = "skip"


def run(conn: psycopg.Connection, client: MessagesClient, trial_ids: Iterable[int], settings: Settings,
        prompt: Prompt | None = None, progress: Progress = None, requirement_set: RequirementSet | None = None,
        should_stop: Callable[[], bool] | None = None, unready: str = REFUSE) -> RunSummary:
    """The check's one entry point, for the command line, jobs and gold evaluations alike: judge each
    trial (core.trial ids, each once) in one run, as the session's actor, with a named setup's settings
    (checks/settings.py), its prompt and its requirement set (or the ones given). A trial whose evidence
    is not complete and current is refused (NotReady, before anything is sent) or skipped (`unready`);
    with no trial left, no run is started. `should_stop` is asked between trials."""
    if prompt is None:
        prompt = load_prompt(settings.prompt)
    if requirement_set is None:
        requirement_set = requirements.load(settings.requirements)
    summary = RunSummary(None)
    wanted = _checkable(conn, trial_ids, requirement_set, unready, summary, progress)
    if not wanted:
        report(progress, summary.summary())
        return summary
    names = core.trial_names(conn, wanted)
    # runs whose process died are closed first, so they never look as if they were still going
    checks_db.close_abandoned_runs(conn)
    run_id = checks_db.start_run(
        conn, check_name=CHECK_NAME, model=settings.model, effort=settings.effort, prompt=prompt.name,
        prompt_sha256=prompt.sha256, prompt_text=prompt.text, requirements=requirement_set.name,
        requirements_sha256=requirement_set.sha256, requirements_text=requirement_set.text, params=settings.params(),
    )
    summary.run_id = run_id
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
            summary.cost_usd += llm.total_cost(billed)
            summary.unpriced_calls += llm.unpriced_count(billed)
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
