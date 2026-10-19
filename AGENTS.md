# Working on Cairn

Cairn is a knowledge store for AI coding agents: one ASGI app (REST `/api/v1`, MCP `/mcp`, health) on Postgres when
hosted or SQLite when local, plus a stdlib-only `cairn` CLI. **DESIGN.md is the source of truth**: read the relevant
section before changing behavior, and update it when a decision changes.

## Layout

- `src/cairn/app/`: settings (`CAIRN_*` env), app assembly (`build.py`), auth middleware, logging.
- `src/cairn/core/`: the domain: analyzer, search, write path, access scopes, admin ops, injection, stores/federation.
- `src/cairn/db/`: SQLAlchemy Core schema, engines, Alembic migrations (applied at startup).
- `src/cairn/routers/`: REST `/api/v1` and `/healthz` `/readyz`.
- `src/cairn/mcp/`: the five MCP tools and their plain-text results.
- `src/cairn/cli/`: the `cairn` command: serve, local, remote, auth, inject, setup, service, install.
- `src/cairn/assets/`: files the CLI installs (the wrap-up skill).
- `src/cairn/models.py`: request/response models shared by REST, MCP and federation.
- `tests/unit/`: fast tests on SQLite, no services.
- `tests/parity/`: search on SQLite and Postgres against the same corpus (DESIGN §6.6).
- `tests/integration/`: the real stack: hosted server on Postgres, MCP client, local federation, CLI flows.

## Commands

Everything runs in Docker; the host needs no Python or uv. `make help` lists the targets.

- `make lint`: ruff + mypy. `make format`: ruff's autofixes.
- `make test-unit`: unit and parity tests in the `cairn-dev` image (`make image` builds it).
- `make test`: builds the compose stack, runs `tests/integration` and `tests/parity` (on Postgres) in the `test`
  service, then `down -v`.
  The suite bootstraps the instance admin once, so it needs a fresh stack each run.
- One file while iterating:
  `docker run --rm -v "$PWD/src:/app/src" -v "$PWD/tests:/app/tests" cairn-dev pytest -q tests/unit/test_x.py`

## Conventions

- `from __future__ import annotations`, full type hints (mypy strict), line length 120, ruff rules E,W,F,I,UP,B,SIM.
  The code is not run through `ruff format`.
- Keep it minimal: no speculative options, short module docstrings, comments only for a non-obvious why.
- No new runtime dependencies.
- Same behavior everywhere: Postgres and SQLite, local and hosted, REST and MCP.
- Every read and write goes through the access scope (`core/access.py`); unreadable things answer 404, not 403.
- Schema changes go in `db/schema.py` plus an Alembic migration that is backward-compatible (add now, drop later).
- `cairn inject` and `cairn auth header` must not import fastapi, sqlalchemy, pydantic, httpx or mcp.
