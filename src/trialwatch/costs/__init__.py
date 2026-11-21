"""What a paid API call cost, in US dollars.

The prices are one current list (prices.json, Anthropic's list prices per million tokens). A call is
priced per billed attempt: when a model declined and a fallback model answered (server-side fallback),
the response's usage lists each attempt (usage.iterations) with the model that ran it, and each is
priced at that model's rate. A model missing from the list is left unpriced (cost None), never $0, so
a total never quietly leaves it out: pages show how many calls are unpriced.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

PRICES_FILE = Path(__file__).parent / "prices.json"
MILLION = Decimal(1_000_000)
# The token kinds a call is billed for, as prices.json names their prices
TOKEN_KINDS = ("input", "output", "cache_write", "cache_read")

_prices: dict | None = None


def price_list() -> dict:
    """prices.json's models: model id -> its prices (as decimal strings), read once."""
    global _prices
    if _prices is None:
        _prices = json.loads(PRICES_FILE.read_text())["models"]
    return _prices


@dataclass(frozen=True)
class CallCost:
    """One billed attempt of a call: the model that ran it, its tokens and what they cost."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    # None: the model is not in the price list
    cost_usd: Decimal | None = None
    # the prices it was computed with, US dollars per million tokens
    prices: dict | None = None

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None


def prices_for(model: str, prompt_tokens: int) -> dict | None:
    """The four prices of `model` for a request with this many prompt tokens, or None when the model
    is not listed. Some models cost more above a prompt length (prices.json long_prompt)."""
    listed = price_list().get(model)
    if listed is None:
        return None
    chosen = listed
    long_prompt = listed.get("long_prompt")
    if long_prompt is not None and prompt_tokens > long_prompt["over_prompt_tokens"]:
        chosen = long_prompt
    prices = {}
    for kind in TOKEN_KINDS:
        prices[kind] = chosen[kind]
    return prices


def price_tokens(model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0,
                 cache_read_tokens: int = 0) -> CallCost:
    prompt_tokens = input_tokens + cache_write_tokens + cache_read_tokens
    prices = prices_for(model, prompt_tokens)
    if prices is None:
        return CallCost(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
    counts = {"input": input_tokens, "output": output_tokens, "cache_write": cache_write_tokens,
              "cache_read": cache_read_tokens}
    cost = Decimal(0)
    for kind in TOKEN_KINDS:
        cost += Decimal(counts[kind]) * Decimal(prices[kind]) / MILLION
    return CallCost(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost, prices)


def _count(usage: Any, name: str) -> int:
    """A token count from a usage object; 0 when the API did not report it."""
    count = getattr(usage, name, 0)
    if not count:
        return 0
    return count


def price_usage(usage: Any, model: str) -> list[CallCost]:
    """The billed attempts of one response, priced. `model` is the model the response says answered;
    an attempt that names no model of its own ran on it."""
    if usage is None:
        return []
    attempts = getattr(usage, "iterations", None)
    if not attempts:
        attempts = [usage]
    costs = []
    for attempt in attempts:
        attempt_model = getattr(attempt, "model", None)
        if not attempt_model:
            attempt_model = model
        costs.append(price_tokens(attempt_model, _count(attempt, "input_tokens"), _count(attempt, "output_tokens"),
                                  _count(attempt, "cache_creation_input_tokens"),
                                  _count(attempt, "cache_read_input_tokens")))
    return costs


def total_cost(costs: list[CallCost]) -> Decimal:
    """What the priced attempts cost together (unpriced ones count as nothing; see unpriced_count)."""
    total = Decimal(0)
    for cost in costs:
        if cost.cost_usd is not None:
            total += cost.cost_usd
    return total


def unpriced_count(costs: list[CallCost]) -> int:
    count = 0
    for cost in costs:
        if cost.cost_usd is None:
            count += 1
    return count


def dollars(amount: Decimal | float | None) -> str:
    """An amount as pages show it: $1.23, or $0.0042 below a cent; "—" when unknown."""
    if amount is None:
        return "—"
    value = Decimal(amount)
    if value != 0 and abs(value) < Decimal("0.01"):
        return f"${value:.4f}"
    return f"${value:,.2f}"
