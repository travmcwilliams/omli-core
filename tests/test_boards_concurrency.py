"""A second writer waits for the board's lock: deterministic, every platform.

One process holds the board's lock. A second process, in the middle of an append, must not
finish while the lock is held, and must finish (with its whole line) only after the release.
Without the lock the second append lands at once, so the test fails on every run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOLD_SECONDS = 2.0

HOLDER = r"""
import sys
from omli.boards import BoardLog
log = BoardLog(sys.argv[1])
with log.locked():
    print("held", flush=True)
    sys.stdin.readline()
"""

WRITER = r"""
import sys
from omli.boards import BoardLog
log = BoardLog(sys.argv[1])
print("ready", flush=True)
log.append("checkpoint", op="from-writer", by="writer", label="second process")
print("done", flush=True)
"""


def _spawn(script, env, cwd, **kw):
    return subprocess.Popen([sys.executable, "-c", script, "shared"], cwd=str(cwd), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)


def _expect_line(proc, word):
    line = proc.stdout.readline().decode("utf-8").strip()
    if line != word:
        proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace")[-2000:]
        pytest.fail(f"expected {word!r} from the child, got {line!r}: {err}")


def test_a_second_writer_waits_for_the_lock_and_completes_only_after_release(tmp_path):
    try:
        import omli.boards  # noqa: F401
    except ImportError as exc:
        pytest.fail(f"omli.boards does not exist yet ({exc})")
    home = tmp_path / "Omli Home"
    env = dict(os.environ, OMLI_HOME=str(home), PYTHONPATH=str(REPO))
    log_path = home / "boards" / "shared.jsonl"

    holder = _spawn(HOLDER, env, tmp_path, stdin=subprocess.PIPE)
    writer = None
    try:
        _expect_line(holder, "held")
        writer = _spawn(WRITER, env, tmp_path)
        _expect_line(writer, "ready")
        time.sleep(HOLD_SECONDS)
        assert writer.poll() is None, "the second append finished while the lock was held"
        assert not log_path.exists() or b"from-writer" not in log_path.read_bytes()

        holder.stdin.write(b"release\n")
        holder.stdin.close()
        assert holder.wait(timeout=60) == 0
        released_at = time.monotonic()
        out, err = writer.communicate(timeout=60)
        assert writer.returncode == 0, err.decode("utf-8", "replace")[-2000:]
        assert out.decode("utf-8").strip() == "done"
        assert time.monotonic() >= released_at
    finally:
        for proc in (holder, writer):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc is not None:
                for stream in (proc.stdout, proc.stderr, proc.stdin):
                    if stream is not None and not stream.closed:
                        stream.close()

    lines = log_path.read_bytes().decode("utf-8").split("\n")
    assert lines[-1] == "" and len(lines) == 2
    assert json.loads(lines[0])["op"] == "from-writer"
