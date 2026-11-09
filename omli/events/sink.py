"""Durable event sink — append selected EventBus emissions to daily JSONL files.

The EventBus is in-memory per run: gate verdicts, model calls, escalations, and their
latencies/token counts vanish when the process exits, so the chain remembers *what* was
decided but not *what it cost*. This sink keeps that record: one best-effort tap in
``EventBus.emit`` appends each architectural event as an append-only fact (Primitive 4)
to ``<events_dir>/YYYY-MM-DD.jsonl`` (one file per UTC day), and it never raises.

Off by default, and on only when a caller invokes :func:`enable` with a directory, so a
throwaway bus never writes to disk. Purely observational: it records
choreography, it never participates in it (no orchestration, no auto-tuning).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from omli.frozen import thaw

_LOCK = threading.Lock()
_DIR: Optional[Path] = None

# Only the architectural event families are persisted. These are the emissions in
# omli/agents/base.py, which tests/test_release_hardening.py holds equal, plus the guard below.
PERSISTED_EVENTS = frozenset({
    "pre_gate", "blocked_pre", "retrieved", "model_call", "model_error",
    "post_gate", "escalated", "blocked_post", "wrote",
    # traffic guard (omli/gates/traffic.py): admit/defer/downgrade decisions with the
    # sampled hardware state.
    "traffic_gate",
})


def enable(events_dir: str | Path) -> None:
    """Turn the sink on, writing daily files under ``events_dir``. Idempotent."""
    global _DIR
    with _LOCK:
        _DIR = Path(events_dir)


def disable() -> None:
    """Turn the sink off (test hook / manual)."""
    global _DIR
    with _LOCK:
        _DIR = None


def is_enabled() -> bool:
    return _DIR is not None


def record(name: str, payload: Mapping[str, Any], seq: int) -> None:
    """Append one event row if the sink is on and the event is a persisted family.

    Never raises (a full disk degrades to silence, not a crashed run). Payload values that aren't JSON-serializable are
    stringified rather than dropped.
    """
    directory = _DIR
    if directory is None or name not in PERSISTED_EVENTS:
        return
    try:
        ts = datetime.now(timezone.utc).isoformat()
        row = {"ts": ts, "event": name, "seq": seq, **thaw(payload)}
        path = directory / f"{ts[:10]}.jsonl"
        line = json.dumps(row, ensure_ascii=False, default=str)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass
