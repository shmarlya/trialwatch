"""The gold pages (staff): each dataset's items next to our judgements, and one item with every
judgement of its trial."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from trialwatch import db
from trialwatch.checks import requirements as requirement_sets
from trialwatch.db import overview
from trialwatch.gold import datasets as gold_datasets
from trialwatch.gold.views import (
    GOLD_OURS_FILTERS,
    comparable,
    gold_rows,
    gold_summary,
    keeps,
    label_rules,
    set_aside,
    stored_identifiers,
)
from trialwatch.trials import readiness
from trialwatch.db import auth as auth_db
from trialwatch.web.deps import TEMPLATES, Requires, default_settings, running

router = APIRouter()


def _load_dataset(ref: str) -> gold_datasets.Dataset:
    try:
        return gold_datasets.load(ref)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/gold", response_class=HTMLResponse)
def gold_page(
    request: Request,
    dataset: str = "",
    q: str = "",
    label: str = "",
    reviewers: str = "",
    ours: str = "",
    user: auth_db.User = Depends(Requires("view.internal")),
) -> HTMLResponse:
    refs = gold_datasets.dataset_refs()
    context = {
        "refs": refs,
        "dataset": None,
        "rows": [],
        "summary": None,
        "q": q,
        "label_filter": label,
        "reviewers": reviewers,
        "ours": ours,
        "ours_filters": GOLD_OURS_FILTERS,
        "lock_problems": gold_datasets.lock_problems(),
        "gold_dir": gold_datasets.gold_dir(),
    }
    if not refs:
        return TEMPLATES.TemplateResponse(request, "gold.html", context)
    chosen_ref = dataset
    if chosen_ref not in refs:
        # by default the last in name order: the newest version of the last-named dataset
        chosen_ref = refs[-1]
    chosen = _load_dataset(chosen_ref)
    with db.connect_system() as conn:
        rows = gold_rows(conn, chosen)
    shown = []
    for row in rows:
        if keeps(row, q.strip(), label, reviewers, ours):
            shown.append(row)
    context["dataset"] = chosen
    context["rows"] = shown
    context["summary"] = gold_summary(rows)
    return TEMPLATES.TemplateResponse(request, "gold.html", context)


@router.get("/gold/{name}/{version}/{key}", response_class=HTMLResponse)
def gold_item_page(
    request: Request,
    name: str,
    version: str,
    key: str,
    error: str | None = None,
    user: auth_db.User = Depends(Requires("view.internal")),
) -> HTMLResponse:
    dataset = _load_dataset(f"{name}/{version}")
    item = gold_datasets.find_item(dataset, key)
    if item is None:
        raise HTTPException(404, f"no item {key} in {dataset.ref}")
    with db.connect_system() as conn:
        found = overview.trials_for_identifiers(conn, stored_identifiers(item))
        trial_id = None
        for identifier in stored_identifiers(item):
            if identifier in found:
                trial_id = found[identifier]
                break
        judgements = []
        documents = []
        ready = False
        if trial_id is not None:
            judgements = overview.trial_judgements(conn, trial_id)
            documents = overview.trial_documents(conn, trial_id)
            kinds = requirement_sets.load(dataset.requirements).document_kinds
            ready = trial_id not in readiness.not_ready(conn, [trial_id], kinds)
    compared = []
    for judgement in judgements:
        if not comparable(judgement, dataset):
            continue
        comparison = None
        if not set_aside(dataset, judgement.latest_published_at):
            comparison = gold_datasets.compare(
                item, dataset.parts, judgement.parts, judgement.label, label_rules(dataset.requirements)
            )
        compared.append((judgement, comparison))
    ct_numbers = []
    for identifier in item.identifiers:
        if identifier.scheme == "eu-ct":
            ct_numbers.append(identifier.value)
    context = {
        "dataset": dataset,
        "item": item,
        "trial_id": trial_id,
        "compared": compared,
        "documents": documents,
        "ct_numbers": ct_numbers,
        "running": running(),
        "error": error,
        "ready": ready,
        "defaults": default_settings(),
        "reviewers_disagreed": gold_datasets.reviewers_disagreed(item),
    }
    return TEMPLATES.TemplateResponse(request, "gold_item.html", context)
