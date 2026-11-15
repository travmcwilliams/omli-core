"""OMLi starts an external program only from an absolute folder on PATH.

``shutil.which`` searches the current folder first on Windows (always before Python 3.12, and
by default after), and any PATH can hold ``.``, an empty entry or a relative folder. Any of
those would let a folder someone else prepared supply a program OMLi runs. So programs are
found only through absolute PATH entries, never the current folder. This covers the GPU
sampler (``omli.hardware``), the traffic gate's VRAM reading (``omli.gates.traffic``) and the
browser launcher of ``omli-ui login`` (``omli.ui.client``).

Each test plants a fake program that writes a marker file if it ever runs.
"""

from __future__ import annotations

import importlib
import os
import stat
import sys
from pathlib import Path

import pytest

IS_WIN = os.name == "nt"


def _plant(folder: Path, name: str, marker: Path) -> Path:
    """A program called ``name`` that writes ``marker`` when run."""
    folder.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        p = folder / f"{name}.bat"
        p.write_text(f'@echo ran> "{marker}"\r\n', encoding="utf-8")
    else:
        p = folder / name
        p.write_text(f"#!/bin/sh\necho ran > '{marker}'\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """The current folder holds planted programs, and PATH points at it in every way it can:
    '.', an empty entry, a relative entry, and the folder's own absolute path."""
    start = tmp_path / "prepared folder"
    marker = tmp_path / "PLANTED-RAN.txt"
    for name in ("nvidia-smi", "xdg-open", "open"):
        _plant(start, name, marker)
        _plant(start / "rel", name, marker)
        _plant(start / "bin", name, marker)
        # also the bare name, which is how macOS and Linux look a program up
        (start / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.chdir(start)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "rel", str(start)]))
    monkeypatch.delenv("OMLI_GPU_TELEMETRY", raising=False)
    return start, marker


@pytest.fixture
def hardware(monkeypatch):
    mod = importlib.import_module("omli.hardware")
    yield mod
    monkeypatch.undo()
    importlib.reload(mod)  # back to the real PATH for later tests


def test_find_program_never_returns_a_program_from_the_current_folder(prepared):
    programs = importlib.import_module("omli.programs")
    for name in ("nvidia-smi", "xdg-open", "open"):
        assert programs.find_program(name) is None, name


def test_find_program_finds_a_program_in_an_absolute_path_entry(prepared, tmp_path,
                                                                monkeypatch):
    start, marker = prepared
    real = _plant(tmp_path / "drivers", "nvidia-smi", marker)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "rel", str(tmp_path / "drivers")]))
    found = importlib.import_module("omli.programs").find_program("nvidia-smi")
    assert found is not None and Path(found).resolve() == real.resolve()


def test_the_gpu_sampler_never_runs_a_planted_nvidia_smi(prepared, hardware):
    start, marker = prepared
    importlib.reload(hardware)
    hardware._cache.update(t=None, value=None)
    assert hardware.probe_gpu() is None and hardware.gpu_state() is None  # "no GPU data"
    assert not marker.exists(), "a program from the current folder ran"


def test_the_traffic_gate_never_runs_a_planted_nvidia_smi(prepared):
    start, marker = prepared
    traffic = importlib.import_module("omli.gates.traffic")
    assert traffic._nvidia_free_vram_mb() is None
    assert not marker.exists(), "a program from the current folder ran"


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_browser_launcher_is_never_taken_from_the_current_folder(prepared, monkeypatch,
                                                                     platform):
    start, marker = prepared
    client = importlib.import_module("omli.ui.client")
    monkeypatch.setattr(sys, "platform", platform)
    assert client.browser_launcher() is None
    assert client.open_browser("file:///nowhere.html") is False
    assert not marker.exists(), "a program from the current folder ran"


# --------------------------------------------------------------------------------------------
# Every spelling of the current folder, and entries that are not fully absolute
# --------------------------------------------------------------------------------------------

def _link_to(target: Path, link: Path) -> bool:
    """A junction (Windows) or symlink (macOS, Linux) at ``link`` pointing at ``target``."""
    try:
        if IS_WIN:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, ImportError, AttributeError):
        return False


def _spellings(start: Path, tmp_path: Path):
    """(label, PATH entry) pairs. Each names the start folder in another way, or is an entry
    with no drive, which is relative to the current drive."""
    s = str(start)
    out = [("plain", s), ("trailing separator and dot", os.path.join(s, "."))]
    if IS_WIN:
        drive, tail = s[0], s[2:]
        out += [
            ("long-path prefix", "\\\\?\\" + s),
            ("long-path UNC to the admin share", "\\\\?\\UNC\\localhost\\" + drive + "$" + tail),
            ("device prefix", "\\\\.\\" + s),
            ("admin share by name", "\\\\localhost\\" + drive + "$" + tail),
            ("admin share by address", "\\\\127.0.0.1\\" + drive + "$" + tail),
            ("forward slashes", s.replace("\\", "/")),
            ("trailing dot", s + "."),
            ("no drive", tail),
            ("no drive, a subfolder", tail + "\\bin"),
            ("no drive, forward slashes", tail.replace("\\", "/") + "/bin"),
        ]
    else:
        out += [("double leading slash", "/" + s), ("dot-dot", s + "/../" + start.name)]
    link = tmp_path / "link-to-start"
    if _link_to(start, link):
        out.append(("a link to the start folder", str(link)))
    return out


