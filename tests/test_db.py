from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from utils.db import backup_database
from utils.settings import update_guild_settings

GUILD_ID = 424242


def test_backup_database_takes_consistent_snapshot(storage: Path) -> None:
    update_guild_settings(storage, GUILD_ID, {"warn_threshold": 3})

    snapshot = backup_database(storage)
    assert snapshot.is_file()
    assert (snapshot.stat().st_mode & 0o777) == 0o600

    # The snapshot is a standalone, readable SQLite DB with the data in it.
    connection = sqlite3.connect(snapshot)
    try:
        row = connection.execute(
            "SELECT data FROM guild_settings WHERE guild_id = ?", (GUILD_ID,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert json.loads(row[0])["warn_threshold"] == 3


def test_backup_database_prunes_old_snapshots(storage: Path) -> None:
    update_guild_settings(storage, GUILD_ID, {})
    backups_dir = storage / "backups"
    backups_dir.mkdir()
    oldest = backups_dir / "settings-20200101-000000.db"
    older = backups_dir / "settings-20200102-000000.db"
    oldest.write_bytes(b"stale")
    older.write_bytes(b"stale")

    latest = backup_database(storage, keep=2)

    remaining = sorted(p.name for p in backups_dir.glob("settings-*.db"))
    assert latest.name in remaining
    assert older.name in remaining
    assert oldest.name not in remaining
    assert len(remaining) == 2
