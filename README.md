# Cairn

Cairn stores the tribal knowledge your coding agents lack (gotchas, decisions, references) as small, searchable
entries, so each new Claude Code or Codex session builds on what earlier sessions and teammates learned. Run it
locally for yourself (SQLite), host it for a team (Postgres), or both; agents see one MCP server that merges the two.
Design: [DESIGN.md](DESIGN.md).

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/VedAtal/cairn/main/install.sh | sh -s -- [--local] [--remote URL]
# or, with uv:  uvx --from git+https://github.com/VedAtal/cairn cairn install [--local] [--remote URL]
```

This installs the `cairn` CLI, sets up a local store and/or a remote, and configures Claude Code and Codex.
Update with `cairn upgrade`; undo with `cairn uninstall` (your data is kept).

## Connect to a team

Get an API key from your instance admin (creating your account returns your first key), then:

```sh
cairn remote add https://cairn.acme.dev   # prompts for the key
```

## Host it

One image plus Postgres 16-18 (with `pg_trgm`). Migrations run at startup.

| Variable | |
|---|---|
| `CAIRN_DB__URL` | Postgres URL (required) |
| `CAIRN_DB__MIGRATE_URL` | direct connection for migrations, if `CAIRN_DB__URL` goes through a pooler |
| `CAIRN_STORE_ALIAS` | the store name in refs, e.g. `acme` |
| `CAIRN_SETUP_TOKEN` | one-time setup token; unset, one is logged at first start |

Then create the instance admin and keep its API key:

```sh
cairn remote bootstrap https://cairn.acme.dev --token <setup token> --email you@acme.dev
```

To try it: `make server` (or `docker compose up server`) runs one on http://localhost:8000 with the setup token
`compose-setup-token`; see `.env.example`.

## Develop

Everything runs in Docker. See [AGENTS.md](AGENTS.md).

```sh
make lint        # ruff + mypy
make test-unit   # unit and parity tests
make test        # integration + Postgres parity tests against the compose stack
make test-all    # both
make server      # hosted instance on :8000;  make logs / make down
```
