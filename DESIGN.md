# Cairn — design

Status: as of 2026-10-18, no code yet. All open decisions are resolved. §17 lists a few remaining assumptions for
you to veto.

Cairn stores the tribal knowledge an agent lacks — the "if you know you know" facts, decisions and gotchas — as small
entries, so every new session builds on what earlier sessions and teammates learned. It is v2 of the markdown store
in `~/projects/knowledge_store_blueprint`. It keeps v1's discipline (search returns metadata, never bodies; a strict
write bar; opt-in startup injection) and rebuilds it as a server with a database, so teams can share it.

One codebase, two **opt-in** modes, usable alone or together:

- **Remote**: a team instance, as stateless containers plus managed Postgres.
- **Local**: your personal store, the same server on your machine on SQLite, optionally backed up to git.

Agents see one MCP surface. With both modes on, your local server merges your personal store with your team's.
Everything Cairn does, admin included, can be done through MCP. Specialist reports with sources, prototypes and
benchmarks are listed in §18.

---

## 1. Principles

1. **Context is the scarce resource.** Headlines first, bodies only by ID, a hard cap on every tool result.
2. **The agent is the LLM.** The server makes no LLM calls. The agent expands queries and judges relevance. The
   server indexes, ranks, enforces rules and stores.
3. **Most things don't belong in the store.** v1's write bar (transferable, non-recoverable, durable) is enforced as
   far as a server can, and stated in the tool descriptions.
4. **Same behavior everywhere.** Same tools, same result text, same ranking on Postgres and SQLite, local and remote.
5. **Small footprint.** One Python package, one container image, one database. No Redis, queue service, object
   storage or Node.

---

## 2. Decisions log (settled with you)

| Topic | Decision |
|---|---|
| Name | Cairn (trail markers). Installed from the public GitHub repo `VedAtal/cairn`, not PyPI (package name `cairnkb`, since `cairn` is taken there); the command is `cairn`. |
| Modes | Local and remote are each **opt-in**. Remote-only runs nothing locally except the CLI; local-only has no remote. |
| Remote (hosted) | Stateless app containers, **no persistent volume**, separate **managed Postgres**. Designed for ~1k users, ~100 teams, ≤ 1M entries, p95 search < 100 ms. |
| Local | Same server on your machine, **SQLite**, launches at login. The MCP/LLM interface is identical to remote; the server handles everything. |
| Parity | Retrieval quality is identical on Postgres and SQLite. |
| DB layer | **SQLAlchemy Core** (not the ORM). Every table is typed in Python. |
| Migrations | **Alembic**, applied automatically on startup, install and update, to whichever DB is connected. |
| Git mode | Optional, local only. The server mirrors the store to git after writes. **One-way** (DB → git), one file per entry, never a committed `.db`. Hand edits are out of scope. Restore only into an empty DB. One store per machine. |
| IAM | **One instance admin**, created at first setup, creates all users and teams. **Team roles:** admin, member. **Project roles:** write, read, stored per project; there is no project admin. Team admins see everything in their team and are writers in every project. A member in zero projects sees nothing. Teams never get a default or team-wide project. |
| Projects | Purely an access boundary. **Not tied to repos**: no repo↔project mapping, no scoping by cwd. |
| Injection | Single entries or whole nodes can be flagged. **Global**: every session gets everything flagged that the user can read, within a harness-safe budget (§8). Only admins set flags. **Members cannot write to injected content.** |
| Lifecycle | Entries are **active** or **archived**. A changed fact is an `update` (new revision, same ref). **Archived = soft delete**: excluded from everything, reversible, and `get` returns a one-line tombstone. |
| Team writes | Go live immediately. No review queue. |
| Placement | The server never defaults store, project or node. The agent asks you unless placement is obvious. |
| Auth | Agents use **personal API keys** (bearer), which carry their owner's full powers, admin included. People use email + password; accounts are created by the instance admin. No SSO, no service accounts. MCP OAuth is deferred. |
| Admin via MCP | Every admin action is available to agents through an `admin` tool. The agent must ask you first, and the harness always prompts for it. |
| Harness permissions | `search`, `get`, `map` are auto-allowed. `write` **prompts** by default. `admin` always prompts. |
| Harnesses | Claude Code and Codex, through a standards-compliant MCP server. |
| Remotes | At most one remote; v2.0 enforces this. |
| Hierarchy | Nodes nest up to **8** levels. |
| Install | **One command** installs the CLI and configures Claude Code and Codex. `cairn remote add` connects a remote later. Updates via `cairn upgrade`. |
| Mac app | A native Swift menu-bar app on your swift-app-kit, a pure and deletable interface over local and remote. **Last priority**: built after the core works. |

---

## 3. Architecture

```
 Claude Code / Codex ── MCP (streamable HTTP), header from `cairn auth header` ──┐
        │ SessionStart hook: `cairn inject`                                       │
        ▼                                                                         ▼
 LOCAL ONLY / BOTH:                                              REMOTE ONLY:
 ┌─── local Cairn server (127.0.0.1:<port>, launchd/systemd) ──┐  harness → hosted /mcp directly;
 │ /mcp /api/v1 /ui /healthz /readyz                            │  `cairn inject` → hosted /api/v1
 │ Federation ─┬─ SqlStore "me" → ~/.cairn/cairn.db (SQLite)    │  (nothing runs locally)
 │             └─ RemoteStore "<alias>" ─ HTTPS + API key ──────┼──┐
 │ Git mirror worker → ~/.cairn/mirror.git → push               │  │
 └──────────────────────────────────────────────────────────────┘  ▼
                                    hosted Cairn: 2–4 stateless replicas (/mcp /api/v1 /ui)
                                                     │
                                            managed Postgres (HA, PITR)
```

**Core library**, shared by both modes:

- **Analyzer.** The only text processing (§6.1).
- **Search planner and ranker.** Python; the database only answers token-set predicates (§6).
- **Write pipeline** (§7).
- **`Store` protocol** with three implementations: `SqlStore(postgres|sqlite)`, `RemoteStore` (REST client) and
  `Federation` (fan out, then merge).

