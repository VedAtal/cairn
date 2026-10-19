---
name: cairn-wrapup
description: Distil the current session into at most a few Cairn knowledge entries. Use only when the user asks to wrap up, save what was learned, or record the session in Cairn.
---

# Cairn wrap-up

Distil this session into Cairn entries. Usually nothing qualifies, and that is a good outcome. Do not write a
session summary.

1. List candidates: surprises, failures whose cause was not obvious, decisions and the alternatives rejected,
   constraints discovered, naming traps.
2. Keep only what passes the write bar. Each must be:
   - transferable: it helps a different task, not only this one;
   - non-recoverable: it is not in the code, the git history, the docs or the ticket;
   - durable: it will still be true in six months.
   Expect 0 to 3. If none qualify, say "nothing met the bar" and stop.
3. One fact per entry. The title states the claim; the body says why it holds and how to apply it. Write
   invariants, not current values; if a value is the point, mark it "as of YYYY-MM".
4. Search Cairn for each one's concepts. If an entry already covers it, `update` or `verify` that entry instead of
   adding a new one.
5. Pick placement with `map` (store, project, node). If the right place is not obvious, ask the user.
6. Show the plan as one short table (op, placement, title), then write.
7. Report one line per write: op, ref, title.

Reject: "X broke on Tuesday", a PR review finding, one file's layout, a walkthrough of what you did.
Keep: a flow that spans several services, why a design is the way it is, a recurring failure with a non-obvious
cause, a vocabulary trap.
