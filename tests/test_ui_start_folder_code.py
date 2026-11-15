"""The UI command loads no module of its own from the folder it is started in.

``python -m`` puts the start folder first on the import path, so a module planted there (an
``argparse.py``, say) would run as the user. The command drops that folder before importing
anything else, and every message and document names the ``omli-ui`` command instead.
Known limit, stated in SECURITY.md: under ``python -m``, Python's own start-up modules (such as
``warnings``) and ``omli`` itself are imported before the command runs, so only ``omli-ui``,
``python -P`` or ``python -I`` keeps them out of that folder.
Runs with no extras: ``--help`` needs no web package.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_a_module_planted_in_the_start_folder_is_not_loaded(tmp_path):
    folder = tmp_path / "prepared folder"
    folder.mkdir()
    marker = folder / "PLANTED-RAN.txt"
    (folder / "argparse.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        "raise SystemExit('planted module ran instead of argparse')\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    proc = subprocess.run([sys.executable, "-m", "omli.ui", "--help"], cwd=folder, env=env,
                          capture_output=True, text=True, timeout=120)
    assert not marker.exists(), "a module from the start folder ran"
    assert proc.returncode == 0, proc.stderr
    assert "usage: omli-ui" in proc.stdout


def test_messages_and_documents_name_the_omli_ui_command():
    offenders = []
    for path in sorted((ROOT / "omli" / "ui").glob("*.py")) + [ROOT / "SECURITY.md"]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "python -m omli.ui" in line:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{n}")
    readme = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    for n, line in enumerate(readme, 1):
        if "python -m omli.ui" in line and "folder you run it in" not in line:
            offenders.append(f"README.md:{n}")
    assert not offenders, offenders


def test_the_readme_explains_the_risk_of_the_module_form():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"python -m omli\.ui.*folder you run it in", text)


def test_security_states_the_limit_of_the_module_form_and_how_to_avoid_it():
    """Under python -m, Python loads its own start-up modules (and omli itself) from the start
    folder before any OMLi code runs; the guard cannot help there. SECURITY.md says so and
    names the safe forms."""
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "modules it loads at start-up" in text
    assert "python -P -m omli.ui" in text and "python -I -m omli.ui" in text


def test_security_states_where_external_programs_come_from():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "only from an absolute folder on PATH" in text
    assert "found on the search path" not in text
