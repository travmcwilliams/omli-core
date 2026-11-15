"""The UI tests run on the test client Starlette recommends: starlette.testclient backed by
httpx2. pyproject.toml turns Starlette's "use httpx2" deprecation warning into an error, so a
fallback to plain httpx fails the run instead of warning."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parent.parent


def test_the_test_client_is_backed_by_httpx2():
    import starlette.testclient as tc

    assert tc.httpx.__name__ == "httpx2"


def test_the_dev_install_names_httpx2_and_the_ui_install_a_starlette_that_uses_it():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dev = re.search(r"^dev\s*=\s*\[(.*?)\]", text, re.M | re.S).group(1)
    ui = re.search(r"^ui\s*=\s*\[(.*?)\]", text, re.M | re.S).group(1)
    assert '"httpx2' in dev and '"httpx>' not in dev and '"httpx"' not in dev
    assert re.search(r'"starlette>=1\.2', ui)


def test_the_deprecation_warning_is_an_error_in_pytest_config(pytestconfig):
    filters = pytestconfig.getini("filterwarnings")
    assert any(f.startswith("error:Using `httpx` with `starlette.testclient` is deprecated")
               for f in filters), filters
