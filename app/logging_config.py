"""Logging setup with a filter that scrubs personal data and secrets from every record."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.safety.redaction import redact

_NOISY_LOGGERS = ("httpx", "httpcore", "mcp", "google_genai", "google.genai")
_SERVER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [secret for secret in secrets if len(secret) >= 6]

    def add_secrets(self, secrets: list[str]) -> None:
        self._secrets.extend(s for s in secrets if len(s) >= 6 and s not in self._secrets)

    def clean(self, text: str) -> str:
        return self._clean(text)

    def _clean(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[secret]")
        return redact(text).text

    def _clean_arg(self, value: object) -> object:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._clean(str(value))

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Clean the template and each argument separately, keeping the shape of
            # record.args: formatters such as uvicorn's AccessFormatter unpack it.
            if isinstance(record.msg, str):
                record.msg = self._clean(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(self._clean_arg(arg) for arg in record.args)
            elif isinstance(record.args, dict):
                record.args = {key: self._clean_arg(arg) for key, arg in record.args.items()}
            if record.exc_info:
                formatter = logging.Formatter()
                record.exc_text = self._clean(formatter.formatException(record.exc_info))
                record.exc_info = None
        except Exception:  # a logging filter must never break the app
            pass
        return True


_FILTER: RedactingFilter | None = None


def register_secrets(secrets: list[str]) -> None:
    """Mask values set at runtime (e.g. a new API key from /admin) in every later log record."""
    if _FILTER is not None:
        _FILTER.add_secrets(secrets)


def scrub(text: str) -> str:
    """Apply the log filter to text shown outside the log files (the admin log viewer)."""
    return _FILTER.clean(text) if _FILTER is not None else redact(text).text


def configure_logging(secrets: list[str], log_file: Path) -> None:
    global _FILTER
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_format)
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(log_format))
        root.addHandler(file_handler)

    if _FILTER is None:
        _FILTER = RedactingFilter(secrets)
    else:
        _FILTER.add_secrets(secrets)
    redacting_filter = _FILTER
    handlers = list(root.handlers)
    for name in _SERVER_LOGGERS:
        handlers.extend(logging.getLogger(name).handlers)
    for handler in handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(redacting_filter)
    # Transport libraries log full request URLs, which may embed credentials.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
