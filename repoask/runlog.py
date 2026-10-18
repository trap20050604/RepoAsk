"""JSONL run tracing.

Every answering run writes one JSON line per event so that experiments can be
replayed and audited later. Secrets are redacted structurally: we never accept
a key named like a credential, and values are length-capped.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECRET_KEY_MARKERS = ("key", "token", "secret", "password", "authorization")
_MAX_VALUE_CHARS = 4000


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def _redact(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in SECRET_KEY_MARKERS):
        return "<redacted>"
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[:_MAX_VALUE_CHARS] + f"...<truncated {len(value) - _MAX_VALUE_CHARS} chars>"
    if isinstance(value, dict):
        return {k: _redact(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(key, item) for item in value]
    return value


class RunLogger:
    """Append-only JSONL writer for one run."""

    def __init__(self, path: str | Path, run_id: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or new_run_id()
        self._started = time.perf_counter()
        self._closed = False

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("RunLogger already closed")
        entry = {
            "run_id": self.run_id,
            "event": event,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": int((time.perf_counter() - self._started) * 1000),
        }
        entry.update({key: _redact(key, value) for key, value in fields.items()})
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a trace file back; used by tests and by the CLI summary command."""
    entries: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries
