"""Strategy package.

Drop a new ``*.py`` file next to this one, subclass :class:`Strategy`,
set ``name = "my_module"``, implement ``analyze`` — ``core.py`` will
pick it up automatically.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Type

from .base import CALL, HOLD, PUT, Signal, Strategy, closed_candles

__all__ = [
    "CALL", "PUT", "HOLD",
    "Signal", "Strategy",
    "closed_candles",
    "discover", "load_strategy", "list_strategies",
]

_PKG_DIR = Path(__file__).resolve().parent
_SKIP = {"base", "indicators"}


def _ensure_import_path() -> None:
    """Make both ``import strategies`` and ``import userbot.strategies`` work."""
    parent = str(_PKG_DIR.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)


def _is_strategy(obj: object) -> bool:
    return (isinstance(obj, type)
            and issubclass(obj, Strategy)
            and obj is not Strategy
            and not getattr(obj, "__abstractmethods__", None))


def _classes_in(module) -> List[Type[Strategy]]:
    found: List[Type[Strategy]] = []
    for attr in dir(module):
        if attr.startswith("_"):
            continue
        obj = getattr(module, attr)
        if _is_strategy(obj):
            found.append(obj)
    return found


def discover() -> Dict[str, Strategy]:
    """Instantiate every Strategy subclass shipped in this folder."""
    _ensure_import_path()
    found: Dict[str, Strategy] = {}
    for info in pkgutil.iter_modules([str(_PKG_DIR)]):
        if info.name.startswith("_") or info.name in _SKIP:
            continue
        try:
            module = importlib.import_module(f"strategies.{info.name}")
        except Exception:
            try:
                module = importlib.import_module(f"userbot.strategies.{info.name}")
            except Exception:
                continue
        for cls in _classes_in(module):
            try:
                inst = cls()
            except Exception:
                continue
            key = (inst.name or cls.__name__).strip().lower()
            found[key] = inst
    return found


def load_from_path(path: "str | Path") -> Strategy:
    """Load a user-supplied ``.py`` file that contains a Strategy subclass."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"strategy file not found: {path}")
    spec = importlib.util.spec_from_file_location(f"custom_strategy_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import strategy from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    classes = _classes_in(module)
    if not classes:
        raise ImportError(f"{path} does not define a Strategy subclass")
    return classes[0]()


def load_strategy(name_or_path: str,
                  available: Optional[Dict[str, Strategy]] = None) -> Strategy:
    """Resolve ``STRATEGY=`` from .env — module name *or* filesystem path."""
    raw = (name_or_path or "").strip()
    if not raw:
        raise ValueError("empty strategy name")
    if raw.endswith(".py") or "/" in raw or "\\" in raw:
        return load_from_path(raw)
    catalog = available if available is not None else discover()
    key = raw.lower()
    if key in catalog:
        inst = catalog[key]
        inst.reset()
        return inst
    # filename without the class name matching
    for inst in catalog.values():
        if type(inst).__name__.lower() == key:
            inst.reset()
            return inst
    known = ", ".join(sorted(catalog)) or "(none)"
    raise KeyError(f"unknown strategy {raw!r}. available: {known}")


def list_strategies() -> List[Dict[str, object]]:
    rows = [inst.info() for inst in discover().values()]
    rows.sort(key=lambda r: str(r.get("name", "")))
    return rows
