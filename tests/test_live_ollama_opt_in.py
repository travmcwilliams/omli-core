"""The live Ollama test does nothing, including probing the network, unless it is opted in."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BLOCKER = """
import socket
def _refuse(*a, **k):
    raise AssertionError("network attempted")
socket.socket.connect = _refuse
socket.create_connection = _refuse
"""


def test_live_ollama_module_skips_without_the_variable_and_opens_no_socket(tmp_path):
    (tmp_path / "netblock.py").write_text(BLOCKER, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "OMLI_LIVE_OLLAMA"}
    env["PYTHONPATH"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "netblock", "-q", "-rs", "-p", "no:cacheprovider",
         "tests/test_live_ollama_outcome.py"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode in (0, 5), proc.stdout + proc.stderr  # 5: nothing left to collect
    assert "OMLI_LIVE_OLLAMA=1" in proc.stdout
    assert "1 skipped" in proc.stdout