**Data access:** SQLAlchemy 2 **Core**, no ORM.

- Every table is a typed `Table` definition in one `schema.py`. Alembic generates migrations from that metadata.
- Queries are built with Core expressions. The few search statements specific to one dialect (FTS5 `MATCH`, GIN
  `@@`) are typed `text()` constructs kept beside them.
- Rows map to frozen dataclasses per table.
- Postgres uses psycopg 3. SQLite uses the standard `sqlite3` driver. The installer pins a uv-managed Python, which
  ships SQLite 3.53.1. The server refuses to start below SQLite 3.51.3 (the WAL-reset corruption fix) or without FTS5.

**Surfaces of the one ASGI app:**

| Surface | Purpose |
|---|---|
| `/mcp` | agents |
| `/api/v1` | CLI, hook, federation, Mac app |
| `/ui` | people (server-rendered pages) |
| `/healthz` | liveness, unauthenticated; includes `version` and `api_version` |
| `/readyz` | DB and schema check, unauthenticated |

**Invariants:**

- **Same surface everywhere.** Tool list, schemas and result text are identical in every mode; only the list of
  stores differs.
- **Stateless MCP.** `/mcp` runs with `stateless_http=True, json_response=True` everywhere: no server-to-client
  notifications, no SSE, no per-session server state. Any replica can serve any request.
- **One access check.** Access is enforced in `AccessScope` (§5) as a `WHERE` filter, never by Postgres RLS.
- Placement is never defaulted. Injection is global. Archived means gone from every agent-facing path.

---

## 4. Data model

### 4.1 Identity and refs

Rows have a `bigint` identity plus a `uid` (UUIDv7) used as a cross-check in restore and federation. SQLite uses
`INTEGER PRIMARY KEY AUTOINCREMENT`, so a purged id is never reused.

**Refs** name things in agent and human text:

| Form | Names | Example |
|---|---|---|
| `<store>:e<id>` | entry | `acme:e812` |
| `<store>:n<id>` | node | `me:n41` |
| `<store>:p<id>` | project | `acme:p3` |

- **The store alias names one server, not a team.** A hosted instance sets `CAIRN_STORE_ALIAS`
  (`[a-z][a-z0-9-]{1,31}`) and advertises it in `/api/v1/hello` and in the MCP instructions. Clients use it as is,
  with no override.
- **`me` is reserved** for the local store.
- **Paths work too.** `<store>:<team>/<project>/<slug>/…` is accepted wherever a node or project ref is. It resolves
  through current and former paths, and the canonical ref is echoed back.
- **Slugs** are `[a-z0-9_]{1,48}`, not a Windows reserved name, and unique per parent.

### 4.2 Tables

All tables are defined once in SQLAlchemy Core.

| Table | Key columns | Notes |
|---|---|---|
| `users` | id, email, display_name, password_hash (argon2id), is_instance_admin, active | Exactly one instance admin. Local mode has one row, `local`. |
| `teams` | id, uid, slug, name, archived_at | |
| `team_members` | team_id, user_id, role `admin\|member` | |
| `projects` | id, uid, team_id, slug, name, archived_at, searchable_entries | Access boundary only. |
| `project_members` | project_id, user_id, role `write\|read` | No row means no access (team admins excepted). |
| `nodes` | id, uid, project_id, parent_id, slug, path, depth ≤ 8, title, summary ≤ 400, keywords ≤ 12, rev, inject, archived_at, archived_by, archive_reason, former_paths, an_* | ID is identity; `path` is a derived label. |
| `entries` | id, uid, node_id, project_id (denormalized), type, title ≤ 160, body ≤ 2,000, keywords ≤ 8, rev, inject, origin `agent\|human`, created/updated/verified _by/_at, archived_at, archived_by, archive_reason, is_searchable, an_title, an_kw, an_body | One fact per row. The title states the claim. |
| `entry_revisions` | entry_id, rev, full snapshot, by, at | History, undo, and optimistic locking (`base_revision`). |
| `links` | from_ref, to_ref, reason | Cross-links. A reason is required. Hidden unless the reader can see both ends and both are active. |
| `term_df` | term, project_id, df | Document frequency per project, counting active entries only. |
| `vocab` | term | Insert-only token list for substring expansion. Opaque `zz…` tokens are never inserted. |
| `change_log` | seq, project_id, ref, op, by, at, summary | The feed that replaces v1's git-diff review. Filtered by `AccessScope`. |
| `credentials` | id, user_id, kind `api_key\|web_session\|setup\|password_set`, sha256, token_prefix, name, scopes, expires_at, last_used_at, revoked_at | API keys are prefixed `cairn_pat_`. `scopes` = `{read_only, no_admin, teams\|null, projects\|null}`. |
| `audit_log`, `schedules`, `search_log`, `meta` | | §12. `meta` holds the Alembic revision and `analyzer_version`. |

**Entry types:**

| Type | Holds |
|---|---|
| `general` | the catch-all |
| `gotcha` | what will bite you |
| `decision` | what was chosen, why, what was rejected |
| `reference` | stable lookups |
| `open_question` | known unknowns |
| `practice` | v1's `general/` working practices |

The node summary replaces v1's `intro.md` "What". Keywords exist on both nodes and entries.

**Limits** count Unicode code points after NFC normalization. Tool output caps are in characters; the injection
budget is in UTF-8 bytes.

**Hierarchy:** `parent_id` plus a materialized `path`, depth ≤ 8. No ltree. In a SQLite benchmark (3,000 nodes;
Postgres not benchmarked), a path-range query took 0.03 ms and renaming a 1,254-node subtree took 1.4 ms.

### 4.3 Lifecycle: active, archived, purged

- **Update.** A changed fact (a limit moved, a decision was revised) is an `update`: a new revision of the same
  entry. The ref stays the same, so links, citations and flags keep working. `get` with `history=true` shows older
  revisions.
