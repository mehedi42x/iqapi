"""Copy this file to ``my_strategy.py`` (no leading underscore) and edit.

Rules
-----
* Subclass ``Strategy`` and implement ``analyze``.
* Return a ``Signal`` — never place an order, never sleep on the network.
* ``core.py`` will discover the class automatically once the file lives
  in this folder.  Or point ``STRATEGY=/absolute/path/to/file.py`` in ``.env``.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from .base import Signal, Strategy
from . import indicators as ta


class MyStrategy(Strategy):
    name = "my_strategy"          # this is what you put in STRATEGY=
    description = "example custom module — replace me"
    instrument = "binary"         # binary | digital | blitz | any
    timeframe = 60                # seconds (documentation + auto-picker)
    min_candles = 50
    assets = ()                   # e.g. ("XAUUSD", "GOLD") or empty = any
    tags = ("custom",)

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        closes = ta.closes(candles)
        rsi = ta.last(ta.rsi(closes, 14))
        ema21 = ta.last(ta.ema(closes, 21))
        if rsi is None or ema21 is None:
            return Signal.hold("warmup")

        price = closes[-1]
        if rsi < 30 and price > ema21:
            return Signal.call(0.70, f"rsi {rsi:.0f} bounce above ema21")
        if rsi > 70 and price < ema21:
            return Signal.put(0.70, f"rsi {rsi:.0f} reject below ema21")
        return Signal.hold(f"rsi={rsi:.0f}")

    def on_result(self, signal, result, pnl, context=None):
        """Optional.  Called by core after a trade settles (win/loss/equal)."""
