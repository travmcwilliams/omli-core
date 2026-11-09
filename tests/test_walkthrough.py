"""The walkthrough runs from any directory, prints its key lines and leaves an existing home alone."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import omli

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "examples" / "walkthrough.py"
ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]|(?<![\w.])/(?:tmp|home|Users|var|private)/")

KEY_LINES = (
    "STEP 1: load the example pack and model registry",
    "STEP 2: CanonicalInput to OutcomeRecord, each step wrapped in gates",
    "chain complete: 5 artifacts in the log",
    "pre-gate block: 0 artifacts written",
    "post-gate block: 0 artifacts written",
    "local-only block: 0 provider calls, 0 artifacts written",
    "TAMPERED",
    "DONE: every step behaved as described",
)


def _run(cwd: Path, **extra) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("OMLI_HOME", "OMLI_SECRETS_DIR")}
    # The package is importable here however it was installed; the script imports it normally.
    env["PYTHONPATH"] = str(Path(omli.__file__).resolve().parents[1])
    env.update(extra)
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("cwd"))


def test_walkthrough_exits_zero(result):
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("line", KEY_LINES)
def test_walkthrough_prints_key_line(result, line):
    assert line in result.stdout


def test_walkthrough_prints_gate_events_in_order(result):
    first_step = result.stdout.split("STEP 3")[0]
    names = re.findall(r"event (\w+)", first_step)
    assert names[names.index("pre_gate"):][:5] == ["pre_gate", "model_call", "post_gate", "artifact_written", "wrote"]
    assert names.count("pre_gate") == 4


def test_walkthrough_blocks_are_announced(result):
    third = result.stdout.split("STEP 3")[1].split("STEP 5")[0]
    assert "event blocked_pre" in third
    assert "event blocked_post" in third
    assert "reason=sensitivity-local-only" in third


def test_walkthrough_verifies_the_original_before_the_copy(result):
    fifth = result.stdout.split("STEP 5")[1]
    assert fifth.index("verify the original") < fifth.index("verify the edited copy") < fifth.index("TAMPERED")


def test_walkthrough_prints_plain_text_without_absolute_paths(result):
    assert result.stdout.isascii()
    assert not ABSOLUTE_PATH.search(result.stdout), result.stdout


def test_walkthrough_leaves_nothing_in_the_current_directory(tmp_path):
    assert _run(tmp_path).returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_walkthrough_uses_a_preset_home_without_touching_what_is_there(tmp_path):
    home = tmp_path / "home"
    log = home / "var" / "artifacts.jsonl"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"existing\n")
    assert _run(tmp_path, OMLI_HOME=str(home)).returncode == 0
    assert log.read_bytes() == b"existing\n"
    assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*")) == ["var", "var/artifacts.jsonl"]
