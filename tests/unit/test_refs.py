"""Refs: `<store>:e<id>`, `n`, `p`."""

from __future__ import annotations

import pytest

from cairn.core.refs import Ref, RefError, parse


@pytest.mark.parametrize(("raw", "kinds", "expected"), [
    ("acme:e812", "enp", Ref("acme", "e", 812)),
    (" me:n41 ", "n", Ref("me", "n", 41)),
    ("team-2:p3", "np", Ref("team-2", "p", 3)),
    ("a" * 32 + ":e1", "e", Ref("a" * 32, "e", 1)),
])
def test_parse(raw: str, kinds: str, expected: Ref) -> None:
    ref = parse(raw, kinds)
    assert ref == expected and str(ref) == raw.strip()


@pytest.mark.parametrize(("raw", "kinds"), [
    ("acme:e", "e"),
    ("acme:x1", "enp"),
    ("Acme:e1", "e"),
    ("1acme:e1", "e"),
    ("acme/e1", "e"),
    ("acme:e-1", "e"),
    ("acme:e1:", "e"),
    ("a" * 33 + ":e1", "e"),
    ("acme:e1", "np"),  # right shape, wrong kind
])
def test_parse_rejects(raw: str, kinds: str) -> None:
    with pytest.raises(RefError, match="expected"):
        parse(raw, kinds)
