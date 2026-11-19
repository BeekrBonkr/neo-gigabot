"""Players, plants and every change to them.

Nothing here runs on a timer. A plant stores when its current growth ends and
the barrel stores when it started filling; the present state is worked out
from those times whenever someone looks.

Every change to water or sunlight is also written to `garden_ledger`, so
totals can be audited and, later, anything paid can be traced.
"""

from __future__ import annotations

import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.garden import rules

DB_FILENAME = "garden.db"

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS garden_players (
    user_id INTEGER PRIMARY KEY,
    water INTEGER NOT NULL,
    sunlight INTEGER NOT NULL,
    barrel_at REAL NOT NULL,           -- when the barrel started filling from empty
    sun_day TEXT NOT NULL DEFAULT '',  -- UTC day the daily sunlight count belongs to
    sun_today INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS garden_plants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    species TEXT NOT NULL,
    seed INTEGER NOT NULL,             -- a number unique to this plant, for looks later
    stage INTEGER NOT NULL DEFAULT 0,
    growing_until REAL,                -- NULL: waiting to be watered
    status TEXT NOT NULL DEFAULT 'pot',-- 'pot' (growing in the greenhouse) or 'bench' (grown, waiting for a garden)
    pot INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    matured_at REAL
);
CREATE INDEX IF NOT EXISTS garden_plants_owner ON garden_plants (owner_id, status);
CREATE TABLE IF NOT EXISTS garden_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_id INTEGER NOT NULL,
    currency TEXT NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    plant_id INTEGER
);
CREATE INDEX IF NOT EXISTS garden_ledger_user ON garden_ledger (user_id, id);
"""

_ready: set[str] = set()


class GardenError(ValueError):
    """Raised with a message that is safe to show to the player."""


def connect(storage_path: Path) -> sqlite3.Connection:
    path = storage_path / DB_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    if str(path) not in _ready:
        connection.executescript(CREATE_SQL)
        _ready.add(str(path))
    return connection


def clear_cache() -> None:
    """For tests, which reuse storage paths."""
    _ready.clear()


# ---------- pieces ----------


def _ledger(connection: sqlite3.Connection, now: float, user_id: int, currency: str, amount: int, reason: str, plant_id: int | None = None) -> None:
    if amount:
        connection.execute(
            "INSERT INTO garden_ledger (ts, user_id, currency, amount, reason, plant_id) VALUES (?, ?, ?, ?, ?, ?)",
            (now, user_id, currency, amount, reason, plant_id),
        )


def _barrel(player: sqlite3.Row, now: float) -> dict[str, Any]:
    barrel = rules.load()["barrel"]
    each, capacity = rules.seconds(barrel["seconds_per_water"]), int(barrel["capacity"])
    elapsed = max(0.0, now - player["barrel_at"])
    stored = min(capacity, int(elapsed // each))
    return {
        "stored": stored,
        "capacity": capacity,
        "full": stored >= capacity,
        # when the next unit of water arrives; None once the barrel is full
        "next_at": None if stored >= capacity else player["barrel_at"] + (stored + 1) * each,
    }


def _player(connection: sqlite3.Connection, user_id: int, now: float, *, create: bool) -> tuple[sqlite3.Row | None, bool]:
    row = connection.execute("SELECT * FROM garden_players WHERE user_id = ?", (user_id,)).fetchone()
    if row is not None or not create:
        return row, False
    config = rules.load()
    barrel = config["barrel"]
    # A new greenhouse starts with a full barrel, so the first visit has something to do.
    barrel_at = now - rules.seconds(barrel["seconds_per_water"]) * int(barrel["capacity"])
    start = config["starting"]
    connection.execute(
        "INSERT INTO garden_players (user_id, water, sunlight, barrel_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, int(start["water"]), int(start["sunlight"]), barrel_at, now),
    )
    _ledger(connection, now, user_id, "water", int(start["water"]), "welcome")
    _ledger(connection, now, user_id, "sunlight", int(start["sunlight"]), "welcome")
    return connection.execute("SELECT * FROM garden_players WHERE user_id = ?", (user_id,)).fetchone(), True


def _settle(connection: sqlite3.Connection, user_id: int, now: float) -> None:
    """Move every plant whose growing time has passed on to its next stage."""
    connection.execute(
        "UPDATE garden_plants SET stage = stage + 1, growing_until = NULL, "
        "matured_at = CASE WHEN stage + 1 >= ? THEN growing_until ELSE matured_at END "
        "WHERE owner_id = ? AND status = 'pot' AND growing_until IS NOT NULL AND growing_until <= ?",
        (rules.MATURE_STAGE, user_id, now),
    )


def _potted(connection: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM garden_plants WHERE owner_id = ? AND status = 'pot' ORDER BY pot LIMIT 1", (user_id,)).fetchone()


def _plant_view(plant: sqlite3.Row) -> dict[str, Any]:
    stage = int(plant["stage"])
    mature, growing = stage >= rules.MATURE_STAGE, plant["growing_until"] is not None
    return {
        "id": plant["id"],
        "species": plant["species"],
        "name": rules.species_name(plant["species"]),
        "stage": stage,
        "stage_name": rules.stage_name(stage),
        "mature": mature,
        "growing": growing,
        "ready_at": plant["growing_until"],
        "needs_water": not mature and not growing,
    }


def _state(connection: sqlite3.Connection, user_id: int, now: float, player: sqlite3.Row, new: bool) -> dict[str, Any]:
    config = rules.load()
    plant = _potted(connection, user_id)
    view = _plant_view(plant) if plant is not None else None
    barrel, cost = _barrel(player, now), config["watering"]
    pots = [
        {"index": index, "locked": index >= int(config["pots"]["usable"]), "plant": view if index == 0 else None}
        for index in range(int(config["pots"]["shown"]))
    ]
    bench = connection.execute("SELECT COUNT(*) FROM garden_plants WHERE owner_id = ? AND status = 'bench'", (user_id,)).fetchone()[0]
    enough = player["water"] >= cost["water"] and player["sunlight"] >= cost["sunlight"]
    return {
        "user_id": str(user_id),
        "new": new,
        "now": now,
        "water": player["water"],
        "sunlight": player["sunlight"],
        "sun_today": player["sun_today"] if player["sun_day"] == _day(now) else 0,
        "sun_daily_cap": int(config["sunlight"]["daily_cap"]),
        "barrel": barrel,
        "pots": pots,
        "plant": view,
        "bench": bench,
        "cost": {"water": int(cost["water"]), "sunlight": int(cost["sunlight"])},
        "starters": [{"species": s, "name": rules.species_name(s)} for s in config["starters"]] if view is None else [],
        "can": {
            "collect": barrel["stored"] > 0,
            "plant": view is None,
            "water": bool(view and view["needs_water"] and enough),
            "bench": bool(view and view["mature"]),
        },
    }


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


# ---------- what players do ----------


def is_player(storage_path: Path, user_id: int) -> bool:
    with connect(storage_path) as connection:
        return connection.execute("SELECT 1 FROM garden_players WHERE user_id = ?", (int(user_id),)).fetchone() is not None


def player_ids(storage_path: Path) -> set[int]:
    with connect(storage_path) as connection:
        return {row[0] for row in connection.execute("SELECT user_id FROM garden_players")}


def get_state(storage_path: Path, user_id: int, *, now: float | None = None) -> dict[str, Any]:
    """The player's greenhouse as it is right now. Opens one for a first-time player."""
    now, user_id = time.time() if now is None else now, int(user_id)
    with connect(storage_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        player, new = _player(connection, user_id, now, create=True)
        _settle(connection, user_id, now)
        return _state(connection, user_id, now, player, new)


def _act(storage_path: Path, user_id: int, now: float | None):
    now, user_id = time.time() if now is None else now, int(user_id)
    connection = connect(storage_path)
    connection.execute("BEGIN IMMEDIATE")
    player, _ = _player(connection, user_id, now, create=True)
    _settle(connection, user_id, now)
    return connection, player, now, user_id


def _finish(connection: sqlite3.Connection, user_id: int, now: float, did: str) -> tuple[dict[str, Any], str]:
    player = connection.execute("SELECT * FROM garden_players WHERE user_id = ?", (user_id,)).fetchone()
    state = _state(connection, user_id, now, player, False)
    connection.commit()
    connection.close()
    return state, did


def _fail(connection: sqlite3.Connection, message: str) -> GardenError:
    connection.commit()  # keeps a first-time player's new greenhouse and any settled growth
    connection.close()
    return GardenError(message)


def collect_water(storage_path: Path, user_id: int, *, now: float | None = None) -> tuple[dict[str, Any], str]:
    connection, player, now, user_id = _act(storage_path, user_id, now)
    barrel = _barrel(player, now)
    if not barrel["stored"]:
        raise _fail(connection, "The rain barrel is empty. It fills up on its own over time.")
    each = rules.seconds(rules.load()["barrel"]["seconds_per_water"])
    # A full barrel starts again from empty; otherwise the part-filled unit is kept.
    barrel_at = now if barrel["full"] else player["barrel_at"] + barrel["stored"] * each
    connection.execute("UPDATE garden_players SET water = water + ?, barrel_at = ? WHERE user_id = ?", (barrel["stored"], barrel_at, user_id))
    _ledger(connection, now, user_id, "water", barrel["stored"], "barrel")
    return _finish(connection, user_id, now, f"collected {barrel['stored']} water")


def plant_seed(storage_path: Path, user_id: int, species: str, *, now: float | None = None) -> tuple[dict[str, Any], str]:
    connection, _, now, user_id = _act(storage_path, user_id, now)
    if species not in rules.load()["starters"]:
        raise _fail(connection, "That seed is not available.")
    if _potted(connection, user_id) is not None:
        raise _fail(connection, "Your pot already has a plant in it.")
    connection.execute(
        "INSERT INTO garden_plants (owner_id, species, seed, created_at) VALUES (?, ?, ?, ?)",
        (user_id, species, random.getrandbits(31), now),
    )
    return _finish(connection, user_id, now, f"planted a {rules.species_name(species)} seed")


def water_plant(storage_path: Path, user_id: int, *, now: float | None = None) -> tuple[dict[str, Any], str]:
    connection, player, now, user_id = _act(storage_path, user_id, now)
    plant = _potted(connection, user_id)
    if plant is None:
        raise _fail(connection, "Plant a seed first.")
    if plant["stage"] >= rules.MATURE_STAGE:
        raise _fail(connection, "This plant is fully grown. Move it to the bench to make room for a new seed.")
    if plant["growing_until"] is not None:
        raise _fail(connection, "This plant is already growing. Come back when it is ready for more water.")
    config = rules.load()
    cost = config["watering"]
    if player["water"] < cost["water"]:
        raise _fail(connection, f"Watering takes {cost['water']} water. Collect some from the rain barrel.")
    if player["sunlight"] < cost["sunlight"]:
        raise _fail(connection, f"Watering takes {cost['sunlight']} sunlight. You earn sunlight by chatting in a server I'm in.")
    grow = rules.seconds(config["grow_seconds"][plant["stage"]])
    connection.execute("UPDATE garden_players SET water = water - ?, sunlight = sunlight - ? WHERE user_id = ?", (cost["water"], cost["sunlight"], user_id))
    connection.execute("UPDATE garden_plants SET growing_until = ? WHERE id = ?", (now + grow, plant["id"]))
    _ledger(connection, now, user_id, "water", -int(cost["water"]), "watering", plant["id"])
    _ledger(connection, now, user_id, "sunlight", -int(cost["sunlight"]), "watering", plant["id"])
    return _finish(connection, user_id, now, f"watered their {rules.species_name(plant['species'])}")


def move_to_bench(storage_path: Path, user_id: int, *, now: float | None = None) -> tuple[dict[str, Any], str]:
    """A grown plant leaves its pot and waits on the bench until it can be
    planted in a server's garden."""
    connection, _, now, user_id = _act(storage_path, user_id, now)
    plant = _potted(connection, user_id)
    if plant is None or plant["stage"] < rules.MATURE_STAGE:
        raise _fail(connection, "Only a fully grown plant can move to the bench.")
    connection.execute("UPDATE garden_plants SET status = 'bench' WHERE id = ?", (plant["id"],))
    return _finish(connection, user_id, now, f"moved a grown {rules.species_name(plant['species'])} to the bench")


ACTIONS = {"collect": collect_water, "water": water_plant, "bench": move_to_bench}


def act(storage_path: Path, user_id: int, action: str, *, species: str = "", now: float | None = None) -> tuple[dict[str, Any], str]:
    """One entry point for both Discord and the website."""
    if action == "plant":
        return plant_seed(storage_path, user_id, species, now=now)
    if action not in ACTIONS:
        raise GardenError("Unknown action.")
    return ACTIONS[action](storage_path, user_id, now=now)


def add_sunlight(storage_path: Path, user_id: int, *, now: float | None = None) -> int:
    """Sunlight for being active. Only people who already have a greenhouse
    earn it, up to a daily cap and a storage cap. Returns what was added."""
    now, user_id = time.time() if now is None else now, int(user_id)
    config = rules.load()["sunlight"]
    with connect(storage_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        player, _ = _player(connection, user_id, now, create=False)
        if player is None:
            return 0
        today = _day(now)
        earned = player["sun_today"] if player["sun_day"] == today else 0
        amount = min(int(config["per_message"]), int(config["daily_cap"]) - earned, int(config["storage_cap"]) - player["sunlight"])
        if amount <= 0:
            return 0
        connection.execute(
            "UPDATE garden_players SET sunlight = sunlight + ?, sun_day = ?, sun_today = ? WHERE user_id = ?",
            (amount, today, earned + amount, user_id),
        )
        _ledger(connection, now, user_id, "sunlight", amount, "activity")
        return amount
