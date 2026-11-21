"""What every model call of a check shares: the system prompts, the client, the fallback rule.

Prompts are files in src/trialwatch/checks/prompts/, one current text each (ADR-004: nothing is
versioned before production); a run stores the exact text it sent and its sha256.
"""

import hashlib
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol

# On a safety refusal the API can re-run the request on the model Anthropic recommends
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    sha256: bytes


def prompt_names() -> list[str]:
    """Every prompt file in src/trialwatch/checks/prompts/, by name without .md, sorted."""
    names = []
    for prompt_file in files("trialwatch.checks.prompts").iterdir():
        if prompt_file.name.endswith(".md"):
            names.append(prompt_file.name.removesuffix(".md"))
    return sorted(names)


def load_prompt(name: str) -> Prompt:
    """A prompt file's text and the sha256 that ties each stored run to it; FileNotFoundError if none.
    Only the files of checks/prompts/ are prompts: a name is never a path (it may come from a form)."""
    if name not in prompt_names():
        raise FileNotFoundError(f"no prompt {name!r} in checks/prompts/")
    prompt_file = files("trialwatch.checks.prompts").joinpath(f"{name}.md")
    text = prompt_file.read_text()
    text_hash = hashlib.sha256(text.encode()).digest()
    return Prompt(name, text, text_hash)


def uses_fallbacks(model: str, fallbacks: bool) -> bool:
    """Whether a request asks for the server-side fallback: Haiku models have none."""
    if not fallbacks:
        return False
    return not model.startswith("claude-haiku")


def first_text(content: list[Any], default: str | None) -> str | None:
    """The text of the first text block in a response's content, or `default` if there is none.

    The response may also hold thinking blocks, which carry no answer.
    """
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            return block.text
    return default


class MessagesClient(Protocol):
    """The slice of anthropic.Anthropic we use, so tests can pass a fake."""

    @property
    def beta(self) -> Any: ...
