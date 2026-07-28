"""Policy context for log records.

Roughly half the package's log calls sit in pure helpers that have no business
taking a ``policy_name`` argument — join-collision warnings in ``transform``,
duplicate-id warnings in ``extract``. A context variable carries it instead, so
every record can be attributed to a policy without threading orchestration
state through the mapping layer.

Use :func:`get_logger` rather than ``logging.getLogger`` inside this package: a
``logging.Filter`` attached to a parent logger is *not* applied to records from
its children, so each logger needs the filter itself.

Operators format it with ``%(policy)s``; it reads ``-`` outside a tick.
"""

from __future__ import annotations

import contextvars
import logging

_policy_name: contextvars.ContextVar[str] = contextvars.ContextVar("policy_name", default="-")


class PolicyContextFilter(logging.Filter):
    """Attach the current policy name to every record as ``%(policy)s``."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Stamp the record and keep it."""
        record.policy = _policy_name.get()
        return True


_FILTER = PolicyContextFilter()


def get_logger(name: str) -> logging.Logger:
    """Package logger that stamps the current policy name onto its records."""
    logger = logging.getLogger(name)
    if _FILTER not in logger.filters:
        logger.addFilter(_FILTER)
    return logger


def set_policy_name(name: str) -> None:
    """Bind the policy name for the current context (one tick)."""
    _policy_name.set(name)


def current_policy_name() -> str:
    """Policy the calling context's tick belongs to, or ``-`` outside a tick."""
    return _policy_name.get()
