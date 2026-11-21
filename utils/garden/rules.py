"""Balance for the garden game, read from assets/garden/rules.json."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

ASSETS = Path(__file__).resolve().parents[2] / "assets" / "garden"
RULES_FILE = ASSETS / "rules.json"
MATURE_STAGE = 5


def enabled() -> bool:
    """The game is switched on per bot with GARDEN_ENABLED=1 in .env, so an
    unfinished game can sit in the code without reaching the live bot."""
    return os.getenv("GARDEN_ENABLED", "").strip() == "1"


def speed() -> float:
    """GARDEN_SPEED divides every waiting time; the beta bot runs fast."""
    try:
        return max(1.0, float(os.getenv("GARDEN_SPEED", "1")))
    except ValueError:
        return 1.0


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def seconds(base: float) -> float:
    """A waiting time from the rules file at this bot's speed."""
    return max(1.0, float(base) / speed())


def species_name(species: str) -> str:
    return load()["species"].get(species, {}).get("name", species.title())


def stage_name(stage: int) -> str:
    names = load()["stage_names"]
    return names[max(0, min(stage, len(names) - 1))]
