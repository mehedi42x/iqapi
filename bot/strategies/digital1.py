"""Digital 1-minute — MACD / Supertrend / Bollinger expansion.

Digital options pay on a strike; we only fire when a *regime shift* is
underway (supertrend flip or MACD hist cross) and ADX says the move has
enough fuel to survive a 60-second hold.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from .base import Strategy, Signal
from . import indicators as ta


class Digital1(Strategy):
    name = "digital1"
    description = "Digital 1m regime-shift (Supertrend + MACD hist + BB expansion + ADX)"
    instrument = "digital"
    timeframe = 60
    min_candles = 100
    tags = ("digital", "1m", "trend")

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        bar = candles[-1]

        st_line, st_dir = ta.supertrend(h, l, c, period=10, multiplier=2.4)
        d0, d1 = ta.last(st_dir), ta.last(st_dir, 2)
        macd_line, macd_sig, hist = ta.macd(c, 12, 26, 9)
        h0, h1 = ta.last(hist), ta.last(hist, 2)
        adx_line, pdi, mdi = ta.adx(h, l, c, 14)
        adx0, p0, m0 = ta.last(adx_line), ta.last(pdi), ta.last(mdi)
        width = ta.bb_width(c, 20, 2.0)
        w0, w_rank = ta.last(width), ta.percentile_rank(width, 40)
        atr0 = ta.last(ta.atr(h, l, c, 14))
        ema21 = ta.last(ta.ema(c, 21))

        if None in (d0, h0, h1, adx0, p0, m0, atr0, ema21):
            return Signal.hold("warmup")

        if atr0 <= 0 or ta.range_(bar) < 0.28 * atr0:
            return Signal.hold("flat market")

        st_flip_up = d1 is not None and d1 <= 0 < d0
        st_flip_dn = d1 is not None and d1 >= 0 > d0
        hist_up = h1 is not None and h1 <= 0 < h0
        hist_dn = h1 is not None and h1 >= 0 > h0
        macd_up = ta.crossed_over(macd_line, macd_sig)
        macd_dn = ta.crossed_under(macd_line, macd_sig)
        expanding = w_rank is not None and w_rank >= 0.55
        trending = adx0 >= 18

        htf = ta.htf_bias(context.get("htf_candles") or [])
        price = c[-1]

        long_votes = 0
        short_votes = 0
        why = []

        if d0 > 0:
            long_votes += 1
            why.append("st↑")
        if d0 < 0:
            short_votes += 1
            why.append("st↓")
        if st_flip_up:
            long_votes += 2
            why.append("st-flip↑")
        if st_flip_dn:
            short_votes += 2
            why.append("st-flip↓")
        if hist_up or macd_up:
            long_votes += 1
            why.append("macd↑")
        if hist_dn or macd_dn:
            short_votes += 1
            why.append("macd↓")
        if p0 > m0 and trending:
            long_votes += 1
            why.append(f"adx{adx0:.0f}+")
        if m0 > p0 and trending:
            short_votes += 1
            why.append(f"adx{adx0:.0f}-")
        if expanding:
            if price > ema21:
                long_votes += 1
            else:
                short_votes += 1
            why.append("bb-expand")
        if htf > 0:
            long_votes += 1
            why.append("htf↑")
        elif htf < 0:
            short_votes += 1
            why.append("htf↓")

        # Fire on a *regime shift*, not on a standing trend — otherwise a
        # clean tape would emit a signal on every bar.
        event_up = st_flip_up or hist_up or macd_up
        event_dn = st_flip_dn or hist_dn or macd_dn

        if event_up and long_votes >= 4 and long_votes >= short_votes + 2:
            conf = ta.score_to_confidence(long_votes + (1.2 if st_flip_up else 0),
                                          floor=0.64, ceiling=0.94, scale=3.2)
            return Signal.call(conf, " | ".join(why), adx=adx0, votes=long_votes)
        if event_dn and short_votes >= 4 and short_votes >= long_votes + 2:
            conf = ta.score_to_confidence(short_votes + (1.2 if st_flip_dn else 0),
                                          floor=0.64, ceiling=0.94, scale=3.2)
            return Signal.put(conf, " | ".join(why), adx=adx0, votes=short_votes)
        return Signal.hold(f"L{long_votes}/S{short_votes} " + " ".join(why))
