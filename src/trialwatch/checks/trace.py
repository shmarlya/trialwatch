"""The trace of one judgement (ADR-004): every step of judging a trial, in order, as the trial page
shows it to anyone: the questions, the pages read, retrieval, each model call (what was asked; the
request as sent and the raw answer are its core.llm_call row, which the step names), quote
verification and the rules. Pages are referenced by file and page, not copied."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from trialwatch.checks.requirements import Question, RequirementSet
from trialwatch.checks.settings import Settings
from trialwatch.checks.verify import every_page_readable, has_readable_text
from trialwatch.db.core import Page

if TYPE_CHECKING:
    # for the type only: trialwatch.checks.evidence uses this module
    from trialwatch.checks.evidence import Evidence


class Trace:
    """The steps of judging one trial, in order, for checks.step: what was asked, searched, sent,
    answered, verified and decided. Pages are referenced by file and page number, not copied."""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def add(self, name: str, data: dict, duration_ms: int | None = None) -> None:
        self.steps.append({"name": name, "at": datetime.now(timezone.utc), "duration_ms": duration_ms, "data": data})


def questions_data(questions: list[Question], requirement_set: RequirementSet, context: dict, record) -> dict:
    listed = []
    for question in questions:
        listed.append(
            {
                "key": question.key,
                "text": question.text,
                "part": question.part,
                "rule": question.rule,
                "guidance": question.guidance,
            }
        )
    record_data = None
    if record is not None:
        record_data = {"id": record.id, "source": record.source, "fetched_at": record.fetched_at.isoformat()}
    return {
        "requirements": requirement_set.name,
        "requirements_sha256": requirement_set.sha256.hex(),
        "trial": context,
        "record": record_data,
        "questions": listed,
    }


def pages_data(pages: list[Page], unread: list[str], readable: bool) -> dict:
    documents = {}
    for page in pages:
        if page.doc not in documents:
            documents[page.doc] = {
                "doc": page.doc,
                "title": page.title,
                "kind": page.kind,
                "file": page.file.hex(),
                "pages": 0,
                "unreadable_pages": [],
            }
        documents[page.doc]["pages"] += 1
        if not has_readable_text(page):
            documents[page.doc]["unreadable_pages"].append(page.page_no)
    listed = []
    for document in documents.values():
        listed.append(document)
    return {
        "documents": listed,
        "total_pages": len(pages),
        "every_page_readable": every_page_readable(pages),
        # answers from every page count as reading the whole document (the setup may allow a page or so
        # without readable text)
        "read_whole": readable,
        "documents_without_text": unread,
    }


def call_data(
    call_number: int,
    settings: Settings,
    pages: list[Page],
    questions: list[Question],
    files: list | None = None,
) -> dict:
    """One model call for the trace: what was asked, of which pages or files. The call itself (the
    request exactly as sent, the answer exactly as received) is recorded with the trial's calls, and the
    step names it (`llm_call`)."""
    keys = []
    asked = []
    for question in questions:
        keys.append(question.key)
        asked.append({"key": question.key, "text": question.text})
    files_sent = []
    if files:
        for file in files:
            files_sent.append(
                {"doc": file.doc, "title": file.title, "file": file.sha256.hex(), "bytes": file.size_bytes}
            )
    return {
        "call": call_number,
        "method": settings.method,
        "model": settings.model,
        "effort": settings.effort,
        "questions": keys,
        "asked": asked,
        "pages": page_refs(pages),
        "files": files_sent,
    }


def verification_data(evidence: "dict[str, Evidence]") -> dict:
    checked = []
    for key, item in evidence.items():
        cited = None
        if item.page is not None:
            cited = {"doc": item.page.doc, "file": item.page.file.hex(), "page": item.page.page_no}
        checked.append(
            {
                "key": key,
                "found": item.found,
                "kind": item.kind,
                "quote": item.quote,
                "cited": cited,
                "verified": item.verified,
                "whole_document": item.whole,
            }
        )
        if item.cited_page_no is not None:
            # the quote was found on a page near the one the model cited
            checked[-1]["cited_page"] = item.cited_page_no
    return {"evidence": checked}


def page_ref(page: Page) -> dict:
    return {"file": page.file.hex(), "page": page.page_no}


def page_refs(pages: list[Page]) -> list[dict]:
    refs = []
    for page in pages:
        refs.append(page_ref(page))
    return refs