- **Archive (soft delete).** For entries, nodes, projects and teams; reversible with `unarchive`. An archived item
  and everything beneath it is **excluded from everything**: search, injection, dedup, `map`, counts, links and
  outlines.
  - `get` on an archived ref returns a one-line tombstone and no body: `acme:e812 — archived 2026-10-20 by vedatal`.
  - Writes into an archived subtree return 409 `archived`.
  - The optional reason appears only in the change feed and the UI.
  - When a different claim replaces an entry, archive the old one and add a new one. The agent should prefer
    `update`.
- **Searchable** means `archived_at IS NULL` on the entry and on every ancestor node, its project and its team.
- **Purge (hard delete)** is for leaked secrets and legal requests: `admin purge_entry`.
  - Removes the row, its revisions and its links, and updates `term_df`.
  - Writes `audit_log` and `change_log` rows.
  - The git mirror deletes the file in its next commit; history rewrite is `cairn git purge` (§11).
  - Hosted data remains in the provider's backups until their retention expires.

---

## 5. Permissions

| | Instance admin | Team admin | Project writer | Project reader |
|---|---|---|---|---|
| Create, disable and reset users; create, archive and unarchive teams; appoint team admins | yes | — | — | — |
| See, search, get, map, receive injection | every team | their team | their projects | their projects |
| `add`, `update`, `archive`, `unarchive`, `verify`, `move`, node ops | everywhere | their team | their projects, **except injected content** | 403 `read_only` |
| Membership, projects, cross-project moves, purge, set and clear inject flags | everywhere | their team | — | — |

**Injected content is admin-only.** A flagged entry, and everything in a flagged node's subtree, can be written,
archived or moved only by admins. Members get 403 `inject_locked`. Locking the whole subtree stops a member adding a
child node whose entries would then be injected.

**Instance admin.** Created at first setup (§12). It can see all content, because it can appoint itself team admin
anyway.

**`AccessScope(request)`** is the role-derived scope intersected with the API key's `scopes`.

- Computed fresh per request.
- Used by search, get, map, the change feed and injection alike.
- A team limit on a key is dynamic, so it covers projects created later.

**API keys carry their owner's full powers, admin included,** so Cairn is fully operable through MCP. A key can be
narrowed: `read_only`, `no_admin`, or limited to specific teams or projects.

**Admin actions need your confirmation.** They are only reachable through the `admin` tool (§9). `cairn setup` never
pre-approves that tool, so the harness prompts you on every call, and the tool description tells the agent to ask
first. This is the main safeguard against a prompt-injected agent with an admin's key: such an agent could otherwise
flag text into everyone's sessions.

**Enforcement:**

- Every query filters `project_id = ANY(:readable)` in its `WHERE`. RLS is not used, because under RLS Postgres can't
  use the GIN index for `@@`.
- An import-linter contract forbids bypassing `AccessScope`.
- An end-to-end access-matrix suite covers every REST endpoint, MCP tool and federated result on both engines.

**Unauthorized looks like absent.** Unreadable items return 404. `map` lists only readable projects.

---

## 6. Search and retrieval

### 6.1 Analysis: Python is the only analyzer

Both databases index the same pre-analyzed tokens, so they return the same matches by construction.

- **Normalization:** NFKC → casefold → strip diacritics.
- **Tokens:** `[a-z0-9]`, length 1–64. The analyzer enforces the length; a `CHECK` on both backends enforces the
  alphabet. Other non-ASCII runs become opaque hashed tokens.
- **Code identifiers are split and kept whole.** `FileLoader` and `file_loader` become `file`, `loader`,
  `fileloader`. `ERR_TLS_CERT_ALTNAME_INVALID` becomes its parts plus the whole.
- **Stemming:** Snowball English, pinned (`snowballstemmer==3.1.1`, importing `EnglishStemmer` directly). Unicode
  data is pinned too (`unicodedata2`). Stems are never shown to agents or fed back as query text.
- **No stopword list.** Common terms are handled by document frequency (§6.2).
- **Analyzer versions.** The active version is one `meta.analyzer_version` row, changed only by an Alembic migration:
  1. add the new `an_*` columns and indexes;
  2. backfill them in batches;
  3. switch `meta`;
  4. drop the old columns.

  Replicas never re-analyze toward their own version. Locally, re-analysis runs at startup, before serving.

**Indexes:**

- **Postgres:** expression GIN indexes over `array_to_tsvector(string_to_array(...))`, one per field group (head =
  title + keywords; all = title + keywords + body), partial `WHERE is_searchable`.
- **SQLite:** FTS5 `unicode61` with `remove_diacritics 0` and `detail=column`, over the same columns.
- No positions in either index; phrase adjacency is checked in Python.

### 6.2 Query: concept coverage

The agent sends 1–8 **concepts**, each one idea written as variants, e.g.
`["stripe|webhook", "retry|backoff|idempotency"]`. Results rank by **how many distinct concepts an entry matches**.

1. **Plan (Python).** Analyze the variants and look up `term_df` over the readable projects.
   - A variant is **generic** (half credit) when its df is above `G = min(5000, max(50, ⌈0.25·N_readable⌉))`.
     Otherwise it is **driving**.
   - A 30k-postings budget demotes the most common terms to "scored in Python only". This caps heap visits, but GIN
     posting-list reads still scale with instance-wide df until benchmarked (§6.6).
   - If nothing is driving, the planner switches to AND mode and tells the agent to add more specific terms.
2. **Candidates (SQL, one shape on both engines).** One GIN or FTS5 scan per (concept, field group), with the access,
   `types` and `within` filters in the `WHERE`. A CTE aggregates these to per-entry coverage, with `LIMIT 200`. Node
   matches add coverage to their entries.
3. **Rank (Python).** Each candidate gets four features:
   - `C`: concepts covered;
   - `E`: concepts covered by the entry itself;
   - `S`: best field weight × credit per concept;
   - `V`: distinct variants hit.

   Sort by `(-C, -E, -S, -V, -depth, -id)`.

   | Field | Weight |
   |---|---|
   | Entry title, entry keywords | 40 |
   | Node title, node keywords | 30 |
   | Node path, node summary | 20 |
   | Entry body | 10 |

   Credit per match: exact 1.0, substring expansion 0.7, generic 0.5.
4. **Group.** Up to 12 hits across up to 6 nodes, plus up to 3 node-only matches marked "read outline".

