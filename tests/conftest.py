"""Keep every test away from the real home folder's ``.omli`` directory.

* Whole suite: that directory is recorded when this file is loaded, before
  any test module is imported. After every test, and once more when the run ends, the
  directory must be exactly as recorded: no file or folder created, changed or removed.
  A difference fails that test, and the run, whichever test caused it.
* UI tests (``test_ui_*``) also run with the home folder pointed at a temporary directory,
  because the login key's default location is in the home folder.
* Every test runs with its own temporary folder as the current folder, so nothing a test
  writes "here" lands in the checkout. When the run ends, the checkout must be as it was when
  this file was loaded: a file or folder added or removed, or a file whose size or
  modification time changed, fails the run. Left out of that check: ``.git``,
  ``.pytest_cache``, ``build``, ``dist`` and ``*.egg-info`` at the top of the checkout only,
  and this file's own bytecode, ``tests/__pycache__/conftest.*.pyc``. Any other file in a
  ``__pycache__`` folder, bytecode included, counts like every other file: bytecode can
  stand in for its module.
* No bytecode is written for modules imported after this file, nor by programs the tests
  start. pytest compiles this file before it runs, so a plain ``pytest`` may still write
  ``tests/__pycache__/conftest…pyc``; ``python -B -m pytest`` writes none at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

_REAL_OMLI = Path.home() / ".omli"
_MESSAGE = "a test created, changed or removed something under the real home folder's .omli"


def _snapshot(root: Path):
    """Every path under ``root`` with its type, size and modification time; None if absent."""
    if not root.exists():
        return None
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            out[p.relative_to(root).as_posix()] = (p.is_dir(), st.st_size, st.st_mtime_ns)
    return out


_AT_START = _snapshot(_REAL_OMLI)

CHECKOUT = Path(__file__).resolve().parent.parent
_TOP_BY_PRODUCTS = {".git", ".pytest_cache", "build", "dist"}


def _left_out(rel: Path, is_dir: bool) -> bool:
    """Version control, caches and build output at the top; the conftest's own bytecode."""
    top = rel.parts[0]
    if len(rel.parts) == 1 and is_dir and (top in _TOP_BY_PRODUCTS or top.endswith(".egg-info")):
        return True
    if rel.name == "__pycache__" and is_dir:
        return True  # the folder itself; what it holds is checked below
    return (not is_dir and rel.parent.as_posix() == "tests/__pycache__"
            and rel.name.startswith("conftest.") and rel.suffix == ".pyc")


def _checkout_state():
    """Every path in the checkout: folders by presence, files by size and modification time."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(CHECKOUT):
        here = Path(dirpath)
        keep = []
        for d in dirnames:
            rel = (here / d).relative_to(CHECKOUT)
            if len(rel.parts) == 1 and _left_out(rel, True) and d != "__pycache__":
                continue  # not walked at all
            keep.append(d)
            if not _left_out(rel, True):
                out[rel.as_posix()] = ("dir",)
        dirnames[:] = keep
        for name in filenames:
            rel = (here / name).relative_to(CHECKOUT)
            if _left_out(rel, False):
                continue
            try:
                st = (here / name).stat()
            except FileNotFoundError:
                continue
            out[rel.as_posix()] = ("file", st.st_size, st.st_mtime_ns)
    return out


def _checkout_differences(before, after):
    """Paths added, removed or changed between two states."""
    return ([f"added {p}" for p in sorted(after.keys() - before.keys())]
            + [f"removed {p}" for p in sorted(before.keys() - after.keys())]
            + [f"changed {p}" for p in sorted(before.keys() & after.keys())
               if before[p] != after[p]])


# Only in a checkout (a pyproject.toml beside tests/), not in a copy of this file elsewhere.
_CHECKOUT_AT_START = _checkout_state() if (CHECKOUT / "pyproject.toml").is_file() else None


def _changed() -> bool:
    return _snapshot(_REAL_OMLI) != _AT_START


@pytest.fixture(autouse=True)
def _the_real_omli_folder_is_left_alone():
    yield
    assert not _changed(), _MESSAGE


def pytest_sessionfinish(session, exitstatus):
    # Covers writes no test teardown sees, such as one made when a module is imported.
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if _changed():
        if reporter is not None:
            reporter.write_line(f"ERROR: {_MESSAGE}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    diff = ([] if _CHECKOUT_AT_START is None
            else _checkout_differences(_CHECKOUT_AT_START, _checkout_state()))
    if diff:
        if reporter is not None:
            reporter.write_line(f"ERROR: tests wrote into the checkout: {diff[:10]}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _each_test_runs_in_a_temporary_folder(tmp_path_factory, monkeypatch):
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    yield


@pytest.fixture(autouse=True)
def _ui_tests_use_a_temporary_home(request, tmp_path_factory, monkeypatch):
    if not request.module.__name__.rsplit(".", 1)[-1].startswith("test_ui_"):
        yield
        return
    fake = tmp_path_factory.mktemp("fake-user-home")
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    # Path.home() follows the variables at call time, so a test may move it again.
    monkeypatch.setattr(Path, "home",
                        classmethod(lambda cls: Path(os.environ.get("USERPROFILE")
                                                     or os.environ["HOME"])))
    yield
