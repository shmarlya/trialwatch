import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts/dev_seed.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("dev_seed", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_dev_seed_matches_its_generator():
    # The seed copies what the loaders produce (rows, hashes, labels); a loader change must regenerate it
    dev_seed = load_generator()
    if not (dev_seed.EXPORT.exists() and dev_seed.FINAL.exists()):
        pytest.skip("Bruckner et al.'s files are not in data/")
    if not dev_seed.OUT.exists():  # git-ignored, so absent until someone builds it
        pytest.skip("no db/dev seed here; build it with `uv run python scripts/dev_seed.py`")
    assert dev_seed.OUT.read_text(encoding="utf-8") == dev_seed.render(), (
        "db/dev seed is stale: run `uv run python scripts/dev_seed.py`"
    )