### 6.3 Substring matching

This reproduces v1's `grep -i`: `altname` finds `err_tls_cert_altname_invalid`.

- It applies to code-shaped or zero-hit variants of at least 4 characters.
- The variant is expanded against `vocab` (`term LIKE '%needle%'`), restricted to terms with df > 0 in readable
  projects. The readable join comes first, so no readable term is dropped.
- The top 16 expansions join the query at 0.7 credit.
- **pg_trgm** indexes this lookup on Postgres and is required (§12). On SQLite no extension is needed.
- No trigram index on entry text, and no prefix operator (`*`) in v1.

### 6.4 Result shape (MCP text)

```
acme · platform/billing · Billing service (acme:n42) — Stripe integration: invoices, webhooks, retries.
  acme:e812  gotcha    Stripe webhooks retry for up to 3 days, so handlers must be idempotent
             matched: stripe|webhook (title), retry|backoff (body) · verified 2026-10-12
  acme:e790  decision  Idempotency keys are stored per event id, not per request
me · personal · Payments notes (me:n7) — …
Next: get(refs=[…]) for the entries you need.
```

- **Matched terms** show the agent's own variants plus the field they matched in, never stems.
- **Snippets** appear only when no matched term is in the title.
- **Unreachable stores** are named at the end.

### 6.5 Federation (both modes on)

- The local server queries its own store and the remote in parallel. The remote has a 1.5 s budget and a circuit
  breaker.
- The remote returns its top 50 entry hits and top 3 node-only matches. Each comes with node title and summary, the
  `(C, E, S, V, depth)` features and a `ranking_version`.
- Merging uses `(-C, -E, -S, -V, -depth, store_order, -id)`, with `me` first. Reciprocal rank fusion is used only if
  ranking versions differ.

### 6.6 Proving parity and performance

- **Oracle.** A pure-Python reference implementation defines the correct result, and each backend must match it
  exactly, including the `types` and `within` filters.
  - SQLite matched 600/600 random concept queries over 50k entries.
  - The analyzer's tokens matched FTS5's on 20k fuzzed strings.
- **The Postgres half has not run yet.** The first implementation task is Postgres 16–18 in CI running the oracle
  suite, plus a 1M-entry synthetic benchmark with plan assertions. If GIN cost scales with instance-wide df, the
  fallback is project-salted lexemes, which give the same results.
- **Retrieval eval.** Golden queries with expected refs, measuring recall@k and tokens per answer, plus the
  search-call rate per harness. Runs in CI on both backends.
- **Measured:** SQLite p95 ≤ 80 ms at 50k entries. **Estimated:** Postgres p95 < 100 ms at 1M entries.

---

## 7. Write path

Steps 2–6 run in one transaction (`BEGIN IMMEDIATE` on SQLite).

0. **Quota and idempotency** (remote).
   - Lock the key's row and count its revisions in the window.
   - If `(key, op_id)` was already applied, return the stored result.
   - `RemoteStore` generates an `op_id` per proxied write and reuses it on retry. Results are kept for 24 h.
1. **Normalize:** NFC, LF line endings, strip zero-width and bidi control characters, UTC millisecond timestamps.
2. **Limits:**
   - Entry: title ≤ 160, body ≤ 2,000, keywords ≤ 8.
   - Node: summary ≤ 400, keywords ≤ 12.
   - Keywords that repeat title words are stripped.
3. **Secret scan:** hard reject on token patterns, private keys, high-entropy strings and `cairn_pat_` keys.
4. **Dedup gate.** You don't read anything first; the server checks.
   - The search pipeline runs over the new entry's 12 rarest head tokens. It covers **active** entries in the target
     store that the writer can read, so duplicates in other projects surface too.
   - Excluded: the entry itself on `update`, and any refs in `distinct_from`.
   - Python scores `max(title-word overlap, title character overlap, 0.8 × body containment)`.
   - At ≥ 0.6 the server writes nothing and returns the similar entries. The agent then updates the existing one, or
     resends with `distinct_from`.
   - The check is lexical, so a paraphrase in entirely different words can slip through. That is why the
     instructions also say to search before writing.
5. **Revision and lock checks.**
   - `update` and `update_node` need `base_revision`; a mismatch returns 409.
   - `verify` bumps `verified_at` and `verified_by` without a new revision.
   - Members writing to injected content get 403 `inject_locked`.
6. **Bookkeeping:** analyzed columns, `term_df` deltas in one sorted pass, insert-only `vocab`, a `change_log` row.

**Placement.** `write` must name the node, or the project for a new top-level node.

- If more than one placement is plausible, the agent proposes one and asks you.
- It proceeds alone only when placement is obvious: you named it, or it is amending what it just read.

**Staleness.** `verified_at` is separate from `updated_at`, and results show the verified date. The code and the user
always win over a note.

---

## 8. Startup injection

**Mechanism.** A SessionStart command hook runs `cairn inject` on `startup|resume|clear|compact|fork`, on both
harnesses.

- The `cairn` entry module imports only the standard library before dispatching `inject` and `auth header`, so it
  starts in about 20 ms. An import test enforces this.
- **Local only or both:** it calls the local server's `POST /api/v1/inject`. If the server is down, it starts it
  through the service and waits up to 3 s.
- **Remote only:** it calls the remote directly with the stored API key.
- On failure it prints empty context plus a `systemMessage` for you. It always exits 0.

**What gets injected:**

- Only active entries in non-archived subtrees.
- A flagged node contributes its summary plus every active entry in its subtree, depth-first.
- Individually flagged entries are added after the nodes. An entry covered by both appears once.
- Each entry renders as `type ref (verified date): title — body`.

**Budget:** at most **8,000 bytes** of `additionalContext`. That stays under Claude Code's 10,000-character hook cap
(above it, Claude Code silently swaps in a 2,000-character preview) and Codex's default cap of about 2,500 tokens.

- The participants are `me` and each team the user can read on the remote. Each gets an equal share, unused bytes are
  redistributed, and the local server tells the remote its share.
- On overflow, bodies are trimmed first. Then the lowest-priority items move to a "Not shown — read with get" ref
  list.
