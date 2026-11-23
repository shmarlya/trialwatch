"""The system prompts: one .md file each in this folder, one current text each (ADR-004: nothing is
versioned before production). A run stores the exact text it sent and its sha256."""

import hashlib
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    sha256: bytes


def prompt_names() -> list[str]:
    """Every prompt file in this folder, by name without .md, sorted."""
    names = []
    for prompt_file in files("trialwatch.checks.prompts").iterdir():
        if prompt_file.name.endswith(".md"):
            names.append(prompt_file.name.removesuffix(".md"))
    return sorted(names)


def load_prompt(name: str) -> Prompt:
    """A prompt file's text and the sha256 that ties each stored run to it; FileNotFoundError if none.
    Only the files of this folder are prompts: a name is never a path (it may come from a form)."""
    if name not in prompt_names():
        raise FileNotFoundError(f"no prompt {name!r} in checks/prompts/")
    prompt_file = files("trialwatch.checks.prompts").joinpath(f"{name}.md")
    text = prompt_file.read_text()
    text_hash = hashlib.sha256(text.encode()).digest()
    return Prompt(name, text, text_hash)
