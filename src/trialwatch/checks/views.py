"""How the trial page shows a check's trace (ADR-004): the steps' titles, each model call laid out
for reading, and the files the steps name."""

import json
from dataclasses import dataclass

from trialwatch.db import overview

# Names of the trace steps as the trial page shows them
STEP_TITLES = {
    "questions": "Questions: the requirements for this trial",
    "pages": "Documents and pages read",
    "retrieval": "Retrieval: what the model is shown",
    "call": "Model call",
    "widening": "Widening",
    "verification": "Quote verification",
    "rules": "Rules",
    "error": "Stopped",
}


def files_by_hex(steps: list[overview.StepRow]) -> dict[str, dict]:
    """File hash (hex) -> {doc, title} from the trace's pages step, to name files in other steps."""
    files = {}
    for step in steps:
        if step.name != "pages":
            continue
        for document in step.data.get("documents", []):
            files[document["file"]] = document
    return files


@dataclass
class CallView:
    """One model call of a trace, laid out for reading: what was sent and what came back."""

    number: int
    asked: list[dict]
    message: str | None
    schema: str | None
    settings: dict
    answer_text: str
    answers: list[dict]
    answer_error: str | None


def call_view(data: dict) -> CallView:
    """The call step's stored data as the trial page shows it. Runs before the request was stored
    have no message or schema."""
    request = data.get("request") or {}
    message = None
    messages = request.get("messages")
    if isinstance(messages, list) and messages:
        content = messages[0].get("content")
        if isinstance(content, str):
            message = content
        elif isinstance(content, list):
            # the files method: attached files (stored by their sha256), then the text
            pieces = []
            for block in content:
                if block.get("type") == "document":
                    source = block.get("source") or {}
                    pieces.append(
                        f"[attached file] {block.get('title')} · sha256 {str(source.get('sha256', ''))[:12]} · "
                        f"{source.get('bytes', '?')} bytes"
                    )
                elif block.get("type") == "text":
                    pieces.append(block.get("text", ""))
            message = "\n\n".join(pieces)
    schema = None
    output_config = request.get("output_config") or {}
    output_format = output_config.get("format") or {}
    if output_format.get("schema"):
        schema = json.dumps(output_format["schema"], indent=2, ensure_ascii=False)
    settings = {}
    for name in ("model", "max_tokens", "thinking", "betas", "fallbacks"):
        if name in request:
            settings[name] = request[name]
    if "effort" in output_config:
        settings["effort"] = output_config["effort"]
    raw = data.get("answer") or ""
    answer_text = raw
    answers = []
    answer_error = None
    try:
        parsed = json.loads(raw)
        answer_text = json.dumps(parsed, indent=2, ensure_ascii=False)
        for answer in parsed.get("answers", []):
            answers.append(answer)
    except (ValueError, AttributeError) as exc:
        if raw:
            answer_error = f"not valid JSON: {exc}"
    asked = data.get("asked")
    if not asked:
        asked = []
        for key in data.get("questions", []):
            asked.append({"key": key, "text": ""})
    return CallView(data.get("call", 0), asked, message, schema, settings, answer_text, answers, answer_error)
