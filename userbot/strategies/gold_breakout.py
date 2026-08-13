"""XAUUSD / GOLD — compression → expansion breakout.

Gold trends hard once a squeeze releases.  We wait for a Donchian-20
break *on the close* (wicks alone are fake-outs), require Bollinger
width to be lifting off a low percentile, and want ADX turning up so
the 1-minute option has somewhere to go.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from .base import Strategy, Signal
from . import indicators as ta


class GoldBreakout(Strategy):
    name = "gold_breakout"
    description = "Gold 1m squeeze-breakout (Donchian 20 close + BB-width lift + rising ADX)"
    instrument = "any"
    timeframe = 60
    min_candles = 100
    assets = ("XAUUSD", "GOLD")
    tags = ("gold", "scalp", "breakout", "1m")

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        bar = candles[-1]
        prev = candles[-2]

        up, dn = ta.donchian(h, l, 20)
        # previous completed channel (exclude the current bar so a break is real)
        up_prev = ta.last(up, 2)
        dn_prev = ta.last(dn, 2)
        width = ta.bb_width(c, 20, 2.0)
        w0 = ta.last(width)
        w_rank = ta.percentile_rank(width, 60)
        adx_line, pdi, mdi = ta.adx(h, l, c, 14)
        adx0, adx1 = ta.last(adx_line), ta.last(adx_line, 2)
        p0, m0 = ta.last(pdi), ta.last(mdi)
        atr0 = ta.last(ta.atr(h, l, c, 14))
        if None in (up_prev, dn_prev, w0, adx0, p0, m0, atr0) or atr0 <= 0:
            return Signal.hold("warmup")

        if ta.range_(bar) > 2.8 * atr0:
            return Signal.hold("spike — skip")

        # squeeze recently: width was in the bottom 40% and is now rising
        width_slope = ta.slope(width, 3)
        squeeze_release = (
            w_rank is not None
            and 0.25 <= w_rank <= 0.80
            and width_slope is not None
            and width_slope > 0
        )
        adx_rising = adx1 is not None and adx0 >= 16 and adx0 >= adx1
        close_up = c[-1] > up_prev and ta.is_bull(bar) and ta.body_ratio(bar) >= 0.45
        close_dn = c[-1] < dn_prev and ta.is_bear(bar) and ta.body_ratio(bar) >= 0.45

        # optional retest: previous bar broke, this bar held the level
        retest_up = (
            ta.is_bull(bar)
            and _f(prev.close) > up_prev
            and l[-1] <= up_prev <= h[-1]
            and c[-1] > up_prev
        )
        retest_dn = (
            ta.is_bear(bar)
            and _f(prev.close) < dn_prev
            and l[-1] <= dn_prev <= h[-1]
            and c[-1] < dn_prev
        )

        htf = ta.htf_bias(context.get("htf_candles") or [])

        long_ok = (close_up or retest_up) and squeeze_release and adx_rising and p0 >= m0 and htf >= 0
        short_ok = (close_dn or retest_dn) and squeeze_release and adx_rising and m0 >= p0 and htf <= 0

        if long_ok and not short_ok:
            tag = "retest" if retest_up and not close_up else "break"
            conf = 0.68 + (0.08 if retest_up else 0.04) + (0.06 if adx0 >= 22 else 0)
            return Signal.call(min(0.93, conf),
                               f"gold {tag}↑ donchian adx{adx0:.0f} bb%{100 * (w_rank or 0):.0f}",
                               adx=adx0)
        if short_ok and not long_ok:
            tag = "retest" if retest_dn and not close_dn else "break"
            conf = 0.68 + (0.08 if retest_dn else 0.04) + (0.06 if adx0 >= 22 else 0)
            return Signal.put(min(0.93, conf),
                              f"gold {tag}↓ donchian adx{adx0:.0f} bb%{100 * (w_rank or 0):.0f}",
                              adx=adx0)
        return Signal.hold("no squeeze release")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
