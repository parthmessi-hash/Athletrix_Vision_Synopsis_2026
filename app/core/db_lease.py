"""Cooperative coordination between a running engine and offline maintenance tools on one database file.

A lease file next to the database (`<database>.athletrix-lock`, empty) is locked with the operating system's advisory
`flock`:
  - every engine `Database` holds a SHARED lock on it for as long as the database is open;
  - maintenance tools that write (scripts/local_data.py removal, scripts/recording_package.py import) take an
    EXCLUSIVE lock first and refuse to run if any engine or other tool holds the lease; while they hold it, an engine
    refuses to open the database (fail closed).
The operating system releases the lock when the process exits, even after a crash, so a stale lease cannot block.

Scope, honestly: this is cooperative. It coordinates Athletrix processes that use this code; it does not stop an engine
from before this lease existed, an sqlite3 shell or another program, and advisory locks are unreliable on network file
systems. The removal tool therefore also requires SQLite's own exclusive lock, which a running engine (which always
reads the file on open) blocks; see app/store/removal.py.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

SUFFIX = ".athletrix-lock"


class LeaseUnavailable(Exception):
    """The lease is held in a conflicting mode by another engine or maintenance tool."""


def lease_path(database: str | Path) -> Path:
    return Path(str(Path(database).expanduser()) + SUFFIX)


class Lease:
    def __init__(self, database: str | Path, *, exclusive: bool) -> None:
        self.path = lease_path(database)
        self.exclusive = exclusive
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            if exclusive:
                raise LeaseUnavailable(f"{database} is in use: an Athletrix engine or another maintenance tool holds its lease ({self.path.name}). "
                                       "Stop the engine that uses this file and try again; nothing was changed.") from exc
            raise LeaseUnavailable(f"{database} is locked by a maintenance tool (removal or import) holding {self.path.name}; "
                                   "the engine will not open it until that tool finishes.") from exc

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
