"""The only text analysis in Cairn. Both databases index this module's output, so they match identically.

Tokens are `[a-z0-9]{1,64}`. Code identifiers are split into parts and also kept whole: `FileLoader` and
`file_loader` both yield `file`, `loader`, `fileloader`. Alphabetic parts are Snowball-stemmed; whole identifiers
aren't. Anything that can't be folded to ASCII becomes an opaque `zz<hash>` token.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from snowballstemmer.english_stemmer import EnglishStemmer  # direct import: the package swaps in PyStemmer if present

ANALYZER_VERSION = "1"

_CHUNK = re.compile(r"[^\W_]+(?:[_\-./:]+[^\W_]+)*")  # words joined by identifier connectors
_CONNECTOR = re.compile(r"[_\-./:]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_CODE_SHAPED = re.compile(r"[_\-./:]|[a-z][A-Z]|\d")
_stem = EnglishStemmer().stemWord


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text).casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def _token(folded: str) -> str:
    if folded.isascii() and folded.isalnum() and len(folded) <= 64:
        return folded
    return "zz" + hashlib.blake2b(folded.encode(), digest_size=8).hexdigest()


_HEX = frozenset("0123456789abcdef")


def is_opaque(token: str) -> bool:
    return len(token) == 18 and token.startswith("zz") and set(token[2:]) <= _HEX


def _term(part: str) -> str:
    token = _token(part)
    return _stem(token) if token.isalpha() and not is_opaque(token) else token


def _chunks(text: str) -> list[list[str]]:
    """Identifier-ish runs of `text`, each split into folded parts."""
    out = []
    for match in _CHUNK.finditer(unicodedata.normalize("NFKC", text)):
        parts = [p for seg in _CONNECTOR.split(match.group()) for p in _CAMEL.split(seg) if p]
        out.append([_fold(p) for p in parts])
    return out


def analyze(text: str) -> list[str]:
    """Document tokens, in order: stemmed parts, then the whole identifier when there was more than one part."""
    tokens: list[str] = []
    for parts in _chunks(text):
        tokens.extend(_term(p) for p in parts)
        if len(parts) > 1:
            tokens.append(_token("".join(parts)))
    return tokens


def analyzed_text(*texts: str) -> str:
    return " ".join(t for text in texts for t in analyze(text))


# A query variant is an AND of chunks; a chunk matches through any one of its alternatives (each an AND of tokens).
Alternative = frozenset[str]
Chunk = tuple[Alternative, ...]


@dataclass(frozen=True)
class Variant:
    raw: str  # the agent's own spelling, echoed back in results
    chunks: tuple[Chunk, ...]
    credit: int = 10  # tenths: exact 10, substring expansion 7 (generic halving is applied at rank time)

    @property
    def tokens(self) -> set[str]:
        return {t for chunk in self.chunks for alt in chunk for t in alt}

    def matches(self, tokens: set[str]) -> bool:
        return bool(self.chunks) and all(any(alt <= tokens for alt in chunk) for chunk in self.chunks)

    @property
    def code_shaped(self) -> bool:
        return len(self.chunks) == 1 and bool(_CODE_SHAPED.search(self.raw))

    @property
    def needle(self) -> str | None:
        """The unstemmed whole token used for substring expansion, if this variant is a single identifier."""
        chunks = _chunks(self.raw)
        if len(chunks) != 1:
            return None
        token = _token("".join(chunks[0]))
        return None if is_opaque(token) else token


def variant(raw: str) -> Variant:
    chunks: list[Chunk] = []
    for parts in _chunks(raw):
        alternatives = [frozenset(_term(p) for p in parts)]
        # The unstemmed whole word also matches identifiers split by case: query 'apns' finds 'APNs' (ap+ns+apns).
        whole = _token("".join(parts))
        if len(parts) > 1 or whole not in alternatives[0]:
            alternatives.insert(0, frozenset({whole}))
        chunks.append(tuple(alternatives))
    return Variant(raw=raw, chunks=tuple(chunks))


def expansion(token: str) -> Variant:
    return Variant(raw=token, chunks=((frozenset({token}),),), credit=7)
