"""IQ Option userbot — modular signal strategies on top of ``iq_option_api``.

Strategies only emit signals.  :mod:`userbot.core` owns configuration, the
broker session, risk, money-management and order execution so a custom
strategy can never wedge the process.
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "1.0.0"


def runtime_dir() -> Path:
    """Where the bot keeps its writable state (``.env``, ``logs/``, ``data/``).

    Resolution order:

    1. ``IQ_USERBOT_DIR`` env var — explicit override.
    2. The package/source folder itself, when it already holds a ``.env``
       (typical for a source checkout / ``pip install -e .``).
    3. ``~/.iqapi`` — used when the package is installed globally (a plain
       ``pip install iqapi``) and ``site-packages`` is not writable.

    The directory is created if it does not exist yet.
    """
    override = os.environ.get("IQ_USERBOT_DIR")
    if override:
        d = Path(override).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d

    pkg = Path(__file__).resolve().parent
    if (pkg / ".env").is_file():
        return pkg

    home = Path.home() / ".iqapi"
    home.mkdir(parents=True, exist_ok=True)
    return home


__all__ = ["__version__", "runtime_dir"]
