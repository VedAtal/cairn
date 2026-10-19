FROM python:3.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv PATH="/venv/bin:$PATH"
WORKDIR /app

# Dependencies first, so source edits don't invalidate the layer.
COPY pyproject.toml uv.lock* README.md ./
RUN uv sync --no-install-project --no-dev

COPY src ./src
RUN uv sync --no-dev

# Test image: adds dev deps and the test suite. `docker compose run test` uses it.
FROM base AS test
RUN uv sync
COPY tests ./tests
CMD ["pytest", "-q"]

FROM base AS server
RUN useradd --create-home cairn
USER cairn
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["cairn", "serve", "--host", "0.0.0.0", "--port", "8000"]
