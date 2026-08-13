"""IQ Option userbot — modular signal strategies on top of ``iq_option_api``.

Strategies only emit signals.  :mod:`userbot.core` owns configuration, the
broker session, risk, money-management and order execution so a custom
strategy can never wedge the process.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
