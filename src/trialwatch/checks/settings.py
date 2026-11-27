"""How the evidence check runs, as data: a JSON file per named setup in checks/configs/, the same rule
ADR-004 set for prompts and requirement sets. A setup names the prompt and the requirement set, the
model and how hard it thinks, and how documents reach the model (their page text, searched per
question, or each PDF whole).

These are settings you choose once and keep, so runs judged with them can be compared; a person
starting a check picks a setup by name and nothing else. A lab run that tries other settings is
another file. Nothing is versioned before production: one current text per file, and every run
records the values it used (checks.run: model, effort, prompt, requirements, params with the setup's
name), so editing a file never changes what an earlier run says.
"""

import json
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from trialwatch.checks.prompts import prompt_names
from trialwatch.checks.requirements import set_names

# How documents reach the model: their page text (searched per question, asked again with every page
# when needed), or their files sent whole (the model also reads scanned pages and charts)
METHODS = ("text", "files")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# How a job sends the model's calls: in Message Batches, at half price, answered within 24 hours
# (ADR-009); or directly, at list price, answered at once. A job may ask for the other (its params' mode)
MODES = ("batch", "direct")
# The setup a check uses unless another is chosen
DEFAULT = "default"
# Every field a setup file must have, with its type
FIELDS = {
    "description": str,
    "model": str,
    "effort": str,
    "method": str,
    "prompt": str,
    "requirements": str,
    "max_tokens": int,
    "fallbacks": bool,
    "whole_pages": int,
    "top_k": int,
    "max_text_chars": int,
    "unreadable_pages_allowed": int,
    "widen_top_k": int,
    "widen_max_chars": int,
    "mode": str,
}


@dataclass(frozen=True)
class Settings:
    # the setup's name: its file in checks/configs/, without .json
    config: str
    # what it is for, in a few words (shown where a setup is chosen)
    description: str
    model: str
    effort: str
    # text | files: see METHODS
    method: str
    # the system prompt: a file in checks/prompts/
    prompt: str
    # what must be reported and how "enough" is decided: a file in checks/requirement_sets/
    requirements: str
    max_tokens: int
    # on a safety refusal the API may answer with another model (not for Haiku models)
    fallbacks: bool
    # documents with at most this many pages in total are sent whole (text method)
    whole_pages: int
    # pages per question from the full-text search, each with its neighbours (text method)
    top_k: int
    # widening sends every page only if their text fits (about 4 characters per token)
    max_text_chars: int
    # text method: a trial's answers still count as reading the whole document when at most this many of
    # its pages have too few letters to have been read (a cover or signature page), so "not found"
    # can mean "not reported"
    unreadable_pages_allowed: int
    # widening when every page is too long to send (max_text_chars): the widen_top_k best pages per
    # question, each with its neighbours, up to widen_max_chars of text in all. A result found there
    # counts; "not found" there still isn't the whole document
    widen_top_k: int
    widen_max_chars: int
    # batch | direct: see MODES
    mode: str = "batch"

    def params(self) -> dict:
        """What checks.run keeps besides its own columns (model, effort, prompt, requirements)."""
        return {"config": self.config, "method": self.method, "max_tokens": self.max_tokens,
                "fallbacks": self.fallbacks, "whole_pages": self.whole_pages, "top_k": self.top_k,
                "max_text_chars": self.max_text_chars, "unreadable_pages_allowed": self.unreadable_pages_allowed,
                "widen_top_k": self.widen_top_k, "widen_max_chars": self.widen_max_chars, "mode": self.mode}

    def summary(self) -> str:
        """The setup in one line, for forms and progress."""
        method = "page text"
        if self.method == "files":
            method = "whole PDFs"
        return (f"{self.config}: {self.model}, effort {self.effort}, {method}, prompt {self.prompt}, "
                f"requirements {self.requirements}, calls {self.mode}")


def names() -> list[str]:
    """Every setup file in checks/configs/, by name, sorted."""
    found = []
    for config_file in files("trialwatch.checks.configs").iterdir():
        if config_file.name.endswith(".json"):
            found.append(config_file.name.removesuffix(".json"))
    return sorted(found)


def _problems(name: str, data: object) -> list[str]:
    """What is wrong with a setup file's content, in words; empty when it can be used."""
    if not isinstance(data, dict):
        return ["a setup is a JSON object"]
    problems = []
    for field, kind in FIELDS.items():
        if field not in data:
            problems.append(f"{field} is missing")
        elif type(data[field]) is not kind:
            problems.append(f"{field} must be a {kind.__name__}")
    for field in data:
        if field not in FIELDS:
            problems.append(f"unknown field {field!r}")
    if problems:
        return problems
    if data["method"] not in METHODS:
        problems.append(f"method must be one of {', '.join(METHODS)}")
    if data["mode"] not in MODES:
        problems.append(f"mode must be one of {', '.join(MODES)}")
    if data["effort"] not in EFFORTS:
        problems.append(f"effort must be one of {', '.join(EFFORTS)}")
    if data["prompt"] not in prompt_names():
        problems.append(f"no prompt {data['prompt']!r} in checks/prompts/")
    if data["requirements"] not in set_names():
        problems.append(f"no requirement set {data['requirements']!r} in checks/requirement_sets/")
    for field in ("max_tokens", "top_k", "max_text_chars", "widen_top_k", "widen_max_chars"):
        if data[field] < 1:
            problems.append(f"{field} must be 1 or more")
    for field in ("whole_pages", "unreadable_pages_allowed"):
        if data[field] < 0:
            problems.append(f"{field} must be 0 or more")
    return problems


@cache
def load(name: str) -> Settings:
    """A setup by name, read once (a changed file takes effect when the process restarts);
    FileNotFoundError when there is no such file (a name is never a path: it may come from a form),
    ValueError when its content can't be used."""
    if name not in names():
        raise FileNotFoundError(f"no setup {name!r} in checks/configs/ (there are {', '.join(names())})")
    text = files("trialwatch.checks.configs").joinpath(f"{name}.json").read_text()
    data = json.loads(text)
    problems = _problems(name, data)
    if problems:
        raise ValueError(f"checks/configs/{name}.json: {'; '.join(problems)}")
    return Settings(
        config=name, description=data["description"], model=data["model"], effort=data["effort"],
        method=data["method"], prompt=data["prompt"], requirements=data["requirements"],
        max_tokens=data["max_tokens"], fallbacks=data["fallbacks"], whole_pages=data["whole_pages"],
        top_k=data["top_k"], max_text_chars=data["max_text_chars"],
        unreadable_pages_allowed=data["unreadable_pages_allowed"], widen_top_k=data["widen_top_k"],
        widen_max_chars=data["widen_max_chars"], mode=data["mode"],
    )


def load_all() -> list[Settings]:
    """Every setup, for a form to offer; one that can't be loaded is left out."""
    found = []
    for name in names():
        try:
            found.append(load(name))
        except ValueError:
            continue
    return found
