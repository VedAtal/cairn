"""Search over a real store, for behavior the parity corpus doesn't pin down."""

from __future__ import annotations

import pytest

from tests.conftest import Org


@pytest.mark.parametrize(("query", "title"), [("apns", "APNs tokens rotate after a restore"),
                                              ("ios", "iOS widgets refresh every 15 minutes")])
async def test_lowercase_query_finds_mixed_case_word(org: Org, query: str, title: str) -> None:
    ref = await org.w.add(org.writer, org.node, title)
    assert [h.ref for h in await org.w.search(org.reader, query)] == [ref]
