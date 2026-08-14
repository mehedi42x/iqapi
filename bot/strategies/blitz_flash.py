"""Blitz flash — ultra-short momentum (5–30 s).

Blitz expires on a timer, not a candle close, so we only fire when the
*micro* tape is already committed: 3–8 EMA alignment, RSI(5) leaving an
extreme, and the last two Heikin-Ashi bars the same colour.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from .base import Strategy, Signal
from . import indicators as ta


class BlitzFlash(Strategy):
    name = "blitz_flash"
    description = "Blitz 5-30s flash momentum (EMA 3/8 + RSI5 exit + Heikin-Ashi)"
    instrument = "blitz"
    timeframe = 5
    min_candles = 40
    tags = ("blitz", "scalp", "momentum")

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        ema3 = ta.ema(c, 3)
        ema8 = ta.ema(c, 8)
        e3, e8, e3p = ta.last(ema3), ta.last(ema8), ta.last(ema3, 2)
        r = ta.rsi(c, 5)
        r0, r1 = ta.last(r), ta.last(r, 2)
        atr0 = ta.last(ta.atr(h, l, c, 7))
        if None in (e3, e8, e3p, r0, r1, atr0):
            return Signal.hold("warmup")

        bar = candles[-1]
        if ta.range_(bar) < 0.20 * atr0:
            return Signal.hold("no tick energy")

        ha = ta.heikin_ashi(candles[-6:])
        ha_up = len(ha) >= 2 and ha[-1][3] > ha[-1][0] and ha[-2][3] > ha[-2][0]
        ha_dn = len(ha) >= 2 and ha[-1][3] < ha[-1][0] and ha[-2][3] < ha[-2][0]
        run = ta.consecutive_color(candles)

        long_ok = (
            e3 > e8 and e3 > e3p
            and r1 <= 40 and r0 > r1 and r0 < 78
            and ha_up
            and run >= 2
        )
        short_ok = (
            e3 < e8 and e3 < e3p
            and r1 >= 60 and r0 < r1 and r0 > 22
            and ha_dn
            and run <= -2
        )

        if long_ok and not short_ok:
            conf = 0.66 + min(0.22, 0.04 * abs(run) + (0.06 if r1 < 30 else 0))
            return Signal.call(conf, f"flash↑ ha-run{run} rsi{r0:.0f}", rsi=r0)
        if short_ok and not long_ok:
            conf = 0.66 + min(0.22, 0.04 * abs(run) + (0.06 if r1 > 70 else 0))
            return Signal.put(conf, f"flash↓ ha-run{run} rsi{r0:.0f}", rsi=r0)
        return Signal.hold("no blitz impulse")
