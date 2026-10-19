from __future__ import annotations

import pytest

from cairn.core.analyzer import analyze, expansion
from cairn.core.search import Concept, parse_concepts, rank_one

Features = tuple[int, int, int, int, list[str]] | None


def concepts(*raw: str, generic: tuple[str, ...] = ()) -> list[Concept]:
    out = parse_concepts(list(raw))
    for c in out:
        c.generic = {v.raw for v in c.variants if v.raw in generic}
    return out


def rank(q: list[Concept], title: str = "", body: str = "", node: str = "", summary: str = "") -> Features:
    return rank_one(q, set(analyze(title)), set(analyze(body)), set(analyze(node)), set(analyze(summary)))


def key(f: Features) -> tuple[int, ...]:
    assert f is not None
    c, e, s, v, _ = f
    return (-c, -e, -s, -v)


def test_coverage_beats_synonym_stuffing() -> None:
    q = concepts("stripe|webhook|payment|invoice", "retry|backoff")
    stuffed = rank(q, title="Stripe webhook payment invoice handling")
    covering = rank(q, title="Webhook handlers", body="they retry")
    assert stuffed and covering and stuffed[3] > covering[3]  # more variants hit...
    assert key(covering) < key(stuffed)  # ...but fewer concepts covered


@pytest.mark.parametrize(("where", "generic", "score"), [
    ("title", False, 800), ("title", True, 400),
    ("body", False, 200), ("body", True, 100),
])
def test_field_weight_and_generic_credit(where: str, generic: bool, score: int) -> None:
    q = concepts("cache", generic=("cache",) if generic else ())
    f = rank(q, **{where: "cache"})
    assert f is not None and f[2] == score


def test_substring_expansion_credit() -> None:
    q = concepts("consumergroup")
    q[0].variants.append(expansion("kafkaconsumergroup"))
    f = rank(q, title="kafkaConsumerGroup stalls")
    assert f is not None and f[2] == 40 * 7 * 2 and f[4] == ["kafkaconsumergroup (title/keywords)"]


def test_node_fields_add_coverage_but_not_own_coverage() -> None:
    q = concepts("billing", "retry")
    f = rank(q, title="Retries are capped", node="Billing", summary="billing service")
    assert f is not None
    c, e, s, _, matched = f
    assert (c, e, s) == (2, 1, 800 + 600) and matched == ["billing (node)", "retry (title/keywords)"]


def test_best_field_counts_once_per_concept() -> None:
    q = concepts("cache|caching")
    f = rank(q, title="cache", body="caching cache", node="cache")
    assert f is not None and f[:4] == (1, 1, 800, 2)


def test_matched_shows_the_agents_spelling_not_stems() -> None:
    f = rank(concepts("Retrying"), body="retries happen")
    assert f is not None and f[4] == ["Retrying (body)"]


@pytest.mark.parametrize(("generic", "title", "node", "kept"), [
    ((), "", "retry billing", False),  # no own coverage at all
    (("retry",), "retry", "", False),  # own coverage only generic, and not every concept covered
    (("retry",), "retry", "billing", True),  # only generic, but every concept covered
    ((), "retry", "", True),  # one specific own match is enough
])
def test_own_coverage_rule(generic: tuple[str, ...], title: str, node: str, kept: bool) -> None:
    f = rank(concepts("retry", "billing", generic=generic), title=title, node=node)
    assert (f is not None) is kept