def test_no_spelling_of_the_current_folder_and_no_driveless_entry_is_used(prepared, tmp_path,
                                                                          monkeypatch):
    start, marker = prepared
    programs = importlib.import_module("omli.programs")
    labels = []
    for label, entry in _spellings(start, tmp_path):
        monkeypatch.setenv("PATH", entry)
        labels.append(label)
        for name in ("nvidia-smi", "xdg-open", "open"):
            assert programs.find_program(name) is None, (label, name)
        assert programs.accepted_entries() == [], label
    assert not marker.exists()
    if IS_WIN:
        assert {"long-path prefix", "admin share by name", "no drive, a subfolder"} <= set(labels)


def test_the_gpu_sampler_runs_nothing_through_any_spelling(prepared, hardware, tmp_path,
                                                           monkeypatch):
    start, marker = prepared
    for label, entry in _spellings(start, tmp_path):
        monkeypatch.setenv("PATH", entry)
        importlib.reload(hardware)
        hardware._cache.update(t=None, value=None)
        assert hardware.probe_gpu() is None, label
        assert importlib.import_module("omli.gates.traffic")._nvidia_free_vram_mb() is None
        assert not marker.exists(), f"a planted program ran through: {label}"


@pytest.mark.skipif(IS_WIN, reason="a file symlink needs extra privilege on Windows")
def test_a_program_linked_from_the_current_folder_is_not_used(prepared, tmp_path, monkeypatch):
    start, marker = prepared
    drivers = tmp_path / "drivers"
    drivers.mkdir()
    os.symlink(start / "nvidia-smi", drivers / "nvidia-smi")
    monkeypatch.setenv("PATH", str(drivers))
    programs = importlib.import_module("omli.programs")
    assert programs.accepted_entries() == [str(drivers)]
    assert programs.find_program("nvidia-smi") is None


def test_an_absolute_folder_on_another_spelling_still_works(prepared, tmp_path, monkeypatch):
    start, marker = prepared
    real = _plant(tmp_path / "drivers", "nvidia-smi", marker)
    entry = str(real.parent)
    if IS_WIN:
        entry = "\\\\?\\" + entry
    monkeypatch.setenv("PATH", os.pathsep.join([str(start), entry]))
    found = importlib.import_module("omli.programs").find_program("nvidia-smi")
    assert found is not None and os.path.samefile(found, real)


# --------------------------------------------------------------------------------------------
# Programs OMLi starts do not inherit the start folder or the unfiltered PATH
# --------------------------------------------------------------------------------------------

class _Spy:
    def __init__(self):
        self.calls = []

    def run(self, args, **kw):
        self.calls.append((list(args), kw.get("cwd"), (kw.get("env") or {}).get("PATH")))
        raise OSError("not really started")

    def startfile(self, target, *a, **kw):
        self.calls.append(([target], kw.get("cwd"), os.environ.get("PATH")))


def _assert_started_safely(calls, start, drivers):
    assert calls, "nothing was started"
    for args, cwd, path in calls:
        assert cwd is not None, f"{args[0]} inherited the start folder"
        assert not os.path.samefile(cwd, start), f"{args[0]} ran in the start folder"
        entries = [e for e in (path or "").split(os.pathsep) if e]
        assert entries, "PATH was not narrowed"
        for e in entries:
            assert os.path.isabs(e) and not os.path.samefile(e, start), e
        assert any(os.path.samefile(e, drivers) for e in entries)


def test_gpu_callers_start_nvidia_smi_outside_the_start_folder(prepared, hardware, tmp_path,
                                                               monkeypatch):
    start, marker = prepared
    drivers = tmp_path / "drivers"
    _plant(drivers, "nvidia-smi", marker)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "rel", str(start), str(drivers)]))
    spy = _Spy()
    import subprocess

    monkeypatch.setattr(subprocess, "run", spy.run)
    importlib.reload(hardware)
    hardware._cache.update(t=None, value=None)
    assert hardware.probe_gpu() is None
    assert importlib.import_module("omli.gates.traffic")._nvidia_free_vram_mb() is None
    assert len(spy.calls) == 2
    _assert_started_safely(spy.calls, start, drivers)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_browser_launcher_starts_outside_the_start_folder(prepared, tmp_path, monkeypatch,
                                                              platform):
    start, marker = prepared
    drivers = tmp_path / "drivers"
    _plant(drivers, "open" if platform == "darwin" else "xdg-open", marker)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "rel", str(start), str(drivers)]))
    spy = _Spy()
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", spy.run)
    monkeypatch.setattr(sys, "platform", platform)
    client = importlib.import_module("omli.ui.client")
    client.open_browser("file:///nowhere.html")
    _assert_started_safely(spy.calls, start, drivers)


