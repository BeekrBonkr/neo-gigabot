from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_FILENAME = "settings.db"

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
    return connection


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
