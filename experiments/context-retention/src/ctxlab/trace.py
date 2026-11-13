"""Run recording: one directory per run with ``manifest.json``, ``events.jsonl`` and ``final.md``.

Events capture the chain the Lab is about: tool result -> context before/after compaction -> the exact outgoing
payload -> the model's action or final answer. Payloads are stored in full (no headers, no credentials). Any
configured secret that still appears in an event is replaced before the line is written, and counted.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Recorder:
    def __init__(self, run_dir: Path, secrets: list[str] | None = None):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.secrets = [s for s in (secrets or []) if s]
        self.redactions = 0
        self.seq = 0
        self._f = open(self.run_dir / "events.jsonl", "w", encoding="utf-8", newline="\n")
        self.t0 = time.monotonic()

    def event(self, type_: str, **data) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "type": type_, "t": now_iso(), "elapsed_s": round(time.monotonic() - self.t0, 3),
              **data}
        line = json.dumps(ev, ensure_ascii=False)
        for s in self.secrets:
            if s in line:
                self.redactions += line.count(s)
                line = line.replace(s, "[REDACTED]")
        self._f.write(line + "\n")
        self._f.flush()
        return ev

    def write_json(self, name: str, obj) -> None:
        with open(self.run_dir / name, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")

    def write_text(self, name: str, text: str) -> None:
        with open(self.run_dir / name, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def close(self) -> None:
        self._f.close()


class MemoryRecorder:
    """Same interface as Recorder, keeps everything in memory (replay checks and tests)."""

    def __init__(self):
        self.events: list[dict] = []
        self.files: dict[str, object] = {}
        self.seq = 0
        self.redactions = 0

    def event(self, type_: str, **data) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "type": type_, **data}
        self.events.append(ev)
        return ev

    def write_json(self, name: str, obj) -> None:
        self.files[name] = obj

    def write_text(self, name: str, text: str) -> None:
        self.files[name] = text

    def close(self) -> None:
        pass


def load_events(run_dir: Path) -> list[dict]:
    with open(Path(run_dir) / "events.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_manifest(run_dir: Path) -> dict:
    return json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
