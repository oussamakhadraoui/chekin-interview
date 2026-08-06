import logging
import uuid
from contextvars import ContextVar

import structlog

from app.config import settings

# Propagated from the inbound X-Request-ID header (or minted per request) and bound to
# every log line the request emits. With N instances behind a load balancer, this is
# what lets you follow one client's retry across whichever processes happened to serve
# the original and the retry.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex


def _add_request_id(_logger, _method, event_dict):
    event_dict["request_id"] = request_id_var.get()
    return event_dict


def configure_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger("ledger")
