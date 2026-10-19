from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from snowballstemmer.english_stemmer import EnglishStemmer  # avoids the package's PyStemmer swap-in

ANALYZER_VERSION = "1"

_CHUNK = re.compile(r"[^\W_]+(?:[_\-./:]+[^\W_]+)*")
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
    out = []
    for match in _CHUNK.finditer(unicodedata.normalize("NFKC", text)):
        parts = [p for seg in _CONNECTOR.split(match.group()) for p in _CAMEL.split(seg) if p]
        out.append([_fold(p) for p in parts])
    return out


def analyze(text: str) -> list[str]:
    tokens: list[str] = []
    for parts in _chunks(text):
        tokens.extend(_term(p) for p in parts)
        if len(parts) > 1:
            tokens.append(_token("".join(parts)))
    return tokens


def analyzed_text(*texts: str) -> str:
    return " ".join(t for text in texts for t in analyze(text))


Alternative = frozenset[str]
Chunk = tuple[Alternative, ...]


@dataclass(frozen=True)
class Variant:
    raw: str
    chunks: tuple[Chunk, ...]
    credit: int = 10

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
        chunks = _chunks(self.raw)
        if len(chunks) != 1:
            return None
        token = _token("".join(chunks[0]))
        return None if is_opaque(token) else token


def variant(raw: str) -> Variant:
    chunks: list[Chunk] = []
    for parts in _chunks(raw):
        alternatives = [frozenset(_term(p) for p in parts)]
        # The unstemmed whole word also matches case-split identifiers: 'apns' finds 'APNs'.
        whole = _token("".join(parts))
        if len(parts) > 1 or whole not in alternatives[0]:
            alternatives.insert(0, frozenset({whole}))
        chunks.append(tuple(alternatives))
    return Variant(raw=raw, chunks=tuple(chunks))


def expansion(token: str) -> Variant:
    return Variant(raw=token, chunks=((frozenset({token}),),), credit=7)
