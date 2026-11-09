"""The shipped connection example holds placeholders only."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

EXAMPLE = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"
TEXT = EXAMPLE.read_text(encoding="utf-8")


#: The second-level names RFC 2606 reserves for examples, and the reserved top-level names.
RESERVED_SECOND_LEVEL = ("example.com", "example.net", "example.org")
RESERVED_TOP_LEVEL = ("example", "test", "invalid", "localhost")


def is_reserved_domain(domain: str) -> bool:
    d = domain.lower().rstrip(".")
    return (d.rsplit(".", 1)[-1] in RESERVED_TOP_LEVEL
            or any(d == r or d.endswith("." + r) for r in RESERVED_SECOND_LEVEL))


def test_the_reserved_domain_check_itself():
    for good in ("example.com", "mail.example.org", "corp.example", "host.test"):
        assert is_reserved_domain(good), good
    for bad in ("example.io", "notexample.com", "example.com.evil.net", "example.co"):
        assert not is_reserved_domain(bad), bad


def test_every_email_address_uses_a_reserved_example_domain():
    domains = re.findall(r"[A-Za-z0-9_.+*-]+@([A-Za-z0-9.-]+)", TEXT)
    assert domains
    assert [d for d in domains if not is_reserved_domain(d)] == []


def test_every_guid_is_the_zero_placeholder():
    guids = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", TEXT)
    assert guids and set(guids) == {"00000000-0000-0000-0000-000000000000"}


def test_every_slack_channel_id_is_a_placeholder():
    data = yaml.safe_load(TEXT)
    slack = [c for c in data["connections"] if c["target"]["kind"] == "slack"]
    ids = [r["id"] for c in slack for r in c["selection"]["resources"] if not r["id"].startswith("#")]
    assert ids == ["CEXAMPLE001"]


def test_example_names_no_docs_path():
    assert "docs/" not in TEXT
