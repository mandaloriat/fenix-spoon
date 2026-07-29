"""Durable job storage (roadmap M3, issue #13).

The job manager keeps live jobs in memory — they own asyncio queues and a threading
event, which nothing can serialize. This module owns everything that must outlive the
process: job metadata, the progress-event log, and the result payload.

Two backends implement :class:`JobStore`:

- :class:`MemoryJobStore` — no persistence, the dev default; behaves exactly like the
  dict the manager used before there was a store at all.
- :class:`SqliteJobStore` — one file under the data directory, WAL enabled. Metadata and
  events go in the database; **result payloads go on disk** as ``result.json`` next to
  that job's artifacts. A 512x341 grid is several megabytes of JSON, and the data
  directory is already the durable-storage contract for artifacts — putting the payload
  there keeps the database small enough to stay fast, and makes a job's bytes one
  directory you can copy, delete or mount.

Postgres and S3 are the two backends production will eventually want. They are not here:
what makes them possible is this interface, and adding an untested backend for a database
nobody has run against would be pretend infrastructure. A Postgres store implements the
same six methods; an S3 artifact backend belongs behind ``SolverContext.artifact``.
"""

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .solvers.base import SolverResult


@dataclass
class JobRecord:
    """The persistable half of a job — everything except its live runtime state."""

    id: str
    solver: str
    status: str
    owner: str = "anonymous"
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    result: SolverResult | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def artifact_bytes(self) -> int:
        return sum(int(entry.get("size", 0)) for entry in self.artifacts)


class JobStore(ABC):
    """Where jobs live between requests, and between process lifetimes."""

    @abstractmethod
    def put(self, record: JobRecord) -> None:
        """Insert or update a job's metadata, result and artifact list (not its events)."""

    @abstractmethod
    def add_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Append one event to a job's log and return its sequence number.

        The seq is what lets a subscriber reconcile replayed history with a live feed:
        attach to the feed, read the history, drop anything live already seen. Counting
        instead of comparing breaks as soon as the two sources interleave, which across
        a process boundary they will.
        """

    @abstractmethod
    def get(self, job_id: str) -> JobRecord | None:
        """Load a job with its full event log, or None if it is unknown or purged."""

    @abstractmethod
    def list_jobs(
        self, limit: int = 50, offset: int = 0, owner: str | None = None
    ) -> list[JobRecord]:
        """Newest first, without event logs — this is for a history page, not replay.

        ``owner`` restricts the page to one principal; ``None`` spans every principal.
        """

    @abstractmethod
    def count(self, owner: str | None = None) -> int:
        """Total stored jobs, for pagination. Scoped to ``owner`` when given."""

    @abstractmethod
    def purge_before(self, cutoff: datetime) -> list[str]:
        """Delete jobs created before ``cutoff``; return the ids removed so the caller
        can clean up their artifact directories."""

    @abstractmethod
    def usage(self, owner: str, since: datetime) -> tuple[int, int, int]:
        """``(active jobs, jobs created since, artifact bytes)`` for one principal.

        One method rather than three because quota enforcement needs all of it on every
        submit, and a backend with a real query planner should answer it in one round
        trip rather than three.
        """

    def unfinished_ids(self) -> list[str]:
        """Jobs left ``queued``/``running`` — after a restart, nothing is solving them."""
        return [r.id for r in self.list_jobs(limit=10_000) if r.status in ("queued", "running")]

    def close(self) -> None:
        """Release whatever the backend holds. Nothing to do for in-memory stores."""
        return None


class MemoryJobStore(JobStore):
    """Dev default: keeps records in a dict, loses them with the process."""

    def __init__(self) -> None:
        self._records: dict[str, JobRecord] = {}

    def put(self, record: JobRecord) -> None:
        existing = self._records.get(record.id)
        # Keep the event log across metadata updates; put() is not about events.
        events = existing.events if existing is not None else record.events
        self._records[record.id] = JobRecord(
            id=record.id,
            solver=record.solver,
            status=record.status,
            owner=record.owner,
            error=record.error,
            created_at=record.created_at,
            finished_at=record.finished_at,
            result=record.result,
            artifacts=list(record.artifacts),
            events=events,
        )

    def add_event(self, job_id: str, event: dict[str, Any]) -> int:
        record = self._records.get(job_id)
        if record is None:
            return 0
        record.events.append(event)
        return len(record.events)

    def get(self, job_id: str) -> JobRecord | None:
        return self._records.get(job_id)

    def list_jobs(
        self, limit: int = 50, offset: int = 0, owner: str | None = None
    ) -> list[JobRecord]:
        matching = [r for r in self._records.values() if owner is None or r.owner == owner]
        ordered = sorted(matching, key=lambda r: r.created_at, reverse=True)
        return ordered[offset : offset + limit]

    def count(self, owner: str | None = None) -> int:
        if owner is None:
            return len(self._records)
        return sum(1 for r in self._records.values() if r.owner == owner)

    def purge_before(self, cutoff: datetime) -> list[str]:
        doomed = [r.id for r in self._records.values() if r.created_at < cutoff]
        for job_id in doomed:
            del self._records[job_id]
        return doomed

    def usage(self, owner: str, since: datetime) -> tuple[int, int, int]:
        mine = [r for r in self._records.values() if r.owner == owner]
        active = sum(1 for r in mine if r.status in ("queued", "running"))
        recent = sum(1 for r in mine if r.created_at >= since)
        return active, recent, sum(r.artifact_bytes for r in mine)


# Opening a database runs these three in order: tables, then column migrations, then
# indexes. The order is load-bearing — an index over a column that a migration is about
# to add cannot be created before the migration runs.
_TABLES = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    solver         TEXT NOT NULL,
    status         TEXT NOT NULL,
    error          TEXT,
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    result_kind    TEXT,
    stats          TEXT,
    artifacts      TEXT NOT NULL DEFAULT '[]',
    owner          TEXT NOT NULL DEFAULT 'anonymous',
    artifact_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    job_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
"""

