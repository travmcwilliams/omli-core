"""Guards that apply to the whole test suite, checked by running pytest on small test files.

* Any test run fails if a file under the real home folder's ``.omli`` directory is created
  or changed, whichever test (UI or not) does it, and also when it happens at import time.
* A ResourceWarning (an unclosed file, for example) fails the run.
* A run fails when a test adds, removes or changes a file or folder in the checkout, at any
  depth; only the conftest's own bytecode and build output at the top are left out.

Each inner run uses this repository's pytest configuration and a copy of tests/conftest.py,
with the home folder pointed at a temporary directory that stands in for the real one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD_MESSAGE = "real home folder's .omli"


def _run(tmp_path, source, prepare=None):
    work = tmp_path / "suite"
    work.mkdir()
    shutil.copy(ROOT / "tests" / "conftest.py", work / "conftest.py")
    (work / "test_inner.py").write_text(textwrap.dedent(source), encoding="utf-8")
    home = tmp_path / "stand-in-home"
    home.mkdir()
    if prepare:
        prepare(home)
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-c", str(ROOT / "pyproject.toml"), "--rootdir", str(work),
         "--basetemp", str(tmp_path / "inner-tmp"), str(work / "test_inner.py")],
        cwd=work, env=env, capture_output=True, text=True, timeout=300)
    return proc.returncode, proc.stdout + proc.stderr


def _existing_key(home):
    (home / ".omli").mkdir()
    (home / ".omli" / "seal.key").write_text("stand-in\n", encoding="utf-8")


def test_a_test_that_creates_a_file_under_the_real_omli_folder_fails_the_run(tmp_path):
    code, out = _run(tmp_path, """
        from pathlib import Path

        def test_writes_into_home():
            d = Path.home() / ".omli"
            d.mkdir(exist_ok=True)
            (d / "stray.key").write_text("x")
    """)
    assert code != 0, out
    assert GUARD_MESSAGE in out, out
    assert "1 error" in out, "the test that wrote there is not the one reported"


def test_a_test_that_changes_an_existing_file_there_fails_the_run(tmp_path):
    code, out = _run(tmp_path, """
        from pathlib import Path

        def test_changes_the_key():
            with open(Path.home() / ".omli" / "seal.key", "a") as fh:
                fh.write("more")
    """, prepare=_existing_key)
    assert code != 0, out
    assert GUARD_MESSAGE in out, out


def test_a_write_at_import_time_fails_the_run(tmp_path):
    code, out = _run(tmp_path, """
        from pathlib import Path

        (Path.home() / ".omli").mkdir(exist_ok=True)
        (Path.home() / ".omli" / "at-import.key").write_text("x")

        def test_nothing():
            pass
    """)
    assert code != 0, out
    assert GUARD_MESSAGE in out, out


def test_a_write_after_the_last_test_fails_the_run(tmp_path):
    code, out = _run(tmp_path, """
        from pathlib import Path

        import pytest

        @pytest.fixture(scope="session")
        def writes_at_the_end():
            yield
            (Path.home() / ".omli").mkdir(exist_ok=True)
            (Path.home() / ".omli" / "late.key").write_text("x")

        def test_uses_it(writes_at_the_end):
            pass
    """)
    assert code != 0, out
    assert GUARD_MESSAGE in out, out


def test_a_test_that_leaves_the_home_folder_alone_passes(tmp_path):
    code, out = _run(tmp_path, """
        from pathlib import Path

        def test_reads_only(tmp_path):
            (tmp_path / "f.txt").write_text("x")
            assert (Path.home() / ".omli" / "seal.key").read_text() == "stand-in\\n"
    """, prepare=_existing_key)
    assert code == 0, out


def test_an_unclosed_file_fails_the_run(tmp_path):
    code, out = _run(tmp_path, """
        import gc

        def test_leaks_a_file(tmp_path):
            p = tmp_path / "f.txt"
            p.write_text("x")
            p.open().read()
            gc.collect()
    """)
    assert code != 0, out
    assert "ResourceWarning" in out, out


def test_a_closed_file_passes(tmp_path):
    code, out = _run(tmp_path, """
        def test_closes_the_file(tmp_path):
            p = tmp_path / "f.txt"
            p.write_text("x")
            with p.open() as fh:
                fh.read()
    """)
    assert code == 0, out


def _inner_checkout_run(tmp_path, body):
    """Run one inner test in a stand-in checkout made of this repository's pytest configuration
    and conftest, plus a tracked-looking file and package. Returns (exit code, output)."""
    checkout = tmp_path / "checkout"
    (checkout / "tests").mkdir(parents=True)
    (checkout / "pkg").mkdir()
    (checkout / "pkg" / "keep.py").write_text("X = 1", encoding="utf-8")
    (checkout / "README.md").write_text("readme", encoding="utf-8")
    shutil.copy(ROOT / "pyproject.toml", checkout / "pyproject.toml")
    shutil.copy(ROOT / "tests" / "conftest.py", checkout / "tests" / "conftest.py")
    lines = ["from pathlib import Path", "", "CHECKOUT = Path(__file__).resolve().parent.parent",
             "", "", "def test_inner():"] + ["    " + line for line in body]
    (checkout / "tests" / "test_inner.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    home = tmp_path / "stand-in-home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-c", str(checkout / "pyproject.toml"),
         "--rootdir", str(checkout), "--basetemp", str(tmp_path / "inner-tmp"),
         str(checkout / "tests" / "test_inner.py")],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=300)
    return proc.returncode, proc.stdout + proc.stderr


def _write(*parts):
    """Lines that make the folders of CHECKOUT/parts and write a file there."""
    path = " / ".join(f'"{x}"' for x in parts)
    folder = " / ".join(f'"{x}"' for x in parts[:-1])
    return [f"(CHECKOUT / {folder}).mkdir(parents=True, exist_ok=True)",
            f'(CHECKOUT / {path}).write_text("x")']


@pytest.mark.parametrize("body, reported", [
    (_write("ui", "keys.jsonl"), "added ui/keys.jsonl"),
    (['with open(CHECKOUT / "README.md", "a") as fh:', '    fh.write("more")'],
     "changed README.md"),
    (['(CHECKOUT / "pkg" / "keep.py").unlink()'], "removed pkg/keep.py"),
    (_write("pkg", "build", "k.jsonl"), "added pkg/build/k.jsonl"),
    (_write("tests", "dist", "s.key"), "added tests/dist/s.key"),
    (_write("pkg", "__pycache__", "planted.py"), "added pkg/__pycache__/planted.py"),
    (_write("pkg", "notes.egg-info", "d.txt"), "added pkg/notes.egg-info/d.txt"),
    (_write("pkg", "__pycache__", "keep.cpython-312.pyc"),
     "added pkg/__pycache__/keep.cpython-312.pyc"),
    (_write("tests", "__pycache__", "test_inner.cpython-312.pyc"),
     "added tests/__pycache__/test_inner.cpython-312.pyc"),
], ids=["new folder", "changed file", "removed file", "nested build", "nested dist",
        "non-bytecode in __pycache__", "nested egg-info", "package bytecode",
        "test bytecode"])
def test_a_test_that_writes_into_the_checkout_fails_the_run(tmp_path, body, reported):
    code, out = _inner_checkout_run(tmp_path, body)
    assert code != 0, out
    assert "tests wrote into the checkout" in out and reported in out, out


def test_the_conftest_bytecode_and_top_level_build_output_are_not_counted(tmp_path):
    code, out = _inner_checkout_run(
        tmp_path, _write("tests", "__pycache__", "conftest.cpython-312-pytest-9.2.0.pyc")
        + _write("build", "lib.txt"))
    assert code == 0, out
