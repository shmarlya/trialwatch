"""Asking the model (ADR-004, step 3): the request of one call (the trial, its documents as page text or
as whole files, the questions) and the answer to each question by its key. The model quotes, it does
not judge: whether a quote is really on its page and what the answers mean is decided in code
(checks/verify.py, checks/requirements.py)."""

import base64
import html
import json
import re
from typing import Any

from trialwatch import llm
from trialwatch.checks.prompts import Prompt
from trialwatch.checks.requirements import KINDS, Question
from trialwatch.checks.settings import Settings
from trialwatch.db import core
from trialwatch.db.core import Page
from trialwatch.llm import FALLBACK_BETA, CallResult, MessagesClient, uses_fallbacks


# What its model calls are for, in core.llm_call
OPERATION = "check"


# The model's three answers to "did you find it?": "dont_know" is always allowed and never a guess
FOUND_ANSWERS = ("yes", "no", "dont_know")


# A request may be 32 MB; files travel base64-encoded (4/3 larger), plus room for the questions
MAX_FILES_BYTES = 23 * 1024 * 1024


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


def answer_schema() -> dict:
    """The answers' JSON schema: the same for every request, so Anthropic compiles its grammar once. A
    trial's question keys are not in it (an enum of them made each trial's schema new, and a batch of them
    ran into the limit on grammar compilations per minute); ask() keeps only the keys it asked."""
    item = {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
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
    return {
        "type": "object",
        "properties": {"answers": {"type": "array", "items": item}},
        "required": ["answers"],
        "additionalProperties": False,
    }


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


def build_request(
    settings: Settings, prompt: Prompt, pages: list[Page], questions: list[Question], context: dict
) -> dict:
    """The text method's request: the trial, then its documents' page text, then the questions."""
    content = (
        f"<trial>\n{describe_trial(context)}\n</trial>\n\n"
        f"<documents>\n{render_pages(pages)}\n</documents>\n\n"
        f"Answer each of these questions once, by its key:\n{_questions_text(questions)}"
    )
    return _request(settings, prompt, questions, content)


def build_files_request(
    settings: Settings, prompt: Prompt, files: list[core.TrialFile], questions: list[Question], context: dict
) -> dict:
    """The files method's request: each document's file as a document block (numbered as in <trial>),
    then the trial and the questions as text."""
    content: list[dict] = []
    for file in files:
        encoded = base64.standard_b64encode(file.content).decode()
        content.append(
            {
                "type": "document",
                "title": f"Document {file.doc}: {file.title}",
                "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
            }
        )
    text = (
        f"<trial>\n{describe_trial(context)}\n</trial>\n\n"
        "The documents are the attached files, in the order of their numbers above; a page number is the "
        "file's own 1-based page.\n\n"
        f"Answer each of these questions once, by its key:\n{_questions_text(questions)}"
    )
    content.append({"type": "text", "text": text})
    return _request(settings, prompt, questions, content)


def _questions_text(questions: list[Question]) -> str:
    question_lines = []
    for question in questions:
        line = f"- {question.key}: {question.text}"
        if question.guidance:
            line = f"{line}\n  ({question.guidance})"
        question_lines.append(line)
    return "\n".join(question_lines)


def _request(settings: Settings, prompt: Prompt, questions: list[Question], content: str | list[dict]) -> dict:
    """What every call of the check sends, whatever the documents are: the setup's model and effort, the
    system prompt, adaptive thinking, and the answers as JSON in the schema of these questions."""
    schema = answer_schema()
    request: dict[str, Any] = {
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "system": prompt.text,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": settings.effort, "format": {"type": "json_schema", "schema": schema}},
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
                        stored["source"] = {
                            "type": "file",
                            "sha256": file.sha256.hex(),
                            "bytes": file.size_bytes,
                            "media_type": "application/pdf",
                        }
                    blocks.append(stored)
                else:
                    blocks.append(block)
            messages.append({"role": message["role"], "content": blocks})
        sent[name] = messages
    return sent


def ask(client: MessagesClient, request: dict, keys: list[str]) -> tuple[CallResult, dict[str, dict]]:
    """One model call of the check (trialwatch.llm.call), and the answer to each question by its key.
    `keys` are the questions asked: an answer under any other key is dropped."""
    result = llm.call(client, request, OPERATION)
    answers = {}
    if result.error is not None:
        return result, answers
    try:
        parsed = json.loads(result.raw_text)
        for answer in parsed["answers"]:
            key = answer["key"]
            if key not in keys:
                continue
            if key not in answers:
                answers[key] = answer
            elif _differs(answers[key], answer):
                # two different answers to one question: neither is settled by its position, a person looks
                answers[key] = {
                    "key": key,
                    "found": "dont_know",
                    "quote": "",
                    "document": 0,
                    "page": 0,
                    "kind": "other",
                    "note": "the model answered this question twice, differently",
                }
    except (ValueError, KeyError, TypeError) as exc:
        result.error = f"invalid answer: {type(exc).__name__}: {exc}"
        answers = {}
    return result, answers


def _differs(first: dict, second: dict) -> bool:
    """Two answers to one question that say different things."""
    for name in ("found", "kind", "quote", "document", "page"):
        if first.get(name) != second.get(name):
            return True
    return False


def found_value(raw: object) -> bool | None:
    """The model's "found" answer: True, False, or None for "dont_know" (and anything unreadable)."""
    if raw == "yes" or raw is True:
        return True
    if raw == "no" or raw is False:
        return False
    return None
