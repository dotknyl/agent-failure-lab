"""The evidence bundle and the only three tools the agent gets: list, search and read.

Units and bounds (also stated in the tool descriptions the model sees):

* ``offset`` is the 1-based line number of the first line to return (default 1). Citations use the same numbers.
* ``limit`` is a number of lines: default 100, at most 200. A larger value is applied as 200 and reported.
* One read returns at most ``READ_CHAR_BUDGET`` characters of line text. When the budget stops a read early the
  header says ``bounded_by=char_budget`` and gives ``next_offset``. A read always returns at least one line.
* A single line longer than ``MAX_LINE_CHARS`` is clipped with an explicit ``[line clipped ...]`` marker.
  The fixture contains no such line; the bound exists so that nothing is ever cut silently.
* Search ANDs case-insensitive terms ("double quotes" keep a phrase together); ``in:<artifact-id>`` restricts the
  search to that artifact. It reports the total number of matches and shows at most ``SEARCH_MAX_HITS``.

Every bound that applies is reported in the result header: there is no unannounced truncation.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
READ_CHAR_BUDGET = 16_000
MAX_LINE_CHARS = 4_000
SEARCH_MAX_HITS = 40
SEARCH_LINE_CHARS = 300


class FixtureError(Exception):
    pass


@dataclass(frozen=True)
class Artifact:
    id: str
    title: str
    kind: str
    description: str
    sha256: str
    lines: tuple[str, ...]


@dataclass
class ToolOutcome:
    text: str  # exactly what the model receives as the tool message content
    ok: bool
    meta: dict = field(default_factory=dict)  # for the trace and the audit; never sent to the model


class ArtifactStore:
    def __init__(self, fixture_dir: Path):
        self.fixture_dir = Path(fixture_dir)
        manifest = json.loads((self.fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        self.fixture_id: str = manifest["fixture"]
        self.artifacts: dict[str, Artifact] = {}
        root = (self.fixture_dir / "artifacts").resolve()
        for entry in manifest["artifacts"]:
            path = (self.fixture_dir / entry["file"]).resolve()
            if path.parent != root:
                raise FixtureError(f"artifact {entry['id']} points outside the artifact directory")
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != entry["sha256"]:
                raise FixtureError(f"artifact {entry['id']} hash mismatch: manifest {entry['sha256'][:12]}, "
                                   f"file {digest[:12]} (line endings changed? see .gitattributes)")
            text = raw.decode("utf-8")
            self.artifacts[entry["id"]] = Artifact(entry["id"], entry["title"], entry["kind"],
                                                   entry["description"], digest, tuple(text.split("\n")[:-1]))
        self.bundle_sha256 = hashlib.sha256(
            "\n".join(f"{a.id}:{a.sha256}" for a in self.artifacts.values()).encode()).hexdigest()

    # ------------------------------------------------------------------ tools

    def list_artifacts(self) -> ToolOutcome:
        rows = [f"[list_artifacts] {len(self.artifacts)} artifacts. Read with read_artifact(id, offset, limit); "
                "offset is a 1-based line number."]
        for a in self.artifacts.values():
            chars = sum(len(l) + 1 for l in a.lines)
            rows.append(f"- {a.id} | {a.title} | {a.kind} | {len(a.lines)} lines, {chars} chars | {a.description}")
        return ToolOutcome("\n".join(rows), True, {"tool": "list_artifacts"})

    def read_artifact(self, id: str, offset: int = 1, limit: int = DEFAULT_LIMIT) -> ToolOutcome:
        a = self.artifacts.get(id) if isinstance(id, str) else None
        if a is None:
            return self._error("read_artifact", f"unknown artifact id {id!r}. Call list_artifacts for valid ids.")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
            return self._error("read_artifact", f"offset must be an integer line number >= 1 (got {offset!r}).")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            return self._error("read_artifact", f"limit must be an integer >= 1 (got {limit!r}).")
        total = len(a.lines)
        applied = min(limit, MAX_LIMIT)
        if offset > total:
            header = (f"[read_artifact id={a.id} | offset {offset} is past the last line ({total}) | "
                      f"returned 0 lines | bounded_by=end_of_artifact | has_more=false]")
            return ToolOutcome(header, True, {"tool": "read_artifact", "artifact": a.id, "first_line": None,
                                              "last_line": None, "bounded_by": "end_of_artifact"})
        out, used, clipped = [], 0, []
        last = offset - 1
        bounded_by = "end_of_artifact"
        for n in range(offset, min(total, offset + applied - 1) + 1):
            line = a.lines[n - 1]
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + f" [line clipped: {len(a.lines[n - 1])} chars, showing first " \
                                               f"{MAX_LINE_CHARS}]"
                clipped.append(n)
            if out and used + len(line) > READ_CHAR_BUDGET:
                bounded_by = "char_budget"
                break
            out.append(f"{n}| {line}")
            used += len(line)
            last = n
        else:
            if last < total:
                bounded_by = "limit"
        has_more = last < total
        notes = [f"read_artifact id={a.id}", f"lines {offset}-{last} of {total}", f"bounded_by={bounded_by}",
                 f"has_more={'true' if has_more else 'false'}"]
        if has_more:
            notes.append(f"next_offset={last + 1}")
        if applied != limit:
            notes.append(f"limit {limit} exceeds max {MAX_LIMIT}; applied {MAX_LIMIT}")
        if clipped:
            notes.append(f"clipped_lines={clipped}")
        text = "[" + " | ".join(notes) + "]\n" + "\n".join(out)
        return ToolOutcome(text, True, {"tool": "read_artifact", "artifact": a.id, "first_line": offset,
                                        "last_line": last, "bounded_by": bounded_by, "limit_applied": applied,
                                        "clipped_lines": clipped})

    def search_artifacts(self, query: str) -> ToolOutcome:
        if not isinstance(query, str) or not query.strip():
            return self._error("search_artifacts", "query must be a non-empty string.")
        try:
            tokens = shlex.split(query)
        except ValueError as e:
            return self._error("search_artifacts", f"could not parse query ({e}); check quotes.")
        scope = [t[3:] for t in tokens if t.lower().startswith("in:")]
        terms = [t.lower() for t in tokens if not t.lower().startswith("in:")]
        unknown = [s for s in scope if s not in self.artifacts]
        if unknown:
            return self._error("search_artifacts", f"unknown artifact id(s) in in: filter: {unknown}.")
        if not terms:
            return self._error("search_artifacts", "query has no search terms besides in: filters.")
        ids = scope or list(self.artifacts)
        hits: list[tuple[str, int, str]] = []
        per: dict[str, int] = {}
        for aid in ids:
            for n, line in enumerate(self.artifacts[aid].lines, 1):
                low = line.lower()
                if all(t in low for t in terms):
                    per[aid] = per.get(aid, 0) + 1
                    hits.append((aid, n, line))
        total = len(hits)
        shown = hits[:SEARCH_MAX_HITS]
        dist = ", ".join(f"{k}: {v}" for k, v in per.items()) or "none"
        header = (f"[search_artifacts query={query!r} | {total} matching lines ({dist}) | showing "
                  f"{len(shown)}{' (first ' + str(SEARCH_MAX_HITS) + '; refine the query or add in:<id>)' if total > SEARCH_MAX_HITS else ''}"
                  f" | lines longer than {SEARCH_LINE_CHARS} chars are clipped here; read_artifact shows them in full]")
        rows = []
        for aid, n, line in shown:
            if len(line) > SEARCH_LINE_CHARS:
                line = line[:SEARCH_LINE_CHARS] + " [...clipped]"
            rows.append(f"{aid}:L{n}| {line}")
        return ToolOutcome(header + ("\n" + "\n".join(rows) if rows else ""), True,
                           {"tool": "search_artifacts", "query": query, "total_hits": total, "shown": len(shown),
                            "hits": [[aid, n] for aid, n, _ in shown]})

    def call(self, name: str, arguments: str) -> ToolOutcome:
        """Dispatch a model tool call. Malformed calls become error results, never exceptions."""
        try:
            args = json.loads(arguments) if arguments and arguments.strip() else {}
        except json.JSONDecodeError as e:
            return self._error(name, f"arguments are not valid JSON ({e.msg}).")
        if not isinstance(args, dict):
            return self._error(name, "arguments must be a JSON object.")
        if name == "list_artifacts":
            return self.list_artifacts()
        if name == "search_artifacts":
            extra = set(args) - {"query"}
            if extra:
                return self._error(name, f"unexpected arguments {sorted(extra)}.")
            return self.search_artifacts(args.get("query", ""))
        if name == "read_artifact":
            extra = set(args) - {"id", "offset", "limit"}
            if extra:
                return self._error(name, f"unexpected arguments {sorted(extra)}.")
            return self.read_artifact(args.get("id"), args.get("offset", 1), args.get("limit", DEFAULT_LIMIT))
        return self._error(name, "unknown tool. Available: list_artifacts, search_artifacts, read_artifact.")

    def lines_text(self, artifact_id: str, first: int, last: int) -> str:
        a = self.artifacts[artifact_id]
        return "\n".join(f"{n}| {a.lines[n - 1]}" for n in range(first, last + 1))

    @staticmethod
    def _error(tool: str, message: str) -> ToolOutcome:
        return ToolOutcome(f"[error] {tool}: {message}", False, {"tool": tool, "error": message})


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_artifacts",
            "description": "List the artifacts in the incident evidence bundle with their ids, sizes and a short "
                           "description.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_artifacts",
            "description": (
                "Find lines that contain ALL query terms (case-insensitive substring match; use \"double quotes\" "
                "for a phrase). Add in:<artifact-id> to restrict the search to one or more artifacts. Returns the "
                f"total match count per artifact and at most {SEARCH_MAX_HITS} matching lines as "
                "<artifact-id>:L<line>| <text>."),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search terms, optionally with in:<id>."}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_artifact",
            "description": (
                "Read lines of one artifact. offset is the 1-based line number of the first line (default 1); "
                f"limit is a number of lines (default {DEFAULT_LIMIT}, max {MAX_LIMIT}). A read returns at most "
                f"{READ_CHAR_BUDGET} characters; the header reports bounded_by and next_offset when more lines "
                "remain. Each line is prefixed with its line number."),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Artifact id from list_artifacts."},
                    "offset": {"type": "integer", "minimum": 1, "description": "1-based first line number."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
                              "description": "Number of lines to return."},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
]
