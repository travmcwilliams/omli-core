"""A tour of OMLi that needs no network and no model server: python examples/walkthrough.py

It runs the five-artifact chain through the gates, shows each gate refuse, shows a local-only input
kept from a hosted model, and shows a tampered log being caught. EchoProvider stands in for the model.

Uses OMLI_HOME when it is set, else a temporary directory. Either way it works in a new subfolder
and removes it at the end, so it never adds to an existing artifact log or seal.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

from omli.agents.base import Agent
from omli.artifacts import ArtifactStore, Provenance, new_artifact
from omli.defaults import default_pack_path
from omli.events.bus import EventBus
from omli.integrity.__main__ import main as integrity_cli
from omli.models.provider import EchoProvider
from omli.models.registry import load_registry
from omli.packs.loader import load_pack
from omli.packs.schema import ModelSelection

LOCAL_MODEL = "example-local-4b"
HOSTED_MODEL = "hosted-burst"
CHAIN = ("Spec", "DecisionRecord", "WorkResult", "OutcomeRecord")


class CountingEcho(EchoProvider):
    """EchoProvider that counts its calls, to show when a provider was never reached."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, entry, request):
        self.calls += 1
        return super().invoke(entry, request)


def show_event(event) -> None:
    fields = {k: event.payload[k] for k in ("kind", "model", "passed", "reason") if event.payload.get(k) is not None}
    detail = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"  event {event.name} {detail}".rstrip())


def canonical(payload, sensitivity=()):
    provenance = Provenance(agent="Walkthrough", pack_name="example", pack_version="0.1.0",
                            source_refs=("walkthrough://input",), sensitivity=sensitivity)
    return new_artifact("CanonicalInput", payload, provenance)


def count_lines(log: Path) -> int:
    return len(log.read_bytes().splitlines()) if log.exists() else 0


def run_cli(work: Path, *args: str) -> int:
    """Call the integrity CLI and print its output with the working folder hidden."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = integrity_cli(list(args))
    text = out.getvalue()
    for root in {str(work), str(work.resolve())}:
        text = text.replace(root + os.sep, "").replace(root, ".")
    for line in text.splitlines():
        print("  " + line.encode("ascii", "replace").decode("ascii"))
    print(f"  exit code {code}")
    return code


def walkthrough(work: Path) -> None:
    log = work / "var" / "artifacts.jsonl"
    log.parent.mkdir(parents=True)
    bus = EventBus()
    bus.subscribe(show_event)
    store = ArtifactStore(log, bus=bus)

    print("STEP 1: load the example pack and model registry")
    registry = load_registry(default_pack_path("model_registry.example.yaml"))
    example = load_pack(default_pack_path("example_pack.yaml"))
    # The example pack selects no model, so choose one here. A deployment sets this in its pack.
    pack = dataclasses.replace(example, model_selection=ModelSelection(model_id=LOCAL_MODEL))
    # EchoProvider answers in place of the local model's real adapter.
    providers = {registry.get(LOCAL_MODEL).adapter: EchoProvider()}
    print(f"  pack {pack.name}@{pack.version}, registry {registry.metadata.registry_version}, "
          f"{len(registry.models)} models, EchoProvider standing in for {LOCAL_MODEL}")

    print("STEP 2: CanonicalInput to OutcomeRecord, each step wrapped in gates")
    current = canonical({"q": "Summarize the release notes"})
    store.write(current)
    agent = Agent(pack, registry, providers, store, bus=bus)
    for kind in CHAIN:
        print(f" {current.kind} -> {kind}")
        current = agent.run(current, kind)
        assert current is not None, kind
    print(f"  chain complete: {count_lines(log)} artifacts in the log")

    print("STEP 3: gates that refuse write nothing")
    before = count_lines(log)
    print(" pre-gate, empty payload")
    assert agent.run(canonical({}), "Spec") is None
    print(f"  pre-gate block: {count_lines(log) - before} artifacts written")
    strict = dataclasses.replace(pack, required_output_fields=(*pack.required_output_fields, "missing_field"))
    print(" post-gate, output missing a required field")
    assert Agent(strict, registry, providers, store, bus=bus).run(canonical({"q": "x"}), "Spec") is None
    print(f"  post-gate block: {count_lines(log) - before} artifacts written")

    print("STEP 4: a local-only input never reaches a hosted model")
    hosted = dataclasses.replace(example, model_selection=ModelSelection(model_id=HOSTED_MODEL))
    counting = CountingEcho()
    hosted_agent = Agent(hosted, registry, {registry.get(HOSTED_MODEL).adapter: counting}, store, bus=bus)
    assert hosted_agent.run(canonical({"q": "private"}, sensitivity=("local-only",)), "Spec") is None
    print(f"  local-only block: {counting.calls} provider calls, {count_lines(log) - before} artifacts written")

    print("STEP 5: seal the log, edit one line in a copy, verify")
    seal, anchor = log.with_suffix(".seal.json"), work / "integrity" / "artifacts.heads.jsonl"
    # --no-sign: signing would need the private key, which this tour never touches.
    assert run_cli(work, "--no-sign", "seal", str(log), str(seal), str(anchor)) == 0
    print(" verify the original")
    assert run_cli(work, "--no-sign", "verify", str(log), str(seal), str(anchor)) == 0
    copy = work / "var" / "copy.jsonl"
    shutil.copyfile(log, copy)
    data = copy.read_bytes()
    edited = data.replace(b"Summarize the release notes", b"Summarize the REDACTED notes", 1)
    assert edited != data
    copy.write_bytes(edited)
    print(" verify the edited copy")
    assert run_cli(work, "--no-sign", "verify", str(copy), str(seal), str(anchor)) != 0

    print("DONE: every step behaved as described")


def main() -> int:
    if not os.environ.get("OMLI_HOME"):
        os.environ["OMLI_HOME"] = tempfile.mkdtemp(prefix="omli_walkthrough_")
        created = Path(os.environ["OMLI_HOME"])
    else:
        created = None
    home = Path(os.environ["OMLI_HOME"]).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="walkthrough_", dir=home))
    try:
        walkthrough(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if created is not None:
            shutil.rmtree(created, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
