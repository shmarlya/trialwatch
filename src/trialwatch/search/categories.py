"""Plain-language categories (categories.json): each a set of MeSH tree branches, as CTIS files a
trial's therapeutic areas, with the words people type for it in eight languages."""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from trialwatch.search.constants import CATEGORY_TOKEN
from trialwatch.sources import profile

CATEGORIES_FILE = Path(__file__).with_name("categories.json")


@dataclass(frozen=True)
class Category:
    slug: str
    name: str
    branches: tuple[str, ...]
    words: tuple[str, ...]


@cache
def all_categories() -> tuple[Category, ...]:
    with CATEGORIES_FILE.open(encoding="utf-8") as file:
        data = json.load(file)
    found = []
    for item in data["categories"]:
        words = []
        for word in item["words"]:
            words.append(word.lower())
        found.append(Category(item["slug"], item["name"], tuple(item["branches"]), tuple(words)))
    return tuple(found)


def by_slug(slug: str) -> Category | None:
    for category in all_categories():
        if category.slug == slug:
            return category
    return None


def token(slug: str) -> str:
    return CATEGORY_TOKEN + slug


def for_areas(areas: tuple[profile.Area, ...]) -> list[str]:
    """The slugs of the categories the trial's therapeutic areas are in."""
    found = []
    for area in areas:
        if area.code is None:
            continue
        for category in all_categories():
            if area.code in category.branches and category.slug not in found:
                found.append(category.slug)
    return found


def for_word(word: str) -> list[str]:
    """The slugs of the categories a query word names ("krebs" is cancer)."""
    found = []
    lowered = word.lower()
    for category in all_categories():
        if lowered in category.words:
            found.append(category.slug)
    return found
