"""Runtime compatibility shims.

Python 3.13 removed the camelCase alias ``threading.Thread.isAlive``.
Old ``websocket-client`` still calls it from ``WebSocketApp.run_forever``
teardown whenever ``ping_interval`` starts a ping thread.  That surfaces
as the bogus connect error::

    'Thread' object has no attribute 'isAlive'

The known-good IQ Option snippet never passes ``ping_interval``, so it
never hits the crash.  We still patch the alias so any leftover library
code keeps working on Termux / Python 3.14.
"""

from __future__ import annotations

import threading
from typing import Any


def patch_thread_is_alive() -> None:
    """Restore ``Thread.isAlive`` as an alias of ``is_alive`` when missing."""
    if not hasattr(threading.Thread, "isAlive"):
        threading.Thread.isAlive = threading.Thread.is_alive  # type: ignore[attr-defined]


def thread_is_alive(thread: Any) -> bool:
    """Version-safe liveness check."""
    if thread is None:
        return False
    probe = getattr(thread, "is_alive", None) or getattr(thread, "isAlive", None)
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:
        return False


# Apply on import so every connection module is safe.
patch_thread_is_alive()