- Priority order: practice, gotcha, decision, reference, general, open_question, then the most recently verified.
- Admins see their team's flagged size against the share.

**Caching:**

- The ETag is computed after access filtering, so a cached copy can't hide a revocation.
- Remote payloads are cached in `~/.cairn/cache` (0600, at most 7 days). The cache is used on timeout, network failure
  or a 5xx response, and deleted on 401/403.
- On `resume`, a hash keyed by `sha256(session_id)` suppresses context that hasn't changed.

**Framing.** The block is headed as notes, not instructions, with control tags neutralized, plus a one-line "search
Cairn before guessing" reminder.

**Flags** are set by admins: through `admin flag` (confirmed by you) or the UI.

---

## 9. MCP and API surface

**Five tools**, identical in every mode:

| Tool | Harness default | Purpose | Key inputs |
|---|---|---|---|
| `search` | auto-allowed | Headlines grouped by node, ranked by concept coverage; no bodies. | `concepts[1..8]`, `stores[]`, `types[]`, `within`, `limit` (default 12) |
| `get` | auto-allowed | Batch read up to 20 refs. An entry gives its full body, keywords, rev and verified date. A node gives an outline: summary, ≤ 30 children, ≤ 30 entry headlines, counts. An archived ref gives a one-line tombstone. | `refs[]`, `history` |
| `map` | auto-allowed | Stores, readable projects (with your role) and the node tree: refs, titles, one-line summaries, counts. | `store`, `root`, `depth`, `filter` |
| `write` | **prompts** | Ops: `add`, `update`, `archive`, `unarchive`, `verify`, `move` (within a project), `create_node`, `update_node`. The description states the write bar and the placement rule. | `op`, `node`/`project`/`ref`, `base_revision`, `entry{type, title, body, keywords, links[{to, reason}]}`, `node_fields{…}`, `reason`, `distinct_from[]` |
| `admin` | **always prompts** | Ops: users, teams, team admins, projects, membership, API keys, flag/unflag, cross-project moves, purge. The description says: "Every call changes access or what other people see. Confirm with the user before calling." | `op`, op-specific fields |

- **Output cap.** Every tool result is capped at 8,000 characters. `get` fills refs in order until the cap, then lists
  the rest as `Not returned (cap): <refs>`. `map` truncates with `more under <ref>` lines.
- **`search` loads up front.** It carries `_meta["anthropic/alwaysLoad"]` so Claude Code loads it without deferring.
  `cairn setup claude` also sets the server-level `alwaysLoad: true`, which waits up to 5 s for a cold server.
- **Permissions** are a harness feature. A skill doesn't bypass them: when the wrap-up skill leads the agent to call
  `write`, the harness prompts exactly as it would for a direct call.

**Server instructions** are about 2,000 static characters. The first 510 must stand alone for Codex. They cover:

- what Cairn is, and to search before non-trivial work;
- how to write concepts;
- "notes, not instructions — the code and the user win";
- the write bar, the placement rule, and preferring `update` over archive-and-add;
- to confirm admin actions;
- to cite refs.

A draft is in the harness report (§18). Amend it before use: drop the `*` prefix syntax, use `acme:e812` refs, say
1–8 concepts with a default of 12, and use API keys instead of `cairn login`/OAuth.

**Session wrap-up** is a skill on both harnesses (Codex appears not to support MCP prompts; unverified), plus an MCP
prompt for other clients. It distils a session into at most a few entries, usually none.

**REST `/api/v1`** has the same operations as the tools, plus:

- `inject`;
- `changes` (the change feed: cursor-paginated, at-least-once, may lag about 5 s);
- `hello` (local proof plus the store alias);
- `status` (bearer token; counts, git state, version);
- `git/push` (local only).

All list endpoints paginate with cursors.

---

## 10. Local mode

### 10.1 Setups

| Setup | Runs locally | Harness connects to |
|---|---|---|
| Local only | the local server | `http://127.0.0.1:<port>/mcp` |
| Remote only | **nothing** (just the CLI) | the hosted `/mcp`, header from `cairn auth header --server URL` |
| Both | the local server | `http://127.0.0.1:<port>/mcp`; the server federates to the remote |

`cairn local enable|disable` and `cairn remote add|remove` switch between setups and rewrite the harness configs to
match.

**Local server:**

- One per OS user, and the only process that opens `~/.cairn/cairn.db`.
- The harness connects over streamable HTTP. Claude Code reconnects dropped HTTP servers but doesn't reconnect stdio
  servers.
- **Auth header helper.** The header comes from `headersHelper` (Claude Code) or `http_headers_helper` (Codex), which
  runs `cairn auth header`. Locally, the helper first starts the server if needed and verifies it with the
  hello-proof (§10.3).

