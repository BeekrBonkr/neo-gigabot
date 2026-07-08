from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DB_FILENAME = "settings.db"
BACKUP_DIR_NAME = "backups"
BACKUP_FILENAME_FORMAT = "settings-%Y%m%d-%H%M%S.db"
DEFAULT_BACKUPS_KEPT = 14

# Generous busy timeout so concurrent writers (e.g. a burst of playlist edits)
# wait for each other instead of immediately raising "database is locked".
BUSY_TIMEOUT_SECONDS = 10.0


def db_path(storage_path: Path) -> Path:
    return storage_path / DB_FILENAME


def connect(storage_path: Path) -> sqlite3.Connection:
    path = db_path(storage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    # WAL keeps reads from ever blocking on a held write lock (and vice versa).
    # All bot access is serial on the event loop, so the only lock contention
    # comes from another process touching the live DB (sqlite3 CLI, backups) -
    # under the default rollback journal that could stall the loop, and thus
    # the whole bot, for the full busy timeout. NORMAL synchronous is the
    # standard WAL pairing (a power cut can drop the last commits, never
    # corrupt). If in-process writes ever move to worker threads, this also
    # keeps them from blocking event-loop reads.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def backup_database(storage_path: Path, *, keep: int = DEFAULT_BACKUPS_KEPT) -> Path:
    """Snapshot settings.db into storage/backups/ using SQLite's online backup
    API (safe while the bot is running - it takes a consistent copy even with
    concurrent writes, unlike cp). Prunes to the newest `keep` snapshots and
    returns the new snapshot's path."""
    backups_dir = storage_path / BACKUP_DIR_NAME
    backups_dir.mkdir(parents=True, exist_ok=True)
    dest_path = backups_dir / datetime.now(timezone.utc).strftime(BACKUP_FILENAME_FORMAT)

    source = connect(storage_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    os.chmod(dest_path, 0o600)  # same posture as the live DB

    if keep > 0:
        snapshots = sorted(backups_dir.glob("settings-*.db"))
        for old in snapshots[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass

    return dest_path


@contextmanager
def write_transaction(storage_path: Path) -> Iterator[sqlite3.Connection]:
    """Serialize a read-modify-write against the DB with an immediate write lock.

    Using BEGIN IMMEDIATE means the write lock is taken up front (rather than
    on the first write statement), so two concurrent callers doing
    read-then-mutate-then-save can't silently clobber each other's changes.
    """
    connection = connect(storage_path)
    connection.isolation_level = None  # autocommit mode; we drive transactions manually
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
