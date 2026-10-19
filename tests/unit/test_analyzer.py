from __future__ import annotations

import random
import re

import pytest

from cairn.core.analyzer import _stem, analyze, is_opaque, variant


@pytest.mark.parametrize(("text", "tokens"), [
    ("FileLoader", ["file", "loader", "fileloader"]),
    ("file_loader", ["file", "loader", "fileloader"]),
    ("file-loader", ["file", "loader", "fileloader"]),
    ("ERR_TLS_CERT_ALTNAME_INVALID", ["err", "tls", "cert", "altnam", "invalid", "errtlscertaltnameinvalid"]),
    ("HTTPServerError", ["http", "server", "error", "httpservererror"]),
    ("src/cairn/core.py", ["src", "cairn", "core", "py", "srccairncorepy"]),
    ("v2.3.1", ["v2", "3", "1", "v231"]),
    # Parts are stemmed; the whole identifier is kept as written.
    ("RetryingLoaders", ["retri", "loader", "retryingloaders"]),
    ("retries retrying caching", ["retri", "retri", "cach"]),
    # NFKC, casefold, then diacritics stripped.
    ("Café NAÏVE Straße", ["cafe", "naiv", "strass"]),
    ("ﬁle Ｆｉｌｅ", ["file", "file"]),
    ("trailing, punctuation! (and) [brackets]", ["trail", "punctuat", "and", "bracket"]),
    ("", []),
])
def test_analyze(text: str, tokens: list[str]) -> None:
    assert analyze(text) == tokens


@pytest.mark.parametrize(("text", "same"), [("日本語", "日本語"), ("ΩΜΈΓΑ", "ωμεγα"), ("A" * 65, "a" * 65)])
def test_unfoldable_or_overlong_text_becomes_one_opaque_token(text: str, same: str) -> None:
    [token] = analyze(text)
    assert is_opaque(token) and analyze(same) == [token]


def test_opaque_tokens_are_never_stemmed() -> None:
    lookalike = "zz" + "a" * 14 + "ed"  # shaped like an opaque token; Snowball would strip "ed"
    assert _stem(lookalike) != lookalike
    assert analyze(lookalike) == [lookalike]
    assert analyze("日本語 tests") == [analyze("日本語")[0], "test"]


def test_identifier_with_opaque_part_keeps_parts_and_whole() -> None:
    tokens = analyze("日本_loader")
    assert tokens[1] == "loader" and is_opaque(tokens[0]) and is_opaque(tokens[2])
    assert variant("日本語").needle is None  # opaque tokens never drive substring expansion


def test_token_alphabet_on_fuzzed_text() -> None:
    rng = random.Random(7)
    alphabet = "aZ09_-./: \t\n'\"!?é߀日本​́ﬁΩ😀İı"
    token = re.compile(r"^[a-z0-9]{1,64}$")
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 80)))
        assert all(token.match(t) for t in analyze(text)), text


@pytest.mark.parametrize(("query", "doc", "hit"), [
    ("file_loader", "the FileLoader class", True),
    ("FileLoader", "file loader", True),  # parts match even without the whole token
    ("ALTNAME_INVALID", "ERR_TLS_CERT_ALTNAME_INVALID", True),
    ("stripe webhook", "Stripe webhooks retry", True),  # every chunk must match
    ("stripe webhook", "Stripe retries", False),
    ("retrying", "retries", True),
    ("café", "CAFE", True),
])
def test_variant_matching(query: str, doc: str, hit: bool) -> None:
    assert variant(query).matches(set(analyze(doc))) is hit


@pytest.mark.parametrize(("raw", "code_shaped", "needle"), [
    ("ALTNAME_INVALID", True, "altnameinvalid"),
    ("FileLoader", True, "fileloader"),
    ("altname", False, "altname"),
    ("http2", True, "http2"),
    ("stripe webhook", False, None),
])
def test_variant_shape(raw: str, code_shaped: bool, needle: str | None) -> None:
    v = variant(raw)
    assert (v.code_shaped, v.needle) == (code_shaped, needle)
