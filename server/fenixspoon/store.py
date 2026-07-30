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
import logging
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .series import Series1DData, curves_of
from .solvers.base import SolverResult


@dataclass
class ResultSummary:
    """A finished job's answer without its field arrays (roadmap M2.5, issue #46).

    The reason this is a separate thing from :class:`~fenixspoon.solvers.base.SolverResult`
    rather than a slice of one: every field here lives in a database column, so a store can
    answer "what did it cost and what did it report" **without reading `result.json` off
    disk**. That file is megabytes. A compact level that had to load it to stay compact
    would be compact only in what it sent, which is half the saving and the less interesting
    half — the local caller shares a filesystem with the server.
    """

    kind: str
    stats: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    converged: bool | None = None
    residual: float | None = None
    warnings: list[str] = field(default_factory=list)
    series: list[Series1DData] = field(default_factory=list)
    """The result's curves (protocol 1.4, issue #69).

    Here rather than in `result.json` for the same reason the metrics are: a curve is the
    *compact* half of an answer — a few hundred numbers with axis labels — and the whole point
    of #69 was that reaching it should not cost a multi-megabyte read of the field arrays it
    happens to sit beside. :data:`~fenixspoon.series.MAX_SERIES_POINTS` is what keeps that
    trade sound; without a ceiling this column would eventually be the arrays again."""


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
    summary: ResultSummary | None = None
    """The result's scalars, available whether or not the payload was loaded (#46).

    Set by the store on read, from columns; derived from `result` on write. `None` until a
    job finishes."""

    inputs: dict[str, Any] = field(default_factory=dict)
    """Which workspace object revisions this job came from, pinned (roadmap M2.5, #44).

    Empty for a job submitted with an inline geometry, which is most of them and stays
    perfectly valid. When a design was resolved it holds the design, geometry and material
    revisions — the `design -> job -> result` relation, recorded at the one moment it is
    knowable. #47 adds the environment and the content hash beside it."""

    @property
    def artifact_bytes(self) -> int:
        return sum(int(entry.get("size", 0)) for entry in self.artifacts)

    def summarize(self) -> "ResultSummary | None":
        """This record's summary, preferring a loaded result over a stored one.

        A live in-process job holds its `SolverResult` and may not have been read back from
        the store yet, so deriving from `result` when it is there keeps the two consistent
        without a round trip.
        """
        if self.result is not None:
            return ResultSummary(
                kind=self.result.kind,
                stats=dict(self.result.stats),
                metrics=dict(self.result.metrics),
                converged=self.result.converged,
                residual=self.result.residual,
                warnings=list(self.result.warnings),
                # `curves_of`, not `result.series`: a `series1d` adapter puts its curves in
                # `data`, and this column is what the compact `series` level reads. Storing them
                # here duplicates them for that one kind — `result.json` has the same bytes — and
                # that is the price of the level costing a row read instead of a payload read.
                series=curves_of(self.result.kind, self.result.data, self.result.series),
            )
        return self.summary


def _without_series(summary: "ResultSummary | None") -> "ResultSummary | None":
    """A summary with its curves removed, for a listing that will not report them."""
    return None if summary is None else replace(summary, series=[])


