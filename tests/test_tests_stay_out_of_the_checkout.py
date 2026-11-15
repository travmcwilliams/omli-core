"""Tests never write into the checkout: each one runs in its own temporary folder."""

from __future__ import annotations

import os
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent.parent


def test_the_current_folder_is_a_temporary_folder_outside_the_checkout():
    here = Path.cwd().resolve()
    assert here != CHECKOUT and CHECKOUT not in here.parents
    assert not any(os.scandir(here)), "the folder is not new"


def test_a_file_written_here_does_not_reach_the_checkout():
    Path("keys.jsonl").write_text("{}\n", encoding="utf-8")
    assert not (CHECKOUT / "keys.jsonl").exists()


def test_no_bytecode_is_written_by_the_run_or_the_programs_it_starts():
    import sys

    assert sys.dont_write_bytecode is True
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"


def test_pytest_keeps_no_cache_folder(request):
    assert not request.config.pluginmanager.has_plugin("cacheprovider")
