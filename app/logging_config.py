"""Logging setup with a filter that scrubs personal data and secrets from every record."""

from __future__ import annotations

import logging

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

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = self._clean(record.getMessage()), None
            if record.exc_info:
                formatter = logging.Formatter()
                record.exc_text = self._clean(formatter.formatException(record.exc_info))
                record.exc_info = None
        except Exception:  # a logging filter must never break the app
            pass
        return True


def configure_logging(secrets: list[str]) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    redacting_filter = RedactingFilter(secrets)
    handlers = list(logging.getLogger().handlers)
    for name in _SERVER_LOGGERS:
        handlers.extend(logging.getLogger(name).handlers)
    for handler in handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(redacting_filter)
    # Transport libraries log full request URLs, which may embed credentials.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
