# Everything runs in Docker; the host needs only docker (no uv or Python).
# Not cairn-test: compose already uses that name for the test service's image.
IMAGE := cairn-dev
RUN := docker run --rm -v "$(CURDIR)/src:/app/src" -v "$(CURDIR)/tests:/app/tests" \
	-v "$(CURDIR)/pyproject.toml:/app/pyproject.toml" $(IMAGE)

.PHONY: help image lint format test-unit test test-all server down logs

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

image: ## build the test image (deps + dev tools; src and tests are mounted at run time)
	docker build --target test -t $(IMAGE) .

lint: image ## ruff + mypy
	$(RUN) sh -c 'ruff check src tests && mypy'

format: image ## apply ruff's fixes (imports, upgrades); the code is not ruff-formatted
	$(RUN) ruff check --fix src tests

test-unit: image ## unit and parity tests (SQLite, no services)
	$(RUN) pytest -q tests/unit tests/parity

test: ## integration + Postgres parity tests against the compose stack
	docker compose --profile test build
	docker compose --profile test run --rm test; status=$$?; docker compose --profile test down -v; exit $$status

test-all: test-unit test ## everything

server: ## run a hosted instance on http://localhost:8000
	docker compose up --build server

down: ## stop the stack and delete its data
	docker compose --profile test down -v

logs: ## follow the server logs
	docker compose logs -f server
