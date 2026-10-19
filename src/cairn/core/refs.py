"""Refs name things in agent and human text: `<store>:e<id>` (entry), `n` (node), `p` (project)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_REF = re.compile(r"^([a-z][a-z0-9-]{0,31}):([enp])(\d+)$")

Kind = Literal["e", "n", "p"]


class RefError(ValueError):
    pass


@dataclass(frozen=True)
class Ref:
    store: str
    kind: Kind
    id: int

    def __str__(self) -> str:
        return f"{self.store}:{self.kind}{self.id}"


def parse(ref: str, kinds: str = "enp") -> Ref:
    match = _REF.match(ref.strip())
    if not match or match.group(2) not in kinds:
        expected = "/".join(f"<store>:{k}<id>" for k in kinds)
        raise RefError(f"bad ref {ref!r}: expected {expected}, e.g. me:{kinds[0]}12")
    return Ref(match.group(1), match.group(2), int(match.group(3)))  # type: ignore[arg-type]
