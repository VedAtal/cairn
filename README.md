# Cairn

Cairn stores the tribal knowledge your coding agents lack (gotchas, decisions, references) as small, searchable
entries, so each new Claude Code or Codex session builds on what earlier sessions and teammates learned. Run it
locally for yourself (SQLite), host it for a team (Postgres), or both; agents see one MCP server that merges the two.
Design: [DESIGN.md](DESIGN.md).

## Philosophy

Models and harnesses can now build software better and faster than most engineers, as long as they have the same
context. That condition is the whole gap. Whether an agent works for a team or for one developer, what it lacks isn't
skill. It's the knowledge you pick up through conversations and experience: the "if you know, you know" kind that
never makes it into the code or the docs. So the goal is to give every agent session that context, and to have each
session leave it richer for the next one, so agents build up the same tribal knowledge an engineering team does.

## Features

- **Local:** a personal store on your machine. The same server runs on SQLite, starts at login, listens only on
  127.0.0.1, and can back itself up to a private git repo (`cairn git enable`).
- **Remote:** a team store you host. Stateless containers (`ghcr.io/vedatal/cairn`) on managed Postgres, with
  users, API keys and roles.
- **Federation:** with both set up, agents see one MCP server. Search merges your store and your team's into one
  ranked list, and writes go to whichever store you name.
- **UI:** a web UI for both stores: browse, search, edit, archive, the change feed, admin pages, and a graph of each
  project. `cairn ui` opens it logged in.
- **Inject on startup:** admins flag nodes or entries, and every new agent session starts with them as standing
  instructions. Acts like a team-wide AGENTS.md to enforce practices.
- **Structure:** everything lives in one tree, and access is granted per project:

```
store                  me (local), or your team's server
└── team               roles: admin, member
    └── project        access: write or read
        └── node       a topic; nodes nest up to 8 deep
            ├── node
            │   └── entry
            └── entry  one fact: a gotcha, decision, reference, practice, …
```

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/VedAtal/cairn/main/install.sh | sh -s -- [--local] [--remote URL]
# or, with uv:  uvx --from git+https://github.com/VedAtal/cairn cairn install [--local] [--remote URL]
```

This installs the `cairn` CLI, sets up a local store and/or a remote, and configures Claude Code and Codex.
Update with `cairn upgrade`; undo with `cairn uninstall` (your data is kept).

On a Mac, [Cairn for Mac](https://github.com/VedAtal/cairn-mac) adds a menu bar app that shows both stores and
controls the local server. It's optional: the CLI does everything it does.

## Connect to a team

Get an API key from your instance admin (creating your account returns your first key), then:

```sh
cairn remote add https://cairn.acme.dev   # prompts for the key
```

## Host it

One image plus Postgres 16-18 (with `pg_trgm`). Migrations run at startup. CI publishes the image for amd64 and
arm64: `ghcr.io/vedatal/cairn:latest` tracks `main`, and `vX.Y.Z` tags publish `X.Y.Z` and `X.Y`.

```yaml
services:
  cairn:
    image: ghcr.io/vedatal/cairn:latest
    ports: ["8000:8000"]
    environment:
      CAIRN_DB__URL: postgresql://cairn:secret@db.example.com:5432/cairn
      CAIRN_STORE_ALIAS: acme
```

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

## License

MIT. See [LICENSE](LICENSE).
