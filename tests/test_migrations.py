import sqlite3

import pytest

from app.accounts import AccountBook
from app.database import Database
from app.history import HistoryDB
from app.ledger import Ledger
from app.monitor_state import MonitorState


def test_fresh_shared_sqlite_database_applies_each_component_once(tmp_path):
    path = str(tmp_path / "shared.db")
    stores = [HistoryDB(path), Ledger(path), AccountBook(path), MonitorState(path)]
    for store in stores:
        store.close()
    with sqlite3.connect(path) as connection:
        versions = connection.execute(
            "SELECT component, version FROM schema_migrations ORDER BY component, version"
        ).fetchall()
    assert versions == [
        ("accounts", 1), ("accounts", 2),
        ("history", 1), ("history", 2), ("history", 3),
        ("history", 4),
        ("history", 5),
        ("ledger", 1), ("ledger", 2), ("ledger", 3), ("ledger", 4),
        ("ledger", 5), ("ledger", 6),
        ("monitor_state", 1),
    ]


def test_migrations_are_idempotent_and_checksum_drift_aborts(tmp_path):
    path = str(tmp_path / "migration.db")
    database = Database(path, "sqlite")
    try:
        database.initialize("CREATE TABLE IF NOT EXISTS fixture(id INTEGER);",
                            component="fixture", version=1)
        database.initialize("CREATE TABLE IF NOT EXISTS fixture(id INTEGER);",
                            component="fixture", version=1)
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            database.initialize("CREATE TABLE IF NOT EXISTS changed(id INTEGER);",
                                component="fixture", version=1)
    finally:
        database.close()