class JobStore(ABC):
    """Where jobs live between requests, and between process lifetimes."""

    #: Short identifier reported by `environment.inspect` (#43), matching the value
    #: `FENIXSPOON_STORE` takes — so what an operator reads back is what they set.
    kind = "unknown"

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
    def get(
        self, job_id: str, *, with_result: bool = True, with_events: bool = True
    ) -> JobRecord | None:
        """Load one job, or None if it is unknown or purged.

        The two flags exist because the expensive parts of a record are rarely the parts
        a caller wants. A result payload is megabytes on disk and the event log is one
        row per progress tick, while most callers — a status poll, a cancel, an artifact
        download — need neither. Fetching them unconditionally made answering "is it done
        yet?" cost a 3 MB read and a JSON parse.

        Backends must return a record that is safe to mutate when either flag is False,
        rather than a live reference with fields blanked out.
        """

    @abstractmethod
    def list_jobs(
        self, limit: int = 50, offset: int = 0, owner: str | None = None
    ) -> list[JobRecord]:
        """Newest first, without event logs or result payloads — a history page.

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

    kind = "memory"

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
            inputs=dict(record.inputs),
            summary=record.summarize(),
        )

    def add_event(self, job_id: str, event: dict[str, Any]) -> int:
        record = self._records.get(job_id)
        if record is None:
            return 0
        record.events.append(event)
        return len(record.events)

    def get(
        self, job_id: str, *, with_result: bool = True, with_events: bool = True
    ) -> JobRecord | None:
        record = self._records.get(job_id)
        if record is None or (with_result and with_events):
            return record
        # Nothing is saved by omitting fields that are already in memory, but the contract
        # has to hold: a caller that asked for a partial record must not get a live
        # reference into the store, or mutating it would rewrite history in place.
        # `replace` copies the dataclass, not the lists inside it, so those go by hand —
        # otherwise appending to `record.events` still reaches the stored log.
        return replace(
            record,
            result=record.result if with_result else None,
            events=list(record.events) if with_events else [],
            artifacts=list(record.artifacts),
            inputs=dict(record.inputs),
            # Survives dropping the payload — that is the point of holding it separately.
            summary=record.summarize(),
        )

    def list_jobs(
        self, limit: int = 50, offset: int = 0, owner: str | None = None
    ) -> list[JobRecord]:
        matching = [r for r in self._records.values() if owner is None or r.owner == owner]
        ordered = sorted(matching, key=lambda r: r.created_at, reverse=True)
        # `artifacts` is copied for the same reason as in `get()`: `replace` copies the
        # dataclass, not the lists inside it, and `Job.from_record` hands this list straight
        # to a live `Job` — so a caller appending to it would rewrite the stored record.
        #
        # `series` is dropped to match the SQLite store, which does not parse the column for a
        # listing. Nothing is saved by withholding it here — the objects are already in memory —
        # but a backend-dependent answer to "does a history page carry curves?" is worse than
        # either answer, and this interface exists so the two are interchangeable.
        return [
            replace(
                r,
                result=None,
                events=[],
                artifacts=list(r.artifacts),
                inputs=dict(r.inputs),
                summary=_without_series(r.summarize()),
            )
            for r in ordered[offset : offset + limit]
        ]

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
    artifact_bytes INTEGER NOT NULL DEFAULT 0,
    inputs         TEXT NOT NULL DEFAULT '{}',
    metrics        TEXT NOT NULL DEFAULT '{}',
    diagnostics    TEXT NOT NULL DEFAULT '{}',
    series         TEXT NOT NULL DEFAULT '[]'
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
    "inputs": "ALTER TABLE jobs ADD COLUMN inputs TEXT NOT NULL DEFAULT '{}'",
    "metrics": "ALTER TABLE jobs ADD COLUMN metrics TEXT NOT NULL DEFAULT '{}'",
    # `converged`, `residual` and `warnings` share one JSON column rather than three of
    # their own. They are read together, always, and a migration per field is a cost paid
    # by every deployment for a shape that is still settling.
    "diagnostics": "ALTER TABLE jobs ADD COLUMN diagnostics TEXT NOT NULL DEFAULT '{}'",
    # A column of its own rather than a key inside `diagnostics`: curves are the answer, and
    # the separation between what a solve answered and how it went is the distinction #46
    # spent a protocol version establishing.
    "series": "ALTER TABLE jobs ADD COLUMN series TEXT NOT NULL DEFAULT '[]'",
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

    **Several processes may open the same file.** That is the worker deployment: an API
    and N workers on one mounted data directory. WAL makes concurrent readers and one
    writer safe, and ``busy_timeout`` makes a second writer wait rather than fail.
    """

    kind = "sqlite"

    #: How long a statement waits for another process's write lock before giving up.
    BUSY_TIMEOUT_MS = 10_000

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
            self._db.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
            self._enable_wal()
            self._db.executescript(_TABLES)
            present = {row["name"] for row in self._db.execute("PRAGMA table_info(jobs)")}
            for column, statement in _MIGRATIONS.items():
                if column not in present:
                    self._db.execute(statement)
            self._db.executescript(_INDEXES)
            self._db.commit()

    def _enable_wal(self) -> None:
        """Switch the file to WAL, tolerating another process doing it at the same moment.

        Changing the journal mode takes a brief exclusive lock, and SQLite does *not*
        run the busy handler for it — two processes starting together will have one of
        them see SQLITE_BUSY. Since the only thing that matters is that the file ends up
        in WAL, checking first and re-reading after a failure is enough: whoever lost
        the race finds the winner's result already in place.
        """
        for attempt in range(5):
            mode = self._db.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() == "wal":
                return
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                time.sleep(0.05 * (attempt + 1))
        # Still not WAL: usable, just with coarser locking. Say so rather than crash —
        # a single-process deployment does not care, and crashing here would take down
        # a worker for a performance property.
        logging.getLogger(__name__).warning(
            "could not enable WAL on %s; concurrent access will be slower", self.path
        )

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
                                     result_kind, stats, artifacts, owner, artifact_bytes,
                                     inputs, metrics, diagnostics, series)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       status=excluded.status, error=excluded.error,
                       finished_at=excluded.finished_at, result_kind=excluded.result_kind,
                       stats=excluded.stats, artifacts=excluded.artifacts,
                       artifact_bytes=excluded.artifact_bytes,
                       metrics=excluded.metrics, diagnostics=excluded.diagnostics,
                       series=excluded.series""",
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
                    json.dumps(record.inputs),
                    json.dumps(record.result.metrics if record.result else {}),
                    json.dumps(
                        {
                            "converged": record.result.converged,
                            "residual": record.result.residual,
                            "warnings": record.result.warnings,
                        }
                        if record.result
                        else {}
                    ),
                    json.dumps(
                        [entry.model_dump() for entry in record.result.series]
                        if record.result
                        else []
                    ),
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

    def _record(
        self,
        row: sqlite3.Row,
        events: list[dict[str, Any]],
        *,
        with_result: bool = True,
        with_series: bool = True,
    ) -> JobRecord:
        stored_diagnostics = json.loads(row["diagnostics"] or "{}")
        # `with_series` exists for the same reason `with_result` does: the expensive parts of a
        # record are rarely the parts a caller wants. Curves are the one summary field that is
        # not a handful of scalars — a surface distribution is tens of kilobytes of JSON and a
        # few hundred pydantic models — so parsing them for every row of a history page that
        # discards the result costs ~0.3 ms a row against ~2 us for the diagnostics beside it.
        stored_series = (
            [Series1DData.model_validate(entry) for entry in json.loads(row["series"] or "[]")]
            if with_series
            else []
        )
        result = None
        if row["result_kind"] and with_result:
            path = self._result_path(row["id"])
            # Metadata without its payload means the file was removed underneath us
            # (manual cleanup, a half-deleted job). Report the job, not a crash.
            if path.is_file():
                result = SolverResult(
                    kind=row["result_kind"],
                    data=json.loads(path.read_text()),
                    stats=json.loads(row["stats"] or "{}"),
                    metrics=json.loads(row["metrics"] or "{}"),
                    converged=stored_diagnostics.get("converged"),
                    residual=stored_diagnostics.get("residual"),
                    warnings=stored_diagnostics.get("warnings") or [],
                    series=stored_series,
                )
        summary = None
        if row["result_kind"]:
            # Built from columns, so it is there whether or not `with_result` loaded the
            # payload. This is what makes the `metrics` and `diagnostics` levels cost a row
            # read instead of a multi-megabyte file read and a JSON parse.
            summary = ResultSummary(
                kind=row["result_kind"],
                stats=json.loads(row["stats"] or "{}"),
                metrics=json.loads(row["metrics"] or "{}"),
                converged=stored_diagnostics.get("converged"),
                residual=stored_diagnostics.get("residual"),
                warnings=stored_diagnostics.get("warnings") or [],
                series=stored_series,
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
            # `inputs` is only ever written at insert; the UPDATE branch leaves it alone
            # because a job's provenance is fixed the moment it is accepted.
            inputs=json.loads(row["inputs"] or "{}"),
            summary=summary,
        )

    def get(
        self, job_id: str, *, with_result: bool = True, with_events: bool = True
    ) -> JobRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            events = (
                [
                    json.loads(r["payload"])
                    for r in self._db.execute(
                        "SELECT payload FROM events WHERE job_id = ? ORDER BY seq", (job_id,)
                    )
                ]
                if with_events
                else []
            )
        return self._record(row, events, with_result=with_result)

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
        # No series either: a listing is `JobStatus` per row — id, solver, status, timestamps —
        # and nothing downstream of here can reach a curve.
        return [self._record(row, [], with_result=False, with_series=False) for row in rows]

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
