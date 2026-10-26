"""The test suite's fallback database folder: owned by this run, closed before removal, nothing else touched."""

import os
from pathlib import Path

import conftest
from app.db import Database, current_database, get_database, set_database


def test_fallback_path_lies_in_this_runs_own_folder():
    fallback = Path(os.environ["DATABASE_PATH"])
    assert fallback.parent == Path(conftest._fallback_dir.name) and fallback.parent.name.startswith("athletrix-tests-")
    assert fallback.parent.is_dir() and not fallback.exists(), "nothing has opened the fallback file"


def test_close_fallback_database_closes_only_a_database_inside_the_folder(tmp_path):
    set_database(None)
    db = get_database()                       # opens the fallback file exactly as a stray call during tests would
    try:
        assert Path(db.path).parent == Path(conftest._fallback_dir.name) and not db.closed
        conftest.close_fallback_database()
        assert db.closed, "closed before the folder is removed at exit"
    finally:
        set_database(None)
    other = Database(tmp_path / "elsewhere.db")
    set_database(other)
    try:
        conftest.close_fallback_database()
        assert not other.closed, "a database outside the fallback folder is left alone"
        assert current_database() is other
    finally:
        other.close(); set_database(None)
    set_database(None)
    conftest.close_fallback_database()        # nothing open: a no-op
