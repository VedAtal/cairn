---
name: cairn-wrapup
description: Distil the current session into Cairn knowledge entries. Use only when the user asks to wrap up, save what was learned, or record the session in Cairn.
---

# Cairn wrap-up

Distil this session into Cairn entries: everything that passes the write bar, and nothing else. Do not write a
session summary.

1. List candidates: surprises, failures whose cause was not obvious, decisions and the alternatives rejected,
   constraints discovered, naming traps.
2. Keep only what passes the write bar. Each must be:
   - transferable: it helps a different task, not only this one;
   - non-recoverable: it is not in the code, the git history or the ticket;
   - durable: it will still be true in six months.
   If none qualify, say "nothing met the bar" and stop.
3. One fact per entry. The title states the claim; the body says why it holds and what to do in one to three
   sentences, at most 1,500 characters. More than that is a second fact: give it its own entry. Write invariants,
   not current values; if a value is the point, mark it "as of YYYY-MM".
4. Search Cairn for each one's concepts. If an entry already covers it, `update` or `verify` that entry instead of
   adding a new one. When a fact changed, update every entry that states it.
5. Pick placement with `map` (store, project, node). When no node fits, create one, named for its area with a
   summary of what belongs in it; each substantial part of a large project gets its own node. Ask the user only
   when the store or project is unclear. Add to a node injected at session start only when the user asks.
6. Show the plan as one short table (op, placement, title), then write.
7. Report one line per write: op, ref, title.

Reject: "X broke on Tuesday", a status, a PR review finding, one file's layout, a walkthrough of what you did, an
"Also" or "Related" tail on an entry.
Keep: a flow that spans several services, why a design is the way it is, a recurring failure with a non-obvious
cause, a vocabulary trap.

Never cite a Cairn ref in code or docs: state the fact there. Refs belong in replies and reports.
