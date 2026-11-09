"""Authoring surface for the Connection Registry.

Authoring writes a NEW *draft* version; promotion to production stays a human sign-off / PR step.
So this module can:

  * template()      — a starting registry (the example),
  * validate(data)  — run the CANONICAL Python validator,
  * dry_run(data)   — run the real scope gate over representative actions,
  * to_yaml(data)   — render the assembled registry,
  * save_draft(data)— write a status:draft YAML to a scratch dir.

It deliberately CANNOT write a production registry, open a PR, reach a target, or resolve a
secret. `sender.py` and `reader.py` are the adapters that reach targets.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from omli.connections.schema import parse_registry
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate
from omli.defaults import default_pack_path
from omli.home import omli_home

_EXAMPLE = default_pack_path("connection_registry.example.yaml")
# Drafts are pre-sign-off scratch, never the live production registry. None means the default
# location under OMLI_HOME; set it to send drafts elsewhere.
DRAFTS_DIR: Optional[Path] = None


def _drafts_dir() -> Path:
    return DRAFTS_DIR if DRAFTS_DIR is not None else omli_home() / "var" / "connections" / "drafts"


def template() -> dict:
    """A valid starting registry (the shipped example)."""
    return yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))


def validate(data: Mapping[str, Any]) -> dict:
    """Run the canonical Python validator. Returns {ok, errors, connection_ids}."""
    try:
        reg = parse_registry(data)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "connection_ids": []}
    return {"ok": True, "errors": [], "connection_ids": list(reg.ids())}


def to_yaml(data: Mapping[str, Any]) -> str:
    """Render the assembled registry as readable YAML (declaration order preserved)."""
    return yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True)


# -- dry-run the gate against representative, config-derived actions ----------


def _concrete(glob: str) -> str:
    """A concrete example value for a glob, so a dry-run reads naturally."""
    v = glob.replace("/**", "/example.py").replace("**/", "").replace("**", "example")
    return re.sub(r"\*", "example", v)


def default_samples(connection) -> list[tuple[str, ConnectionAction]]:
    """Derive representative (label, action) pairs from a parsed connection's own selection."""
    allowed = tuple(connection.target.sensitivity_allowed)
    samples: list[tuple[str, ConnectionAction]] = []
    cid = connection.connection_id

    for r in connection.selection.resources:
        if not r.selected:
            # one denied row exercises default-deny
            if r.kind == "branch":
                samples.append((f"blocked: {r.id} branch (unticked)",
                                ConnectionAction(cid, "write-branch", branch=_concrete(r.id), sensitivity_tags=allowed)))
            else:
                samples.append((f"blocked: {r.id} (unticked)",
                                ConnectionAction(cid, "read", path=_concrete(r.id), sensitivity_tags=allowed)))
            continue
        if r.kind == "branch":
            if "write-branch" in r.operations:
                samples.append((f"write-branch: {r.id}",
                                ConnectionAction(cid, "write-branch", branch=_concrete(r.id), sensitivity_tags=allowed)))
        else:
            if "read" in r.operations:
                samples.append((f"read: {r.id}",
                                ConnectionAction(cid, "read", path=_concrete(r.id), sensitivity_tags=allowed)))
            if "propose-pr" in r.operations:
                samples.append((f"propose-pr: {r.id}",
                                ConnectionAction(cid, "propose-pr", path=_concrete(r.id),
                                                 sensitivity_tags=allowed, files_in_change=2, lines_in_change=40)))

    # A forbidden-path probe and a sensitivity-mismatch probe, if the config lets us build them.
    # They come after the cap, so a long selection cannot crowd them out.
    samples = samples[:8]
    if connection.blast_radius.forbidden_paths:
        fp = _concrete(connection.blast_radius.forbidden_paths[0])
        samples.append(("forbidden-path probe", ConnectionAction(cid, "read", path=fp, sensitivity_tags=allowed)))
    first_read = next((r for r in connection.selection.resources
                       if r.selected and r.kind != "branch" and "read" in r.operations), None)
    if first_read is not None:
        samples.append(("sensitivity mismatch (fail closed)",
                        ConnectionAction(cid, "read", path=_concrete(first_read.id),
                                         sensitivity_tags=("local-only:probe",))))
    return samples


def dry_run(data: Mapping[str, Any], connection_id: Optional[str] = None) -> dict:
    """Validate, then run the real scope gate over derived samples for one connection."""
    try:
        reg = parse_registry(data)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "results": []}
    if not reg.connections:
        return {"ok": False, "errors": ["registry has no connections"], "results": []}
    conn = reg.get(connection_id) if connection_id else reg.connections[0]

    results = []
    for label, action in default_samples(conn):
        decision = connection_scope_gate(action, conn)
        results.append({
            "label": label,
            "operation": action.operation,
            "target": action.branch or action.path,
            "decision": decision.decision,
            "reason": decision.reason or "",
        })
    return {"ok": True, "errors": [], "connection_id": conn.connection_id, "results": results}


# -- draft persistence (draft-only; never production, never a PR) -------------


def save_draft(data: Mapping[str, Any]) -> dict:
    """Force status:draft, validate, and write to the drafts dir. Never production."""
    payload = dict(data)
    meta = dict(payload.get("metadata") or {})
    meta["status"] = "draft"  # this path authors drafts only — promotion is a human sign-off
    payload["metadata"] = meta

    check = validate(payload)
    if not check["ok"]:
        return {"ok": False, "errors": check["errors"]}

    registry_id = str(meta.get("registry_id") or "connection_registry")
    version = str(meta.get("registry_version") or "0.0.0")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{registry_id}.{version}.{stamp}")
    drafts = _drafts_dir()
    drafts.mkdir(parents=True, exist_ok=True)
    out = drafts / f"{safe}.draft.yaml"
    out.write_text(to_yaml(payload), encoding="utf-8")
    return {
        "ok": True,
        "status": "draft",
        "registry_id": registry_id,
        "registry_version": version,
        "path": str(out),
        "filename": out.name,
        "note": "Draft saved. Promotion to production is a human sign-off / PR — not done here.",
    }
