"""ASGI entry point: `uvicorn cairn.main:app`."""

from .app.build import build_app

app = build_app()
