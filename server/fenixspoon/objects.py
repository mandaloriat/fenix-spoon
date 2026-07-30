"""Workspace object storage: JSON files under the data directory (roadmap M2.5, issue #44).

Jobs, results and artifacts already survive the process — :mod:`fenixspoon.store` owns them.
What had nowhere to live is the *input* an engineer works on: the geometry, the material, the
design. This module is where those go.

## Why files rather than rows

Issue #44 left the storage open — new tables in ``jobs.db``, a second database, or manifest
files — and named the criterion in the
[design draft](../../../docs/07-local-agent-interface.md#15-open-questions): *decide by whether
a workspace is meant to be committed to a repository*.

It is. The draft's own use cases include "a repository of designs is re-solved on every solver
change", and §8 asks for a workspace a human "can look at, diff, and put in version control if
they choose". A SQLite file satisfies none of that: `git diff` on it says `Binary files differ`,
and reviewing a geometry change means writing a tool. One JSON file per revision means a design
edit shows up in a pull request as the two coordinates that moved.

The constraint the issue *did* fix still holds: **not a second store**. These files live inside
the same ``FENIXSPOON_DATA_DIR`` as ``jobs.db`` and the artifacts, so there is still one
directory to mount, back up or delete. What differs is retention, deliberately — see below.

## Why one implementation and no interface

:class:`~fenixspoon.store.JobStore` has two backends because a deployment genuinely chooses
between them: `memory` means "history dies with the process". There is no equivalent choice
here. An in-memory object store would exist only to mirror an interface, and the tests that
would use it are as happy with ``tmp_path``. If a hosted deployment ever needs objects in S3,
that is the moment to extract an interface — with a second implementation to shape it.

One consequence worth stating: with ``FENIXSPOON_STORE=memory``, jobs stay in memory but
objects still land on disk. That is the intended asymmetry rather than an oversight. A job is a
computation, and losing it costs a re-run; an object is something a person authored.

## Immutability and allocation

A revision file is written with ``O_EXCL`` and never rewritten, so "objects are versioned rather
than mutated in place" is enforced by the filesystem instead of by convention. Ids are allocated
by trying to ``mkdir`` the next free directory: ``mkdir`` is atomic on every filesystem worth
supporting, so two concurrent creates cannot both win ``g-3`` — the loser sees
``FileExistsError`` and takes ``g-4``. That is the whole locking protocol, and it is enough
because the alternative (a counter file) can drift from what is actually on disk.
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Object type → the short prefix its ids carry. The prefixes are the ones the design draft
#: uses (`geometry:g-42`, `design:d-18`, `study:s-9`); the two thin types get the obvious
#: initials. Readable rather than UUID: an agent that has to carry `design:d-18` through a
#: conversation pays two tokens for it, and a person debugging can type it.
PREFIXES: dict[str, str] = {
    "geometry": "g",
    "material": "m",
    "boundary_condition": "bc",
    "load_case": "lc",
    "design": "d",
    "study": "s",
}

OBJECT_TYPES: tuple[str, ...] = tuple(PREFIXES)

#: `geometry:g-12`, optionally pinned to a revision as `geometry:g-12@3`.
_REF = re.compile(r"^(?P<type>[a-z_]+):(?P<prefix>[a-z]+)-(?P<number>\d+)$")

#: Zero-padded so a plain lexical sort of the directory is a numeric sort of the revisions,
#: and so `ls` reads in order. Five digits is 99,999 revisions of one object; a design edited
#: once a minute for two months would not reach it, and the format degrades gracefully anyway
#: because the sort key is parsed back to an int.
_REVISION_DIGITS = 5


@dataclass(frozen=True)
class ObjectRecord:
    """One revision of one object. Immutable, and the unit that is written and read."""

    ref: str
    type: str
    revision: int
    owner: str
    body: dict[str, Any]
    label: str | None
    created_at: datetime

    @property
    def pinned(self) -> str:
        """This exact revision, as `geometry:g-1@3` — what a result names as its input."""
        return f"{self.ref}@{self.revision}"

    def to_json(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "type": self.type,
            "revision": self.revision,
            "owner": self.owner,
            "label": self.label,
            "created_at": self.created_at.isoformat(),
            "body": self.body,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "ObjectRecord":
        return cls(
            ref=raw["ref"],
            type=raw["type"],
            revision=int(raw["revision"]),
            owner=raw.get("owner", "anonymous"),
            body=raw.get("body", {}),
            label=raw.get("label"),
            created_at=datetime.fromisoformat(raw["created_at"]),
        )


def parse_ref(ref: str) -> tuple[str, str, int | None]:
    """``"geometry:g-1@3"`` → ``("geometry:g-1", "geometry", 3)``.

    Raises :class:`ValueError` on anything else, including a type that exists with the wrong
    prefix (`geometry:d-1`). Checking the prefix against the type is not pedantry: the two
    encode the same fact, so a mismatch means the caller built the string from parts and got
    one of them wrong — much better caught here than by a lookup that quietly finds nothing.
    """
    base, _, revision_text = ref.partition("@")
    match = _REF.match(base)
    if match is None:
        raise ValueError(f"not an object reference: {ref!r} (expected e.g. 'geometry:g-1')")
    object_type, prefix = match["type"], match["prefix"]
    if object_type not in PREFIXES:
        raise ValueError(f"unknown object type {object_type!r} in {ref!r}")
    if prefix != PREFIXES[object_type]:
        raise ValueError(
            f"{ref!r} mixes type {object_type!r} with prefix {prefix!r}: "
            f"a {object_type} id starts with {PREFIXES[object_type]!r}"
        )
    if not revision_text:
        return base, object_type, None
    if not revision_text.isdigit():
        raise ValueError(f"revision must be a positive integer, got {revision_text!r} in {ref!r}")
    revision = int(revision_text)
    if revision < 1:
        raise ValueError(f"revisions start at 1, got {revision} in {ref!r}")
    return base, object_type, revision


class ObjectFileStore:
    """Revisioned JSON objects under ``<root>/objects/<type>/<id>/<revision>.json``."""

    def __init__(self, root: Path) -> None:
        #: Created lazily. A deployment that never touches the workspace never grows the
        #: directory, which keeps `environment.inspect`'s data-directory listing honest.
        self.root = root / "objects"

    # ------------------------------------------------------------------ paths

    def _dir(self, ref: str) -> Path:
        base, object_type, _ = parse_ref(ref)
        return self.root / object_type / base.split(":", 1)[1]

    @staticmethod
    def _revision_file(directory: Path, revision: int) -> Path:
        return directory / f"{revision:0{_REVISION_DIGITS}d}.json"

    # ----------------------------------------------------------------- writes

    def create(
        self, object_type: str, owner: str, body: dict[str, Any], label: str | None = None
    ) -> ObjectRecord:
        """Allocate the next free id of this type and write revision 1."""
        if object_type not in PREFIXES:
            raise ValueError(f"unknown object type: {object_type!r}")
        prefix = PREFIXES[object_type]
        parent = self.root / object_type
        parent.mkdir(parents=True, exist_ok=True)

        number = self._next_number(parent, prefix)
        while True:
            directory = parent / f"{prefix}-{number}"
            try:
                directory.mkdir()
                break
            except FileExistsError:
                # Somebody else took this number between the scan and the mkdir. That is
                # the allocation protocol working, not an error: take the next one.
                number += 1

        return self._write(
            directory,
            ObjectRecord(
                ref=f"{object_type}:{prefix}-{number}",
                type=object_type,
                revision=1,
                owner=owner,
                body=body,
                label=label,
                created_at=datetime.now(UTC),
            ),
        )

    @staticmethod
    def _next_number(parent: Path, prefix: str) -> int:
        highest = 0
        for child in parent.iterdir():
            name = child.name
            if child.is_dir() and name.startswith(f"{prefix}-"):
                tail = name[len(prefix) + 1 :]
                if tail.isdigit():
                    highest = max(highest, int(tail))
        return highest + 1

    def revise(self, record: ObjectRecord) -> ObjectRecord:
        """Write ``record`` as a new revision of an object that already exists."""
        return self._write(self._dir(record.ref), record)

    def _write(self, directory: Path, record: ObjectRecord) -> ObjectRecord:
        path = self._revision_file(directory, record.revision)
        # O_EXCL rather than a check-then-write: it is the filesystem, not this process,
        # that guarantees a revision is written once. Two callers racing to produce
        # revision 4 means one of them read a stale head, and it must lose loudly.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as exc:
            raise FileExistsError(
                f"{record.ref} revision {record.revision} already exists"
            ) from exc
        with os.fdopen(fd, "w") as handle:
            json.dump(record.to_json(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        return record

    # ------------------------------------------------------------------ reads

    def get(self, ref: str, owner: str | None = None) -> ObjectRecord | None:
        """One revision — the pinned one if ``ref`` carries an ``@``, otherwise the head.

        ``owner`` scopes the lookup the way :meth:`JobStore.get` does: somebody else's
        object is indistinguishable from one that does not exist.
        """
        base, _, revision = parse_ref(ref)
        directory = self._dir(base)
        if revision is None:
            revision = self.head_revision(base) or 0
        path = self._revision_file(directory, revision)
        if not path.is_file():
            return None
        record = ObjectRecord.from_json(json.loads(path.read_text()))
        if owner is not None and record.owner != owner:
            return None
        return record

    def head_revision(self, ref: str) -> int | None:
        """Highest revision number written for this object, or None if there is none."""
        base, _, _ = parse_ref(ref)
        directory = self._dir(base)
        if not directory.is_dir():
            return None
        revisions = [
            int(path.stem) for path in directory.glob("*.json") if path.stem.isdigit()
        ]
        return max(revisions) if revisions else None

    def revisions(self, ref: str) -> list[int]:
        """Every revision of this object, ascending."""
        base, _, _ = parse_ref(ref)
        directory = self._dir(base)
        if not directory.is_dir():
            return []
        return sorted(int(p.stem) for p in directory.glob("*.json") if p.stem.isdigit())

    def list_heads(
        self, owner: str | None = None, object_type: str | None = None
    ) -> list[ObjectRecord]:
        """Head revisions, newest first. Whole-workspace scan, which is the right cost here.

        A workspace holds designs a person made, so it is hundreds of objects rather than
        millions — a directory walk is cheaper than the index that would have to be kept
        correct, and an index that can disagree with the files is exactly the failure a
        file layout was chosen to avoid.
        """
        types = [object_type] if object_type else list(PREFIXES)
        records = []
        for name in types:
            parent = self.root / name
            if not parent.is_dir():
                continue
            for directory in parent.iterdir():
                if not directory.is_dir():
                    continue
                ref = f"{name}:{directory.name}"
                head = self.head_revision(ref)
                if head is None:
                    continue
                record = self.get(f"{ref}@{head}", owner=owner)
                if record is not None:
                    records.append(record)
        records.sort(key=lambda r: (r.created_at, r.ref), reverse=True)
        return records
