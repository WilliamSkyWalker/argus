"""Structured logging for Argus agent.

Adds a per-thread / per-case context label that gets injected into every
log line. Dispatch mode runs N worker threads concurrently and their log
lines were interleaving with no way to attribute a Turn / step_progress /
evidence line back to its case — making post-hoc audit of LLM behavior
nearly impossible.

Usage:
    from argus.logger import set_case_context

    # in dispatcher worker thread, before each case:
    set_case_context(f"W{worker_idx}/c{case_idx}")

    # after teardown / between cases:
    set_case_context(f"W{worker_idx}")   # or "" to clear
"""

import contextvars
import logging
import sys

# Per-thread context label (e.g. "W1/c40"). Lives in contextvars so each
# worker thread can set independently without polluting sibling threads.
_case_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "argus_case_ctx", default=""
)


def set_case_context(label: str) -> None:
    """Set the current thread's case context label. Empty string clears."""
    _case_ctx.set(label or "")


class _CaseContextFilter(logging.Filter):
    """Inject %(case_ctx)s into every LogRecord based on the current
    thread's _case_ctx ContextVar."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _case_ctx.get()
        record.case_ctx = f"[{ctx}] " if ctx else ""
        return True


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(case_ctx)s%(message)s"
DATE_FORMAT = "%H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    """Get a named logger under the 'argus' namespace."""
    logger = logging.getLogger(f"argus.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        handler.addFilter(_CaseContextFilter())
        handler.flush = lambda: sys.stderr.flush()  # force flush after each log
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def set_level(level: str) -> None:
    """Set log level for all argus loggers. E.g. 'DEBUG', 'WARNING'."""
    logging.getLogger("argus").setLevel(getattr(logging, level.upper(), logging.INFO))
