"""Единственный процесс Telethon: file lock на сессию."""

from __future__ import annotations

import atexit
import fcntl
import os
from pathlib import Path

from src.config import ROOT

_lock_fd: int | None = None


def session_base() -> Path:
    raw = os.environ.get("TG_SESSION_PATH", "").strip()
    if raw:
        return Path(raw.removesuffix(".session"))
    return (ROOT / "data" / "tg_user").resolve()


def acquire_telethon_lock() -> Path:
    """Эксклюзивный flock. Второй процесс с Telethon не стартует."""
    global _lock_fd
    base = session_base()
    base.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(base) + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RuntimeError(
            f"Telethon уже занят другим процессом (lock {lock_path}). "
            "Оставь только: python -m src.ingest.run"
        ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    _lock_fd = fd

    def _release() -> None:
        global _lock_fd
        if _lock_fd is None:
            return
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None

    atexit.register(_release)
    return base
