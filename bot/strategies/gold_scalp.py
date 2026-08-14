"""XAUUSD / GOLD — VWAP mean-reversion scalp.

Gold spends a lot of the 1-minute tape oscillating around session VWAP
and round-number magnets.  We fade a 0.6–1.4 ATR extension only when
RSI(7) is extreme *and* the last bar rejects (pin or engulf).  A
volatility floor stops us trading the dead Asian grind; a ceiling
skips news spikes.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from .base import Strategy, Signal
from . import indicators as ta


class GoldScalp(Strategy):
    name = "gold_scalp"
    description = "Gold 1m VWAP fade (RSI7 extreme + rejection, ATR band 0.6-1.4)"
    instrument = "any"
    timeframe = 60
    min_candles = 80
    assets = ("XAUUSD", "GOLD")
    tags = ("gold", "scalp", "vwap", "1m")

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        bar = candles[-1]
        prev = candles[-2]

        vwap = ta.vwap(candles, period=40)
        v0 = ta.last(vwap)
        rsi7 = ta.rsi(c, 7)
        r0, r1 = ta.last(rsi7), ta.last(rsi7, 2)
        atr0 = ta.last(ta.atr(h, l, c, 14))
        e21 = ta.last(ta.ema(c, 21))
        if None in (v0, r0, r1, atr0, e21) or atr0 <= 0:
            return Signal.hold("warmup")

        rng = ta.range_(bar)
        if rng < 0.35 * atr0:
            return Signal.hold("gold asleep")
        if rng > 2.6 * atr0:
            return Signal.hold("gold spike — stand aside")

        dist = (c[-1] - v0) / atr0
        reject_up = ta.is_pin_bull(bar) or ta.is_bull_engulf(prev, bar)
        reject_dn = ta.is_pin_bear(bar) or ta.is_bear_engulf(prev, bar)
        htf = ta.htf_bias(context.get("htf_candles") or [])

        # Long fade: stretched below VWAP, RSI hooking up, rejection
        long_ok = (
            dist <= -0.60
            and dist >= -1.50
            and r0 <= 32 and r0 > r1
            and reject_up
            and htf >= 0
        )
        short_ok = (
            dist >= 0.60
            and dist <= 1.50
            and r0 >= 68 and r0 < r1
            and reject_dn
            and htf <= 0
        )

        if long_ok and not short_ok:
            conf = ta.score_to_confidence(abs(dist) + (0.4 if r0 < 25 else 0),
                                          floor=0.64, ceiling=0.92, scale=1.6)
            return Signal.call(conf, f"gold vwap-fade↑ dist{dist:+.2f}atr rsi{r0:.0f}",
                               dist=dist, vwap=v0)
        if short_ok and not long_ok:
            conf = ta.score_to_confidence(abs(dist) + (0.4 if r0 > 75 else 0),
                                          floor=0.64, ceiling=0.92, scale=1.6)
            return Signal.put(conf, f"gold vwap-fade↓ dist{dist:+.2f}atr rsi{r0:.0f}",
                              dist=dist, vwap=v0)
        return Signal.hold(f"vwap dist={dist:+.2f} rsi={r0:.0f}")
