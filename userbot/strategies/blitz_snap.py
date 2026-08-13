"""Blitz snap — fade a 4–6 bar micro-extension.

 complementary to ``blitz_flash``.  When RSI(5) is pinned and the last
bar prints a rejection wick against a 3-bar burst, we fade it for a
5–15 second mean-reversion snap.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from .base import Strategy, Signal
from . import indicators as ta


class BlitzSnap(Strategy):
    name = "blitz_snap"
    description = "Blitz 5-15s snap fade (RSI5 extreme + rejection wick after a micro-burst)"
    instrument = "blitz"
    timeframe = 5
    min_candles = 40
    tags = ("blitz", "scalp", "mean-reversion")

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        bar = candles[-1]
        r0 = ta.last(ta.rsi(c, 5))
        atr0 = ta.last(ta.atr(h, l, c, 7))
        e8 = ta.last(ta.ema(c, 8))
        if None in (r0, atr0, e8) or atr0 <= 0:
            return Signal.hold("warmup")

        burst_up = all(ta.is_bull(x) for x in candles[-4:-1])
        burst_dn = all(ta.is_bear(x) for x in candles[-4:-1])
        ext_up = c[-1] > e8 + 0.8 * atr0
        ext_dn = c[-1] < e8 - 0.8 * atr0

        # fade only with a *rejection* print, never a continuation body
        reject_down = (ta.is_pin_bear(bar) or (ta.is_bear(bar) and ta.upper_wick(bar) > ta.body(bar)))
        reject_up = (ta.is_pin_bull(bar) or (ta.is_bull(bar) and ta.lower_wick(bar) > ta.body(bar)))

        if burst_up and ext_up and reject_down and r0 >= 78:
            return Signal.put(0.70, f"snap-fade↑ rsi{r0:.0f} wick-reject", rsi=r0)
        if burst_dn and ext_dn and reject_up and r0 <= 22:
            return Signal.call(0.70, f"snap-fade↓ rsi{r0:.0f} wick-reject", rsi=r0)
        return Signal.hold("no extension to fade")
