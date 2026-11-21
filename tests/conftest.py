from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The garden game is switched on per bot; the tests always run with it on.
os.environ["GARDEN_ENABLED"] = "1"
os.environ.pop("GARDEN_SPEED", None)

from utils.garden.store import clear_cache as clear_garden_cache
from utils.reactionroles import clear_reaction_role_cache
from utils.settings import clear_settings_caches
from utils.stickies import clear_cache as clear_sticky_cache


@pytest.fixture()
def storage(tmp_path: Path) -> Path:
    """A fresh storage directory per test, with the module-level caches reset
    so state can't leak between tests that happen to reuse a tmp path string."""
    clear_settings_caches()
    clear_reaction_role_cache()
    clear_sticky_cache()
    clear_garden_cache()
    storage_path = tmp_path / "storage"
    storage_path.mkdir()
    yield storage_path
    clear_settings_caches()
    clear_reaction_role_cache()
