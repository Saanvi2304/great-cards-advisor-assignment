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


def configure_logging(secrets: list[str], log_file: Path) -> None:
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_format)
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(log_format))
        root.addHandler(file_handler)

    redacting_filter = RedactingFilter(secrets)
    handlers = list(root.handlers)
    for name in _SERVER_LOGGERS:
        handlers.extend(logging.getLogger(name).handlers)
    for handler in handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(redacting_filter)
    # Transport libraries log full request URLs, which may embed credentials.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