@pytest.mark.skipif(not IS_WIN, reason="the Windows launcher is os.startfile")
def test_the_windows_launcher_starts_outside_the_start_folder(prepared, tmp_path, monkeypatch):
    start, marker = prepared
    drivers = tmp_path / "drivers"
    drivers.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "rel", str(start), str(drivers)]))
    spy = _Spy()
    monkeypatch.setattr(os, "startfile", spy.startfile)
    client = importlib.import_module("omli.ui.client")
    assert client.open_browser("file:///nowhere.html") is True
    _assert_started_safely(spy.calls, start, drivers)
    assert os.environ["PATH"].split(os.pathsep)[0] == ".", "PATH was not restored"


def _wait_for(path: Path, seconds: float = 30.0) -> bool:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def test_a_helper_in_the_start_folder_is_not_run_by_the_real_launcher(prepared, tmp_path,
                                                                      monkeypatch):
    """End to end on this platform. A trusted launcher looks a helper up by name, the way
    ``xdg-open`` and file associations do, and the start folder holds a planted helper."""
    start, marker = prepared
    helper_ran = tmp_path / "HELPER-RAN.txt"
    done = tmp_path / "launcher-done.txt"
    drivers = tmp_path / "drivers"
    drivers.mkdir()
    client = importlib.import_module("omli.ui.client")
    if IS_WIN:
        (start / "omlihelper.bat").write_text(f'@echo ran> "{helper_ran}"\r\n', encoding="utf-8")
        launcher = drivers / "launch.bat"
        launcher.write_text(f'@call omlihelper 2>nul\r\n@echo done> "{done}"\r\n@exit\r\n',
                            encoding="utf-8")
        monkeypatch.setenv("PATH", os.pathsep.join([".", str(start), str(drivers),
                                                    os.environ.get("PATH", "")]))
        assert client.open_browser(str(launcher)) is True
    else:
        helper = start / "omlihelper"
        helper.write_text(f"#!/bin/sh\necho ran > '{helper_ran}'\n", encoding="utf-8")
        helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
        name = "open" if sys.platform == "darwin" else "xdg-open"
        launcher = drivers / name
        launcher.write_text(f"#!/bin/sh\nomlihelper\necho done > '{done}'\n", encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", os.pathsep.join([".", str(start), str(drivers),
                                                    "/usr/bin", "/bin"]))
        assert client.open_browser("file:///nowhere.html") is True
    assert _wait_for(done), "the launcher did not finish"
    assert not helper_ran.exists(), "a helper from the start folder ran"


def test_the_folder_that_really_holds_the_program_is_checked(prepared, tmp_path, monkeypatch):
    """Every platform: the chosen program resolves (as a link would) into the start folder."""
    start, marker = prepared
    real = _plant(tmp_path / "drivers", "nvidia-smi", marker)
    planted = next(p for p in start.iterdir() if p.name.startswith("nvidia-smi") and
                   p.suffix == real.suffix)
    monkeypatch.setenv("PATH", str(real.parent))
    original = os.path.realpath

    def through_a_link(path, *a, **kw):
        return str(planted) if os.path.basename(str(path)) == real.name else original(path, *a, **kw)

    monkeypatch.setattr(os.path, "realpath", through_a_link)
    assert importlib.import_module("omli.programs").find_program("nvidia-smi") is None


def test_the_launcher_handle_is_kept_while_it_runs(tmp_path, monkeypatch):
    """Every platform: on macOS and Linux the browser launcher is started and left running.
    Its process handle must not be dropped while it runs (Python reports a dropped, running
    child as a ResourceWarning), so the client keeps it until the launcher has finished."""
    import gc
    import types
    import warnings

    client = importlib.import_module("omli.ui.client")
    slow = tmp_path / "slow_launcher.py"
    slow.write_text("import time\ntime.sleep(3)\n", encoding="utf-8")
    # Take the macOS and Linux branch on every platform, with Python as the "launcher".
    monkeypatch.setattr(client, "sys", types.SimpleNamespace(platform="linux"))
    monkeypatch.setattr(client, "browser_launcher", lambda: sys.executable)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert client.open_browser(str(slow)) is True
        gc.collect()
    running = [w for w in caught if issubclass(w.category, ResourceWarning)]
    for proc in list(getattr(client, "_STARTED", [])):
        proc.wait(30)
    assert not running, [str(w.message) for w in running]
