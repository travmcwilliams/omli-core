"""Best-effort local GPU telemetry. Generic, core-safe, and fail-quiet.

Exists because a model-call event that records *how long* a call took but not
*what the machine was doing* cannot explain its own variance. Latency can
vary widely across near-identical calls, and GPU contention cannot be shown as the cause
unless hardware state is recorded alongside the duration.

Contract:

* Returns ``None`` when there is no probe, rather than a zeroed dict. An absent
  reading and a reading of zero are different facts and must not be conflated —
  a fabricated 0% utilisation would be worse than no data at all.
* Cached with a short TTL so bursts of calls share one ``nvidia-smi`` spawn.
* Never raises. Telemetry must not be able to fail a model call.
* Names no model or domain term (Primitive 1).

Cost note: each uncached reading spawns an ``nvidia-smi`` subprocess. Callers MUST sample
outside any region they are timing — stamping this inside a latency measurement
would corrupt the number the telemetry exists to explain.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

#: Seconds a probe result stays fresh: calls within this window (for example a burst of
#: retries) share one reading.
GPU_TTL_S = 2.0

_SMI: Optional[str] = shutil.which("nvidia-smi")
_cache: dict = {"t": None, "value": None}

_DISABLED_VALUES = {"0", "off", "false", "no"}


def telemetry_enabled() -> bool:
    """False when the operator has switched hardware sampling off.

    The probe costs a subprocess spawn per sample; this is the escape hatch for
    a run that wants zero telemetry overhead.
    """
    return os.environ.get("OMLI_GPU_TELEMETRY", "1").strip().lower() not in _DISABLED_VALUES


def probe_gpu() -> Optional[dict]:
    """One uncached read. None if unavailable, disabled, or unparseable."""
    if not _SMI or not telemetry_enabled():
        return None
    try:
        result = subprocess.run(
            [
                _SMI,
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        util, used, total, temp = (x.strip() for x in result.stdout.splitlines()[0].split(","))
        used_mb, total_mb = int(float(used)), int(float(total))
        return {
            "util_pct": round(float(util), 1),
            "mem_used_mb": used_mb,
            "mem_total_mb": total_mb,
            "mem_free_mb": total_mb - used_mb,
            "temp_c": round(float(temp)),
        }
    except Exception:
        # Any failure is reported as "no reading", never as a zeroed one.
        return None


def gpu_state(*, ttl_s: float = GPU_TTL_S) -> Optional[dict]:
    """Cached GPU reading, or None when unavailable/disabled."""
    if not _SMI or not telemetry_enabled():
        return None
    now = time.monotonic()
    stamped = _cache["t"]
    if stamped is not None and (now - stamped) < ttl_s:
        return _cache["value"]
    value = probe_gpu()
    _cache["t"], _cache["value"] = now, value
    return value


def reset_cache() -> None:
    """Drop the cached reading (tests, and after a deliberate hardware change)."""
    _cache["t"], _cache["value"] = None, None
