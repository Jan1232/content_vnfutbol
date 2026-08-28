"""Live test day mode: cards to TG bot, no MAX publish, no auto-reject."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_live_test: ContextVar[bool] = ContextVar("editorial_live_test", default=False)
_live_test_date: ContextVar[str] = ContextVar("editorial_live_test_date", default="")


def is_live_test() -> bool:
    if _live_test.get():
        return True
    from app.config import get_settings

    return bool(getattr(get_settings(), "editorial_live_test", False))


def live_test_date() -> str:
    if _live_test_date.get():
        return _live_test_date.get()
    from app.config import get_settings

    return str(getattr(get_settings(), "editorial_test_date", "") or "").strip()


@contextmanager
def live_test_scope(date: str = "") -> Iterator[None]:
    tok_a = _live_test.set(True)
    tok_b = _live_test_date.set(date or "")
    try:
        yield
    finally:
        _live_test.reset(tok_a)
        _live_test_date.reset(tok_b)
