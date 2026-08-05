# Architecture decision records

Most of the reasoning in this repository lives next to the code it explains — the wire
protocol document, the module docstrings, the comments on the awkward branches. That works
because most decisions are local: one function, one model, one file.

A few are not. When a decision sets a **boundary** — what belongs to Fenix Spoon and what
belongs to the applications built on it, what goes on the wire and what does not — it has no
single file to live in, and the alternatives it rejected are the most valuable part of it.
Those get a record here.

An ADR is written once and then left alone. If a later change reverses one, the new record
supersedes it and says so; the old one stays, because a decision log that only records the
current answer cannot tell you why the previous one stopped being right.

| | Record | Status |
|---|---|---|
| [0001](0001-explorable-viewer.md) | An explorable viewer without a protocol change | Accepted |
| [0002](0002-workspace-over-http.md) | The workspace over HTTP | Accepted |
| [0003](0003-axisymmetric-axis-label.md) | The axis label belongs to the kind, not to a field | Accepted |
| [0004](0004-a-mode-is-not-an-instant.md) | A mode is not an instant | Accepted |
