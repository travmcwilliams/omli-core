"""The secret:// resolver — env-first, then a file in the configured store, fail-closed.

Uses only fake secrets; never touches a real credential. Verifies the reference->env-var
mapping, both resolution sources, the required-raises behaviour, and that secret_status reports
presence without exposing the value.
"""

from __future__ import annotations

import pytest

from omli.connections import secrets as S


def test_env_var_name_mapping():
    assert S.env_var_for("secret://connections/example_api/api_key") == "OMLI_SECRET_CONNECTIONS__EXAMPLE_API__API_KEY"
    assert S.env_var_for("secret://connections/other_api/api_key") == "OMLI_SECRET_CONNECTIONS__OTHER_API__API_KEY"


def test_non_secret_ref_rejected():
    with pytest.raises(ValueError):
        S.env_var_for("inline-token")


def test_resolves_from_env_first(monkeypatch, tmp_path):
    """The environment wins over a file in the store when both hold a value."""
    monkeypatch.setattr(S, "_SECRETS_DIR", tmp_path)
    ref = "secret://connections/example_api/api_key"
    target = tmp_path / "connections" / "example_api" / "api_key"
    target.parent.mkdir(parents=True)
    target.write_text("file-value\n", encoding="utf-8")
    monkeypatch.setenv(S.env_var_for(ref), "  env-value  ")
    assert S.resolve_secret(ref) == "env-value"           # stripped, and not the file's value
    assert S.secret_status(ref).source == "env"


def test_resolves_from_file_when_no_env(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_SECRETS_DIR", tmp_path)
    ref = "secret://connections/other_api/api_key"
    monkeypatch.delenv(S.env_var_for(ref), raising=False)
    target = tmp_path / "connections" / "other_api" / "api_key"
    target.parent.mkdir(parents=True)
    target.write_text("file-value\n", encoding="utf-8")
    assert S.resolve_secret(ref) == "file-value"
    st = S.secret_status(ref)
    assert st.present and st.source == "file"
    assert "file-value" not in repr(st)


def test_missing_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_SECRETS_DIR", tmp_path)
    ref = "secret://connections/nope/api_key"
    monkeypatch.delenv(S.env_var_for(ref), raising=False)
    assert S.resolve_secret(ref) is None
    assert S.secret_status(ref).present is False
    with pytest.raises(S.SecretNotFound):
        S.resolve_secret(ref, required=True)


def test_blank_file_is_not_present(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_SECRETS_DIR", tmp_path)
    ref = "secret://connections/blank/api_key"
    monkeypatch.delenv(S.env_var_for(ref), raising=False)
    target = tmp_path / "connections" / "blank" / "api_key"
    target.parent.mkdir(parents=True)
    target.write_text("   \n", encoding="utf-8")           # whitespace only
    assert S.resolve_secret(ref) is None                   # blank != wired (fail closed)


def test_credential_for_reads_connection_ref(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_SECRETS_DIR", tmp_path)

    class _T:
        credential_ref = "secret://connections/example_api/api_key"

    class _C:
        target = _T()

    monkeypatch.setenv(S.env_var_for(_T.credential_ref), "k")
    assert S.credential_for(_C()) == "k"

    class _Anon:
        class target:  # noqa: D106
            credential_ref = None

    assert S.credential_for(_Anon()) is None