**`cairn local enable`** (or the installer's local option):

- creates user `local`, team `me` with you as admin, project `personal` (`me:p1`), the token and the config;
- runs the migrations;
- installs and starts the login service.

You are instance admin and team admin of your local store. More projects come from `cairn project add`, the UI, or
`admin`.

### 10.2 Lifecycle

**The `cairn` CLI owns the service.**

- **macOS:** `~/Library/LaunchAgents/local.cairn.server.plist`, bootstrapped into `gui/$UID`, with:
  - the absolute `cairn` path and an explicit PATH;
  - `RunAtLoad` and `KeepAlive {SuccessfulExit: false}` with the default throttle;
  - logs in `~/.cairn/logs`, rotated at start.

  It does not use `SMAppService.agent` (that needs a Developer ID) or `AssociatedBundleIdentifiers` (that needs a
  Team ID).
- **Linux:** a systemd user unit with `Restart=on-failure`, `RestartSec=5`, `StartLimitIntervalSec=300` and
  `StartLimitBurst=5`, plus optional `--linger`.

**Exit codes:**

- Exit 0 is never restarted: clean shutdown, lost the lock race, port in use, or a schema newer than the code. The
  reason goes to `run/last_error`.
- Non-zero means a crash, and the service manager restarts the server.

**Starting.** The helper, hook and CLI start the server through the service manager when it is installed
(`launchctl kickstart -k …`, `systemctl --user restart cairn`). They spawn a detached server themselves only when no
service is installed.

- A kernel lock (`run/server.lock`) guarantees a single server.
- `run/server.json` records pid, port, version and `managed_by`.

**Codex.** Codex re-runs the header helper only at connection or after a 401/403, and gives optional servers about
1 s at startup. So for Codex, the login service is what keeps Cairn up. `cairn setup codex` offers `required = true`,
and `cairn doctor` warns when the service is missing or blocked.

**Migrations run at startup** (Alembic). The DB is backed up first with SQLite's backup API.

**Stopping.** `cairn service stop` boots out the service and writes a `run/stopped` marker. On-demand starts respect
the marker until `cairn service start`.

**`cairn service {install, uninstall, start, stop, restart, status, logs} --json`**

- Status fields: `installed`, `loaded`, `running`, `managed_by`, `pid`, `port`, `version`, `api_version`,
  `login_item` (`enabled|blocked|unknown`), `last_exit_code`, `last_error`, and `git` (`enabled`, `last_push_at`,
  `state`).
- Exit codes: 0 ok, 2 not installed, 3 blocked.

### 10.3 Security

Browsers can reach localhost, so the local server defends against web pages:

- **Bind and port.** Binds only to `127.0.0.1`, on a fixed per-user port chosen at setup. If the port is taken, the
  server exits 0 with `port_in_use`, and `cairn doctor` offers `--repick-port`.
- **Header checks.** Exact `Host` check (421 on mismatch) and `Origin` check (403). No CORS headers, and no state
  change on GET.
- **Token.** A bearer token in `~/.cairn/run/token` (0600, 256-bit). The MCP SDK's default localhost protection allows
  any port, so the server replaces it with settings for its exact port.
- **Hello-proof.** Before sending the token, a client verifies an HMAC challenge, which defeats port squatting.
- **Web UI.** `cairn ui` opens the UI with a single-use ticket valid for 60 s, exchanged for an HttpOnly,
  SameSite=Lax cookie (every state change is a POST with a CSRF token; Strict would log people out when they
  follow a link from chat or email).

### 10.4 Remote connection

`cairn remote add https://cairn.acme.dev`:

- asks for your API key (created in the hosted web UI);
- tests it;
- stores it in `~/.cairn/credentials.toml` (0600);
- rewires the harness configs for the new setup.

v2.0 refuses a second remote. The file is preferred over the Keychain: the Keychain ties an item to the program that
created it, so a uv Python upgrade could re-prompt (unverified).

---

## 11. Git mode (local only)

**Where:** a one-way mirror into the bare repo `~/.cairn/mirror.git` (branch `main`), optionally pushed to a private
remote. `cairn git enable [--remote URL]` turns it on; `cairn git status`, `push` and `disable` manage it.

**Format:**

- `cairn.json` (the format version), `<team>/_team.md`, `<team>/<project>/_project.md`,
  `<team>/<project>/<node path>/_node.md` and `.../<type>-e<id>.md`.
- Front matter is one `key: <json value>` per line, in a fixed key order: ids, slugs, titles, keywords, `rev`,
  `inject`, timestamps, `archive_reason` and outgoing `links`. A node's summary and an entry's body follow it.
- No `*_by` columns (a local store has one user) and no IAM data. Git history stands in for revisions and the
  change log.
- Archived items stay with their `archived_*` fields. Moves are renames, and a type change renames the file.
- Unchanged data renders byte-for-byte the same.

**Mechanics:**

1. Every write and admin op marks the mirror dirty. The worker waits for 2 s of quiet, then renders the whole store.
2. If the tree's digest differs from HEAD's `Cairn-Digest` trailer, one `git fast-import` commit replaces the tree.
3. It pushes right after, never forced (no prompts, 120 s timeout), and retries every 5 min while the push is
   pending or the mirror is in error.

- A write never waits on git, and a git failure never takes the server down.
- Secrets never reach the mirror because writes refuse them: entries, nodes, archive reasons, team and project names.
- State (`ok`, `error`, `diverged`), the last commit and push, and whether a push is pending show in
  `cairn git status` and `/api/v1/status`.

**One store per machine.**

- If a push is rejected as non-fast-forward, the mirror stops as `diverged` and merges nothing. `cairn git push`
  retries once.
- A store that never wrote the mirror (a `git_mirror` meta row marks one that has) won't commit over a mirror holding
  other data. If `cairn.db` is lost, the backup isn't wiped by the empty store that replaces it.

**Restore:** `cairn restore [repo]`, or `cairn local enable --from-git URL` on a new machine. With no repo, it uses
the machine's own `mirror.git`.

- It fills an **empty** store only, and refuses if `cairn.db` or its `-wal`/`-shm` files exist. On failure it
  removes what it built.
- Team, project, node and entry ids are kept, so refs stay valid. Links get new ids, and their `created_at` is the
  source item's `updated_at`.
- Only the current revision of each entry comes back, and the change log starts empty.
- A URL source becomes the push remote only if it passes the public-repository check.
- Unit tests check `render(restore(render(DB))) == render(DB)`.

**Remote safety:**

- Public remotes are refused (checked anonymously through the GitHub and GitLab APIs; other hosts need
  `--i-know-its-private`).
- URLs containing credentials are refused.

**Not built yet:**

- `cairn git doctor` and `cairn git purge` (a history rewrite after a leaked secret);
- a hook `systemMessage` on long failure or divergence, and status in the UI and the Mac app;
- `--with-history` (replaying older revisions from the git log);
- gitleaks on outgoing commits;
- a warning when `~/.cairn` sits in a macOS-protected folder.

**Not on remote instances in v2.0.** A git export would bypass project access. Recovery there is the provider's
point-in-time restore plus a per-team export by admins.

---

## 12. Remote (hosted) mode

**Topology:**

- 2–4 stateless replicas of one image (1 vCPU / 512 MiB each, one uvicorn process), plus managed Postgres.
- At 1M entries: 2 vCPU / 8 GB with HA and point-in-time restore, and roughly 6–8.5 GB of data including about 1 GB of
  `term_df`. This is an estimate until the benchmark runs.

**Postgres 16–18** (the versions tested in CI); target 17.

**pg_trgm is required.** The first migration runs `CREATE EXTENSION IF NOT EXISTS pg_trgm` and builds the `vocab`
trigram index.

- Without the index, every substring expansion is a sequential scan over `vocab`. At 1M entries that alone could
  break the p95 target.
- If the database refuses the extension, startup fails loudly: `/readyz` stays false and the log prints the
  provider-specific fix. `cairn doctor --db` prints the same fix.

Provider notes:

- **Cloud SQL and AlloyDB:** need their superuser role.
- **Azure:** add pg_trgm to `azure.extensions` through the portal or CLI first. `cairn doctor --db` prints the
  command.
- **Neon:** needs a role created in its console.
- **Supabase:** installs it into the `extensions` schema.
- **RDS, Aurora, Render, Crunchy:** support it directly.

**Migrations (Alembic) run automatically at startup.**

- Each replica runs `alembic upgrade head` under a Postgres advisory lock, so one replica migrates while the others
  wait, and the rest find nothing to do.
- The lock needs a session connection, so migrations use `CAIRN_MIGRATE_DATABASE_URL` when set (a direct connection,
  owner role), otherwise `DATABASE_URL`.
- Old replicas keep serving during a rolling deploy, so every migration must be backward-compatible: add first,
  remove in a later release.
- `/readyz` stays false until the schema matches the code.

**Driver:** psycopg 3, with server-side prepared statements off and no session state (no session-level `SET`,
LISTEN/NOTIFY or session advisory locks). The app therefore works behind PgBouncer, Neon's pooler or Supavisor in
transaction mode.

**State:**

- Everything durable lives in Postgres, including web sessions as hashed credentials.
- Request-rate buckets are kept in memory per replica, which is approximate by design. Write quotas are exact (§7).
- No server cache of injection payloads.

**Maintenance:** no job queue. A `schedules` table holds a few periodic tasks:

- expire sessions and keys;
- prune logs past retention (audit 400 d, search log 30 d, `change_log` 400 d);
- run the analyzer backfill during a migration.

Each replica runs a small loop and claims due tasks by compare-and-set, so each task runs on one replica.

**Bootstrap and accounts:**

- On first startup with no instance admin, the replica that ran the migrations creates a one-time setup token (hashed
  in the DB) and logs its URL once. The token stays valid until the instance admin exists.
- The instance admin creates teams and users. A new user gets a one-time set-password link (no SMTP needed), and
  password resets work the same way.
- Team admins manage membership in their team.

**Auth:**

- Passwords: argon2id, with login throttling.
- API keys: SHA-256 hashed, shown once, prefixed, expiring, revocable, last use tracked.
- Token checks are cached for 30 s only on `initialize`, `server/discover` and `tools/list`; every `tools/call`
  re-checks.

**Also:**

- An audit log (users, teams, membership, keys, flags, purges, archives), written in the same transaction as the
  change.
- JSON logs.
- OpenTelemetry through `OTEL_*`, off by default.

**Deploy:**

- One image, configured by env vars: `DATABASE_URL`, `CAIRN_MIGRATE_DATABASE_URL`, `CAIRN_SECRET_KEYS`,
  `CAIRN_STORE_ALIAS`, …
- Templates: Render + Neon, Cloud Run + Cloud SQL, and Helm with any external Postgres.
- **Updating** means deploying the new image tag; migrations apply on startup.

---

## 13. Install, setup and updates

**One command:**

```
curl -fsSL https://raw.githubusercontent.com/VedAtal/cairn/main/install.sh | sh -s -- [--local] [--remote URL]
# or, if uv is installed:   uvx --from git+https://github.com/VedAtal/cairn cairn install [--local] [--remote URL]
```

It does five things:

1. Installs uv if missing, then the CLI from GitHub: `uv tool install --managed-python git+https://github.com/VedAtal/cairn`. The uv-managed Python ships a
   fixed SQLite.
2. Without flags, asks whether to set up local, remote or both. Each is opt-in.
3. **Local:** `cairn local enable` (§10.1). **Remote:** `cairn remote add URL` (§10.4).
4. Detects Claude Code and Codex and configures each, as in the table below.
5. Prints what it changed. `--dry-run` shows the diff first, a backup is kept, and `cairn uninstall` reverses it.

| | Claude Code | Codex |
|---|---|---|
| MCP | HTTP server `cairn`, `headersHelper: cairn auth header`, server-level `alwaysLoad: true` | `[mcp_servers.cairn]`: `url`, `http_headers_helper`, `startup_timeout_sec = 20`, optional `required = true` |
| Permissions | allow rules for `search`, `get`, `map` only; `write` prompts (the first prompt offers "always allow"); `admin` is never added | `approval_mode`: `auto` for reads (read-only tools pass), `prompt` for `write` and `admin` |
| Startup injection | SessionStart command hook (exec form) → `cairn inject` | SessionStart hook → `cairn inject`; you approve it once in `/hooks` |
| Pointer | one line in `~/.claude/CLAUDE.md`: search Cairn before non-trivial work | one line in `~/.codex/AGENTS.md` |
| Wrap-up | `cairn-wrapup` skill | wrap-up skill |

All paths are absolute, because IDEs launched from the GUI often lack `~/.local/bin` on their PATH.

**Updating:** `cairn upgrade`.

1. Runs `uv tool upgrade cairnkb --reinstall-package cairnkb`, which fetches the repo's latest commit.
2. Restarts the local service, which migrates on startup.
3. Refreshes the hook, skill and MCP config if their format changed.

The CLI shows "update available" at most once a day. A newer CLI meeting an older running server restarts it through
the service manager. `/healthz`'s `api_version` flags breaking skew between the local and remote servers.

**The biggest product risk is agents not searching**, because both harnesses defer MCP tools. Mitigations:

- `alwaysLoad` on `search`;
- the pointer line;
- the reminder in injected context;
- instructions whose opening stands alone;
- measuring the search-call rate in the eval.

---

## 14. Cairn.app (macOS) — last priority

Not built until the core works. The shape agreed so far:

- **Repo:** `VedAtal/cairn-mac` at `~/projects/cairn-mac`, a sibling of recall and snap. Built from source with
  swift-app-kit's `setup.sh` and the Command Line Tools, following the kit's STANDARDS. Bundle ID `local.cairn`.
- **A pure interface.** Install it before or after Cairn, delete it any time; Cairn's behavior doesn't change.
  - It reads `~/.cairn` config to find the local server and the remote.
  - It shows status for both: the local service, counts, git push age, and remote reachability and version.
  - It calls `cairn service … --json` for control. It never spawns the server or writes plists.
- **Kit change:** add `NSAppTransportSecurity → NSAllowsLocalNetworking = true` to the kit's generated Info.plist.
  Since macOS 14, App Transport Security blocks plain HTTP to IP addresses by default.
- **Prototype first:**
  - a loopback `URLSession` request;
  - `SMAppService.statusForLegacyPlist` on the CLI's plist;
  - the label macOS shows for the service in Login Items;
  - whether quitting the app triggers macOS's background-activity prompt.

---

## 15. Typical flows

**Solo user:** run the one command with `--local`. The store, service and harness configs are set up, and Cairn
starts at login.

**Joining a team:**

1. The instance admin creates your account and sends a set-password link. A team admin adds you to projects.
2. You set a password and create an API key in the web UI.
3. Run `cairn remote add https://cairn.acme.dev`, or install with `--remote` from the start. Team results now appear
   in every search; with local on as well, they merge with your personal store.

**Hosting for a team:**

1. Provision managed Postgres.
2. Deploy the image. Migrations run on startup.
3. Open the setup URL from the logs and create the instance admin.
4. Create teams and users, and appoint team admins.

---

## 16. Phasing

**v2.0:**

- the core library;
- local server and service;
- remote with users, teams, projects and API keys;
- federation to one remote;
- git mode (backup and restore);
- the one-command installer and `cairn upgrade`;
- the MCP tools, including `admin`;
- the web UI: search, browse, edit, archive, flags, users, membership, keys, change feed, purge;
- the v1 importer and markdown/JSONL export.

**After the core:** Cairn.app.

**Later:**

- MCP OAuth for zero-install clients (claude.ai, ChatGPT connectors);
- hosted git export;
- a Windows service;
- a Claude Code plugin;
- embeddings, only if search logs show vocabulary misses;
- multiple remotes;
- multi-machine catch-up.

**Not planned** (you said not needed): SSO and service accounts.

**First implementation tasks:**

1. The Postgres oracle suite and 1M-entry benchmark in CI.
2. A retrieval eval set built from real v1 knowledge.

---

## 17. Remaining assumptions (veto any)

- **A1.** The instance admin can see all content (it can appoint itself team admin anyway).
- **A2.** `cairn local enable` creates one project, `personal`. The no-default-project rule applies to teams.
- **A3.** The remote API key lives in a 0600 file, not the Keychain.
- **A4.** Archive and unarchive of entries and nodes is a `write` op for writers, outside injected content. Archiving
  projects and teams is an `admin` op.
- **A5.** An archived ref's tombstone shows date and actor, not the reason.

---

## 18. Source reports

These are in the session scratchpad, which may be cleaned up, so copy anything worth keeping:
`/private/tmp/claude-501/-Users-vedatal-projects/7260373f-5910-422e-b44a-00d78ba950ef/scratchpad/`

- **`wf/`**: round 1. Three independent designs, research on databases, harnesses and prior art, and a review.
- **`wf2out/`**: round 2.
  - `part-parity-search.md`: analyzer, SQL, oracle, measurements.
  - `part-git-mirror.md`
  - `part-local-topology.md`
  - `part-hosted-scale.md`: provider matrix and pooling.
  - `part-harness-adapters.md`: tool descriptions, instructions draft, setup specs.
  - `critique.md`
- **`macout/`**: the Mac app. Maps of swift-app-kit, recall and snap, plus `design.md` and `review.md`.
- **`verify-findings.txt`**: the 82 review findings from the previous pass.
- **`wf2/retrieval-parity/`**: the runnable analyzer and search prototype.

Superseded by your later decisions, where these reports differ:

- OAuth and refresh tokens → API keys.
- Admin approval of injected revisions → members locked out of injected content.
- `retracted` and `superseded` statuses → active and archived only.
- apsw → SQLAlchemy Core.
- Release-step migrations → Alembic on startup.
- The jobs queue → `schedules`.
- Depth 4 → 8.
- G of 20k → 5k.

---

## 19. Implementation status (2026-10-19)

Built: everything in §3–§10 and §13 except the items below, about 3,700 lines of source. Tests: unit, parity on
SQLite and Postgres (the oracle is the two engines agreeing, plus golden expectations), and integration against the
compose stack (hosted server on Postgres, MCP client, local federation, CLI). `make test-all` runs everything.

**Phase 2 so far:** the web UI with passwords (set-password links, no SMTP), `cairn ui` tickets, links and the
graph view; git mode (§11, with the gaps it lists).

**Still to come:** quotas and idempotency, the audit log and `schedules`, purge, the hello-proof, path refs, the
resume-hash dedup, the postings budget and AND-mode fallback, node-only search matches, and cross-project moves.
Cairn.app is phase 3.

**Deviations from the design:**

- **SQLite** runs on a single pooled connection instead of requiring SQLite ≥ 3.51.3: the local server is the file's
  only user, so access is serialized and the WAL-reset trigger (concurrent writers or checkpoints) can't occur.
- **The analyzer** uses the stdlib `unicodedata` (not a pinned `unicodedata2`), and a variant matches on its token set
  without checking adjacency. A single-word query also carries its unstemmed form, so `apns` finds `APNs`.
- **The local server's Host/Origin checks** live in Cairn's own middleware; the MCP SDK's DNS-rebinding protection is
  turned off in both modes, since hosted auth is the bearer token.
- **Bootstrap** can use a known `CAIRN_SETUP_TOKEN` (for automated deploys and tests) instead of the logged one.
