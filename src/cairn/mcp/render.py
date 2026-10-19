from __future__ import annotations

import json
from typing import Any

from ..models import GetResponse, MapNode, MapResponse, SearchResponse, WriteResponse

CAP = 8000
MAX_HITS, MAX_GROUPS = 12, 6


def _cap(text: str) -> str:
    return text if len(text) <= CAP else text[: CAP - 40].rsplit("\n", 1)[0] + "\n… (truncated; narrow the request)"


def search(resp: SearchResponse) -> str:
    groups: dict[str, list[Any]] = {}
    for hit in resp.hits:
        if hit.node_ref not in groups and len(groups) >= MAX_GROUPS:
            continue
        groups.setdefault(hit.node_ref, []).append(hit)
        if sum(len(g) for g in groups.values()) >= MAX_HITS:
            break
    lines = []
    for hits in groups.values():
        h = hits[0]
        lines.append(f"{h.store} · {h.team}/{h.project} · {h.node_title} ({h.node_ref}) — {h.node_summary[:160]}")
        for hit in hits:
            lines.append(f"  {hit.ref}  {hit.type:<13} {hit.title}")
            lines.append(f"      matched: {', '.join(hit.matched)} · verified {hit.verified_at}")
    if not lines:
        lines.append("No matches in Cairn. An empty result is a normal answer: reformulate once at most, then proceed.")
    else:
        refs = [h.ref for g in groups.values() for h in g]
        more = ", …" if len(refs) > 5 else ""
        lines.append(f"Next: get(refs=[{', '.join(refs[:5])}{more}]) for the entries you need.")
    if resp.unreachable:
        lines.append(f"(unreachable: {', '.join(resp.unreachable)})")
    return _cap("\n".join(lines))


def get(resp: GetResponse) -> str:
    blocks, used, skipped = [], 0, []
    for item in resp.items:
        if item.status != "ok":
            block = item.tombstone or f"{item.ref} — not found (or not visible to you)"
        elif item.type:
            block = (f"{item.ref} · {item.type} · rev {item.rev} · verified {item.verified_at} · node {item.node_ref}"
                     f" ({item.path}){' · injected' if item.inject else ''}\n{item.title}\n{item.body}")
            if item.keywords:
                block += f"\nkeywords: {', '.join(item.keywords)}"
            for rev in item.history:
                block += f"\n  rev {rev.rev} ({rev.at}): {rev.title} — {rev.body[:200]}"
        else:
            block = f"{item.ref} · {item.title}" + (f" ({item.path}) · rev {item.rev}" if item.path else "")
            if item.summary:
                block += f"\n{item.summary}"
            for child in item.children:
                block += f"\n  child {child.ref}  {child.title} — {child.summary or ''}"
            for e in item.entries:
                block += f"\n  {e.ref}  {e.type:<13} {e.title}"
            extra = item.counts.get("entries", 0) - len(item.entries)
            if extra > 0:
                block += f"\n  … {extra} more entries (search within {item.ref})"
        if used + len(block) > CAP:
            skipped.append(item.ref)
            continue
        blocks.append(block)
        used += len(block) + 2
    text = "\n\n".join(blocks)
    if skipped:
        text += f"\n\nNot returned (cap): {', '.join(skipped)}"
    if resp.unreachable:
        text += f"\n(unreachable: {', '.join(resp.unreachable)})"
    return text


def map_(resp: MapResponse) -> str:
    lines = []

    def walk(node: MapNode, indent: int) -> None:
        lines.append(f"{'  ' * indent}{node.ref}  {node.title} [{node.entries}] — {node.summary}")
        for child in node.children:
            walk(child, indent + 1)

    for store in resp.stores:
        lines.append(f"store {store.store} (you: {store.user})")
        for p in store.projects:
            lines.append(f"  project {p.ref}  {p.team}/{p.name} (role: {p.role})")
            for node in p.nodes:
                walk(node, 2)
        if not store.projects:
            lines.append("  (no projects visible to you)")
    if resp.unreachable:
        lines.append(f"(unreachable: {', '.join(resp.unreachable)})")
    return _cap("\n".join(lines))


def write(resp: WriteResponse) -> str:
    return f"{resp.message}: {resp.ref}" + (f" (rev {resp.rev})" if resp.rev else "")


def error(code: str, message: str, duplicates: list[Any] | None = None) -> str:
    text = f"error ({code}): {message}"
    for d in duplicates or []:
        text += f"\n  {d.ref}  (similarity {d.score})  {d.title}"
    return text


def admin(result: dict[str, Any]) -> str:
    return _cap(json.dumps(result, indent=1, default=str))