# Columns added after a release. ``CREATE TABLE IF NOT EXISTS`` does nothing to a table
# that already exists, so a database written by an older server needs them added
# explicitly — otherwise the first query after an upgrade is an OperationalError.
_MIGRATIONS = {
    "owner": "ALTER TABLE jobs ADD COLUMN owner TEXT NOT NULL DEFAULT 'anonymous'",
    "artifact_bytes": "ALTER TABLE jobs ADD COLUMN artifact_bytes INTEGER NOT NULL DEFAULT 0",
}

_INDEXES = """
CREATE INDEX IF NOT EXISTS jobs_created_at ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_owner ON jobs (owner, created_at DESC);
"""


class SqliteJobStore(JobStore):
    """SQLite-backed store. One connection, one lock, WAL journaling.

    A single guarded connection rather than one per call: writes here are frequent
    (every progress event) and short, and the lock keeps the connection usable from the
    event loop and from a worker thread hop without ``check_same_thread`` surprises.
    """

    def __init__(self, path: Path, result_dir: Path | None = None) -> None:
        self.path = path
        # Where result.json lives, per job: <result_dir>/<job_id>/result.json. Defaults
        # to the database's own directory, which is also where artifacts go.
        self._result_dir = result_dir if result_dir is not None else path.parent
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_TABLES)
            present = {row["name"] for row in self._db.execute("PRAGMA table_info(jobs)")}
            for column, statement in _MIGRATIONS.items():
                if column not in present:
                    self._db.execute(statement)
            self._db.executescript(_INDEXES)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _result_path(self, job_id: str) -> Path:
        return self._result_dir / job_id / "result.json"

    def put(self, record: JobRecord) -> None:
        if record.result is not None:
            path = self._result_path(record.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record.result.data))
        with self._lock:
            self._db.execute(
                """INSERT INTO jobs (id, solver, status, error, created_at, finished_at,
                                     result_kind, stats, artifacts, owner, artifact_bytes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       status=excluded.status, error=excluded.error,
                       finished_at=excluded.finished_at, result_kind=excluded.result_kind,
                       stats=excluded.stats, artifacts=excluded.artifacts,
                       artifact_bytes=excluded.artifact_bytes""",
                (
                    record.id,
                    record.solver,
                    record.status,
                    record.error,
                    record.created_at.isoformat(),
                    record.finished_at.isoformat() if record.finished_at else None,
                    record.result.kind if record.result else None,
                    json.dumps(record.result.stats) if record.result else None,
                    json.dumps(record.artifacts),
                    record.owner,
                    record.artifact_bytes,
                ),
            )
            self._db.commit()

    def add_event(self, job_id: str, event: dict[str, Any]) -> int:
        with self._lock:
            cursor = self._db.execute(
                """INSERT INTO events (job_id, seq, payload) VALUES (
                       ?,
                       (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE job_id = ?),
                       ?
                   )
                   RETURNING seq""",
                (job_id, job_id, json.dumps(event)),
            )
            seq = int(cursor.fetchone()[0])
            self._db.commit()
        return seq

    def _record(self, row: sqlite3.Row, events: list[dict[str, Any]]) -> JobRecord:
        result = None
        if row["result_kind"]:
            path = self._result_path(row["id"])
            # Metadata without its payload means the file was removed underneath us
            # (manual cleanup, a half-deleted job). Report the job, not a crash.
            if path.is_file():
                result = SolverResult(
                    kind=row["result_kind"],
                    data=json.loads(path.read_text()),
                    stats=json.loads(row["stats"] or "{}"),
                )
        return JobRecord(
            id=row["id"],
            solver=row["solver"],
            status=row["status"],
            owner=row["owner"],
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            finished_at=(
                datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
            ),
            result=result,
            artifacts=json.loads(row["artifacts"]),
            events=events,
        )

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            events = [
                json.loads(r["payload"])
                for r in self._db.execute(
                    "SELECT payload FROM events WHERE job_id = ? ORDER BY seq", (job_id,)
                )
            ]
        return self._record(row, events)

    def list_jobs(
        self, limit: int = 50, offset: int = 0, owner: str | None = None
    ) -> list[JobRecord]:
        where, params = ("WHERE owner = ?", [owner]) if owner is not None else ("", [])
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [self._record(row, []) for row in rows]

    def count(self, owner: str | None = None) -> int:
        where, params = ("WHERE owner = ?", [owner]) if owner is not None else ("", [])
        with self._lock:
            row = self._db.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()
        return int(row[0])

    def purge_before(self, cutoff: datetime) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE created_at < ?", (cutoff.isoformat(),)
            ).fetchall()
            doomed = [row["id"] for row in rows]
            if doomed:
                marks = ",".join("?" * len(doomed))
                self._db.execute(f"DELETE FROM events WHERE job_id IN ({marks})", doomed)
                self._db.execute(f"DELETE FROM jobs WHERE id IN ({marks})", doomed)
                self._db.commit()
        return doomed

    def usage(self, owner: str, since: datetime) -> tuple[int, int, int]:
        with self._lock:
            row = self._db.execute(
                """SELECT
                       COALESCE(SUM(status IN ('queued', 'running')), 0) AS active,
                       COALESCE(SUM(created_at >= ?), 0)                AS recent,
                       COALESCE(SUM(artifact_bytes), 0)                 AS bytes
                   FROM jobs WHERE owner = ?""",
                (since.isoformat(), owner),
            ).fetchone()
        return int(row["active"]), int(row["recent"]), int(row["bytes"])

    def unfinished_ids(self) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
        return [row["id"] for row in rows]
