"""Unit tests for the `**/`-aware glob matcher (omli/connections/globmatch.py).

This matcher backs `forbidden_paths` in the connection scope gate, so a bug here is a
silent default-deny bypass. The load-bearing case is the `**/` zero-leading-segment
semantics: `**/secrets/**` must catch a TOP-LEVEL `secrets/…`, not only a nested one —
the exact reason the `_DBL_SLASH -> (?:.*/)?` sentinel exists rather than plain `.*`.
"""

from __future__ import annotations

from omli.connections.globmatch import glob_match


# --- the forbidden-path bypass this code exists to prevent -------------------
def test_double_star_slash_matches_zero_leading_segments():
    # A top-level `secrets/` must match `**/secrets/**` — otherwise a forbidden path leaks.
    assert glob_match("**/secrets/**", "secrets/key.txt")


def test_double_star_slash_matches_nested_segments():
    assert glob_match("**/secrets/**", "src/services/secrets/key.txt")
    assert glob_match("**/secrets/**", "a/b/c/secrets/deep/file")


def test_double_star_slash_does_not_match_look_alike_prefix():
    # `notsecrets/` must NOT be caught by `**/secrets/**` (anchored, not substring).
    assert not glob_match("**/secrets/**", "src/notsecrets/key.txt")


def test_double_star_requires_the_literal_segment_present():
    assert not glob_match("**/secrets/**", "src/services/rsi.py")


# --- single vs double star segment semantics --------------------------------
def test_single_star_does_not_cross_a_slash():
    assert glob_match("src/*.py", "src/a.py")
    assert not glob_match("src/*.py", "src/sub/a.py")


def test_double_star_crosses_slashes():
    assert glob_match("src/**", "src/a/b/c")
    assert glob_match("src/**", "src/a")


# --- anchoring: patterns are a full match, not a search ----------------------
def test_match_is_anchored_full_match():
    assert glob_match("src/lib/*.py", "src/lib/utils.py")
    assert not glob_match("signals/*.py", "src/lib/utils.py")   # not anchored at start
    assert not glob_match("src/lib/*", "src/lib/utils.py/extra")


def test_regex_metacharacters_in_pattern_are_literal():
    # A '.' in the pattern must match a literal dot, not any char.
    assert glob_match("src/a.py", "src/a.py")
    assert not glob_match("src/a.py", "src/axpy")
    for meta in "+?^$|()[]{}":
        literal = f"src/a{meta}b"
        assert glob_match(literal, literal), meta
    assert not glob_match("src/a+b", "src/aab")
    assert not glob_match("src/a?b", "src/axb")
    assert not glob_match("src/(a|b)", "src/a")
    assert not glob_match("src/[ab]", "src/a")
