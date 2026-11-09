"""The integrity CLI stands alone: its home is OMLI_HOME, and it imports only its own package."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import omli.integrity.__main__ as cli

CLI_SOURCE = Path(cli.__file__).read_text(encoding="utf-8")


def test_integrity_cli_imports_only_its_package_and_the_home_helper():
    imported = []
    for node in ast.walk(ast.parse(CLI_SOURCE)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    ours = [m for m in imported if m == "omli" or m.startswith("omli.")]
    assert ours
    assert all(m == "omli.home" or m.startswith("omli.integrity.") for m in ours)


def test_omli_home_sets_the_cli_root(monkeypatch, tmp_path):
    monkeypatch.setenv("OMLI_HOME", str(tmp_path))
    assert cli.home_root() == tmp_path.resolve()


def test_the_cli_source_names_no_other_root_variable(monkeypatch, tmp_path):
    monkeypatch.delenv("OMLI_HOME", raising=False)
    assert not [name for name in re.findall(r"[A-Z][A-Z0-9]*_[A-Z0-9_]+", CLI_SOURCE)
                if name.endswith(("_HOME", "_ROOT", "_DIR")) and name != "OMLI_HOME"]
    assert cli.home_root() != tmp_path.resolve()
