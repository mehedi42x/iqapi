"""Binary 1-minute — price-action sniper.

Trades rejection at structure (swing high/low, EMA 21) confirmed by a
clear pin / engulfing bar and an ATR volatility floor.  Second binary
module — complementary to ``binary1`` (momentum) so you can A/B them.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from .base import Strategy, Signal
from . import indicators as ta


class BinarySniper(Strategy):
    name = "binary_sniper"
    description = "Binary 1m price-action sniper (pin/engulf at swing + EMA21, ATR filter)"
    instrument = "binary"
    timeframe = 60
    min_candles = 90
    tags = ("binary", "1m", "price-action")

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        bar = candles[-1]
        prev = candles[-2]

        ema21 = ta.ema(c, 21)
        ema50 = ta.ema(c, 50)
        e21, e50 = ta.last(ema21), ta.last(ema50)
        atr0 = ta.last(ta.atr(h, l, c, 14))
        rsi0 = ta.last(ta.rsi(c, 14))
        if None in (e21, e50, atr0, rsi0) or atr0 <= 0:
            return Signal.hold("warmup")

        if ta.range_(bar) < 0.30 * atr0:
            return Signal.hold("dead tape")
        if ta.range_(bar) > 3.0 * atr0:
            return Signal.hold("news spike")

        swing_hi = ta.last_swing_high(h, 3, 2)
        swing_lo = ta.last_swing_low(l, 3, 2)
        price = c[-1]
        touch_ema = abs(price - e21) <= 0.45 * atr0
        touch_lo = swing_lo is not None and abs(l[-1] - swing_lo) <= 0.35 * atr0
        touch_hi = swing_hi is not None and abs(h[-1] - swing_hi) <= 0.35 * atr0

        pin_up = ta.is_pin_bull(bar) or ta.is_pin_bull(prev)
        pin_dn = ta.is_pin_bear(bar) or ta.is_pin_bear(prev)
        eng_up = ta.is_bull_engulf(prev, bar)
        eng_dn = ta.is_bear_engulf(prev, bar)

        htf = ta.htf_bias(context.get("htf_candles") or [])
        trend_up = price > e50 or htf > 0
        trend_dn = price < e50 or htf < 0

        long_setup = (pin_up or eng_up) and (touch_ema or touch_lo) and trend_up and rsi0 < 62
        short_setup = (pin_dn or eng_dn) and (touch_ema or touch_hi) and trend_dn and rsi0 > 38

        if long_setup and not short_setup:
            reasons = []
            score = 2.0
            if pin_up:
                reasons.append("bull-pin")
                score += 0.6
            if eng_up:
                reasons.append("bull-engulf")
                score += 0.8
            if touch_lo:
                reasons.append("swing-low")
                score += 0.5
            if touch_ema:
                reasons.append("ema21")
                score += 0.3
            if rsi0 <= 35:
                reasons.append(f"rsi-os {rsi0:.0f}")
                score += 0.4
            if htf > 0:
                reasons.append("htf↑")
                score += 0.3
            return Signal.call(ta.score_to_confidence(score, floor=0.63, ceiling=0.93, scale=3.5),
                               " | ".join(reasons), rsi=rsi0)

        if short_setup and not long_setup:
            reasons = []
            score = 2.0
            if pin_dn:
                reasons.append("bear-pin")
                score += 0.6
            if eng_dn:
                reasons.append("bear-engulf")
                score += 0.8
            if touch_hi:
                reasons.append("swing-high")
                score += 0.5
            if touch_ema:
                reasons.append("ema21")
                score += 0.3
            if rsi0 >= 65:
                reasons.append(f"rsi-ob {rsi0:.0f}")
                score += 0.4
            if htf < 0:
                reasons.append("htf↓")
                score += 0.3
            return Signal.put(ta.score_to_confidence(score, floor=0.63, ceiling=0.93, scale=3.5),
                              " | ".join(reasons), rsi=rsi0)

        return Signal.hold("no rejection at structure")
