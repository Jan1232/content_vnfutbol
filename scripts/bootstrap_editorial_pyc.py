"""Preload modules from __pycache__ when .py sources are missing."""
from __future__ import annotations

import sys
from importlib.machinery import SourcelessFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_pyc(modname: str, pyc: Path) -> None:
    if modname in sys.modules:
        return
    loader = SourcelessFileLoader(modname, str(pyc))
    spec = spec_from_loader(modname, loader)
    if spec is None:
        raise ImportError(f"cannot load {modname} from {pyc}")
    mod = module_from_spec(spec)
    sys.modules[modname] = mod
    loader.exec_module(mod)


def _maybe_load_pkg(pkg: str, stem: str) -> None:
    py = ROOT / pkg / f"{stem}.py"
    if py.is_file():
        return
    pyc = ROOT / pkg / "__pycache__" / f"{stem}.cpython-312.pyc"
    if pyc.is_file():
        _load_pyc(f"{pkg.replace('/', '.')}.{stem}", pyc)


def bootstrap() -> None:
    for stem in ("config", "http_util", "db", "translate", "max_api"):
        _maybe_load_pkg("app", stem)
    for stem in (
        "catalogs",
        "cycle",
        "factcheck",
        "render",
        "scheduler",
        "sources",
        "store",
        "topic_gate",
        "profanity",
        "story_throttle",
    ):
        _maybe_load_pkg("editorial", stem)


bootstrap()
