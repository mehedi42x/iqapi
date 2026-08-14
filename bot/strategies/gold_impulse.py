"""XAUUSD / GOLD — impulse / pullback continuation.

The bread-and-butter gold scalp: EMA 8/21/55 stacked, a pullback that
kisses the fast EMA, then a strong-bodied resume candle in the trend
direction.  MACD histogram must already be on that side so we are not
catching the last gasp of a move.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from .base import Strategy, Signal
from . import indicators as ta


class GoldImpulse(Strategy):
    name = "gold_impulse"
    description = "Gold 1m impulse-pullback (EMA 8/21/55 stack + EMA kiss + MACD + resume bar)"
    instrument = "any"
    timeframe = 60
    min_candles = 90
    assets = ("XAUUSD", "GOLD")
    tags = ("gold", "scalp", "trend", "1m")

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        bar = candles[-1]

        ema8 = ta.ema(c, 8)
        ema21 = ta.ema(c, 21)
        ema55 = ta.ema(c, 55)
        e8, e21, e55 = ta.last(ema8), ta.last(ema21), ta.last(ema55)
        _m, _s, hist = ta.macd(c, 12, 26, 9)
        h0, h1 = ta.last(hist), ta.last(hist, 2)
        atr0 = ta.last(ta.atr(h, l, c, 14))
        rsi0 = ta.last(ta.rsi(c, 14))
        if None in (e8, e21, e55, h0, atr0, rsi0) or atr0 <= 0:
            return Signal.hold("warmup")

        if ta.range_(bar) > 2.7 * atr0:
            return Signal.hold("spike")

        bull = e8 > e21 > e55
        bear = e8 < e21 < e55
        # pullback: some bar in the last 4 touched EMA8 / EMA21
        touched_fast_from_above = any(
            ta.lows(candles[i:i + 1])[0] <= e8 + 0.15 * atr0
            for i in range(-4, 0)
        ) if bull else False
        touched_fast_from_below = any(
            ta.highs(candles[i:i + 1])[0] >= e8 - 0.15 * atr0
            for i in range(-4, 0)
        ) if bear else False

        resume_up = (
            ta.is_bull(bar)
            and ta.body_ratio(bar) >= 0.55
            and c[-1] > e8
            and h0 is not None and h0 > 0
            and (h1 is None or h0 >= h1)
        )
        resume_dn = (
            ta.is_bear(bar)
            and ta.body_ratio(bar) >= 0.55
            and c[-1] < e8
            and h0 is not None and h0 < 0
            and (h1 is None or h0 <= h1)
        )

        # market structure: last swing in the trend direction
        sh = ta.swing_highs(h, 2, 2)
        sl = ta.swing_lows(l, 2, 2)
        hh = len(sh) >= 2 and h[sh[-1]] > h[sh[-2]]
        ll = len(sl) >= 2 and l[sl[-1]] < l[sl[-2]]

        htf = ta.htf_bias(context.get("htf_candles") or [])

        long_ok = bull and touched_fast_from_above and resume_up and rsi0 < 72 and htf >= 0
        short_ok = bear and touched_fast_from_below and resume_dn and rsi0 > 28 and htf <= 0

        if long_ok and not short_ok:
            conf = 0.67 + (0.07 if hh else 0) + (0.05 if htf > 0 else 0) + (0.04 if ta.body_ratio(bar) > 0.7 else 0)
            return Signal.call(min(0.93, conf),
                               f"gold impulse↑ ema-stack pullback macd+ rsi{rsi0:.0f}",
                               rsi=rsi0)
        if short_ok and not long_ok:
            conf = 0.67 + (0.07 if ll else 0) + (0.05 if htf < 0 else 0) + (0.04 if ta.body_ratio(bar) > 0.7 else 0)
            return Signal.put(min(0.93, conf),
                              f"gold impulse↓ ema-stack pullback macd- rsi{rsi0:.0f}",
                              rsi=rsi0)
        return Signal.hold("no impulse resume")
