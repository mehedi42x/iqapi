"""Binary 1-minute — triple-confluence momentum.

Fires only when trend (EMA stack) + oscillator recovery (RSI / Stochastic)
+ candle impulse agree.  Designed for 60-second binary / turbo expiries.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from .base import Strategy, Signal
from . import indicators as ta


class Binary1(Strategy):
    name = "binary1"
    description = "Binary 1m triple confluence (EMA 9/21/50 + RSI recovery + stochastic + impulse)"
    instrument = "binary"
    timeframe = 60
    min_candles = 80
    tags = ("binary", "1m", "momentum")

    rsi_period = 9
    stoch_k = 14
    impulse_body = 0.55

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        last_bar = candles[-1]
        prev = candles[-2]

        ema9 = ta.ema(c, 9)
        ema21 = ta.ema(c, 21)
        ema50 = ta.ema(c, 50)
        e9, e21, e50 = ta.last(ema9), ta.last(ema21), ta.last(ema50)
        e9p = ta.last(ema9, 2)
        if None in (e9, e21, e50, e9p):
            return Signal.hold("ema warmup")

        rsi = ta.rsi(c, self.rsi_period)
        r0, r1 = ta.last(rsi), ta.last(rsi, 2)
        k_line, d_line = ta.stochastic(h, l, c, self.stoch_k, 3)
        k0, d0 = ta.last(k_line), ta.last(d_line)
        if None in (r0, r1, k0, d0):
            return Signal.hold("oscillator warmup")

        atr_line = ta.atr(h, l, c, 14)
        atr0 = ta.last(atr_line)
        if atr0 is None or atr0 <= 0:
            return Signal.hold("no atr")

        # Dead market: last range much smaller than ATR
        if ta.range_(last_bar) < 0.25 * atr0:
            return Signal.hold("compressed range")

        # Blow-off candle — skip, binary 1m hates these
        if ta.range_(last_bar) > 2.8 * atr0:
            return Signal.hold("blow-off bar")

        price = c[-1]
        bull_trend = price > e9 > e21 > e50 and e9 > e9p
        bear_trend = price < e9 < e21 < e50 and e9 < e9p

        # Higher-timeframe veto when core supplied 5x candles
        htf = ta.htf_bias(context.get("htf_candles") or [])
        if htf > 0:
            bear_trend = False
        elif htf < 0:
            bull_trend = False

        impulse_bull = (
            ta.is_bull(last_bar)
            and ta.body_ratio(last_bar) >= self.impulse_body
            and ta.is_bull(prev)
        )
        impulse_bear = (
            ta.is_bear(last_bar)
            and ta.body_ratio(last_bar) >= self.impulse_body
            and ta.is_bear(prev)
        )

        rsi_up = r1 <= 42 and r0 > r1 and r0 < 68
        rsi_dn = r1 >= 58 and r0 < r1 and r0 > 32
        stoch_up = k0 > d0 and k0 < 80
        stoch_dn = k0 < d0 and k0 > 20

        votes = 0
        reasons = []
        if bull_trend:
            votes += 1
            reasons.append("ema-stack↑")
        if bear_trend:
            votes -= 1
            reasons.append("ema-stack↓")
        if rsi_up:
            votes += 1
            reasons.append(f"rsi↑{r0:.0f}")
        if rsi_dn:
            votes -= 1
            reasons.append(f"rsi↓{r0:.0f}")
        if stoch_up:
            votes += 1
            reasons.append("stoch↑")
        if stoch_dn:
            votes -= 1
            reasons.append("stoch↓")
        if impulse_bull:
            votes += 1
            reasons.append("impulse↑")
        if impulse_bear:
            votes -= 1
            reasons.append("impulse↓")

        # Need at least 3 agreeing votes and a clear side
        if votes >= 3 and bull_trend:
            conf = ta.score_to_confidence(votes, floor=0.62, ceiling=0.92, scale=3.0)
            if ta.crossed_over(ema9, ema21):
                conf = min(0.95, conf + 0.06)
                reasons.append("9/21 cross")
            return Signal.call(conf, " | ".join(reasons), votes=votes, rsi=r0)
        if votes <= -3 and bear_trend:
            conf = ta.score_to_confidence(-votes, floor=0.62, ceiling=0.92, scale=3.0)
            if ta.crossed_under(ema9, ema21):
                conf = min(0.95, conf + 0.06)
                reasons.append("9/21 cross")
            return Signal.put(conf, " | ".join(reasons), votes=votes, rsi=r0)
        return Signal.hold(f"votes={votes} " + " ".join(reasons) if reasons else "no confluence")
