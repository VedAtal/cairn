from __future__ import annotations

import logging
import re

import structlog


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    pre_chain: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    structlog.configure(
        processors=[*pre_chain, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler()
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(processors=[
        structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer], foreign_pre_chain=pre_chain))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").addFilter(_redact)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True


_SECRET_QUERY = re.compile(r"((?:token|ticket)=)[^&\s\"]+")


def _redact(record: logging.LogRecord) -> bool:
    """Set-password tokens and UI tickets travel in URLs; keep them out of access logs."""
    record.args = tuple(_SECRET_QUERY.sub(r"\1…", a) if isinstance(a, str) else a for a in record.args or ())
    return True
