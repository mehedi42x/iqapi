"""XAUUSD / GOLD — session sweep & reverse (ICT-style killzone).

Gold prints a clean Asian range, then London / New York raid the
high or low and reverse.  We:

1. Build the *previous session* high / low (Asia for London, London
   for NY, using UTC hours).
2. Require a sweep — wick through the level by 0.25–1.4 ATR, close
   back inside.
3. Confirm with a reversal bar (engulf / pin / opposite close).
4. Only trade inside the London (07-10) or NY (12-15) killzones,
   plus the 12-16 overlap.

This is a *selective* module: most bars are a hold.  That is the point.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import Strategy, Signal
from . import indicators as ta


# UTC windows (inclusive start, exclusive end)
ASIA = (0, 7)
LONDON_KZ = (7, 10)
NY_KZ = (12, 15)
OVERLAP = (12, 16)


def _hour(ts: float) -> int:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).hour
    except (TypeError, ValueError, OSError):
        return -1


def _in(hour: int, window: Tuple[int, int]) -> bool:
    return window[0] <= hour < window[1]


def _range_of(candles: Sequence[Any], start_h: int, end_h: int) -> Optional[Tuple[float, float]]:
    """High/low of the most recent completed window matching ``[start_h, end_h)``."""
    if not candles:
        return None
    # walk backwards, collect bars whose hour is inside the window, stop when
    # we leave the window after having entered it
    highs: List[float] = []
    lows: List[float] = []
    seen = False
    for bar in reversed(candles[:-1]):  # exclude the live/last bar
        ts = getattr(bar, "from_ts", None) or getattr(bar, "to_ts", None)
        if ts is None:
            continue
        hour = _hour(ts)
        if hour < 0:
            continue
        inside = _in(hour, (start_h, end_h))
        if inside:
            seen = True
            highs.append(float(bar.high))
            lows.append(float(bar.low))
        elif seen:
            break
    if not highs:
        return None
    return max(highs), min(lows)


class GoldSession(Strategy):
    name = "gold_session"
    description = "Gold 1m session sweep-reversal (Asia/London raid + killzone confirmation)"
    instrument = "any"
    timeframe = 60
    min_candles = 120
    assets = ("XAUUSD", "GOLD")
    tags = ("gold", "scalp", "session", "ict", "1m")

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        bar = candles[-1]
        prev = candles[-2]
        ts = getattr(bar, "from_ts", None) or context.get("server_time")
        hour = _hour(ts) if ts else -1
        if hour < 0:
            return Signal.hold("no timestamp")

        in_kz = _in(hour, LONDON_KZ) or _in(hour, NY_KZ) or _in(hour, OVERLAP)
        if not in_kz:
            return Signal.hold(f"outside killzone (utc {hour:02d}h)")

        atr0 = ta.last(ta.atr(h, l, c, 14))
        rsi0 = ta.last(ta.rsi(c, 14))
        if atr0 is None or atr0 <= 0 or rsi0 is None:
            return Signal.hold("warmup")
        if ta.range_(bar) > 2.8 * atr0:
            return Signal.hold("news spike")

        # Which range are we raiding?  Only the last ~16 hours matter.
        recent = candles[-960:] if len(candles) > 960 else candles
        if _in(hour, LONDON_KZ):
            rng = _range_of(recent, *ASIA)
            label = "asia"
        else:
            rng = _range_of(recent, 7, 12) or _range_of(recent, *ASIA)
            label = "london/asia"
        if rng is None:
            return Signal.hold("no session range yet")
        sess_hi, sess_lo = rng

        sweep_hi = (
            h[-1] > sess_hi
            and (h[-1] - sess_hi) >= 0.25 * atr0
            and (h[-1] - sess_hi) <= 1.50 * atr0
            and c[-1] < sess_hi
        )
        sweep_lo = (
            l[-1] < sess_lo
            and (sess_lo - l[-1]) >= 0.25 * atr0
            and (sess_lo - l[-1]) <= 1.50 * atr0
            and c[-1] > sess_lo
        )

        confirm_dn = (
            ta.is_pin_bear(bar) or ta.is_bear_engulf(prev, bar)
            or (ta.is_bear(bar) and ta.body_ratio(bar) >= 0.5)
        )
        confirm_up = (
            ta.is_pin_bull(bar) or ta.is_bull_engulf(prev, bar)
            or (ta.is_bull(bar) and ta.body_ratio(bar) >= 0.5)
        )

        htf = ta.htf_bias(context.get("htf_candles") or [])

        if sweep_hi and confirm_dn and rsi0 >= 55 and htf <= 0:
            extra = 0.06 if ta.is_bear_engulf(prev, bar) else 0.0
            return Signal.put(min(0.94, 0.70 + extra),
                              f"gold sweep {label}-high utc{hour:02d}h rsi{rsi0:.0f}",
                              level=sess_hi, session=label)
        if sweep_lo and confirm_up and rsi0 <= 45 and htf >= 0:
            extra = 0.06 if ta.is_bull_engulf(prev, bar) else 0.0
            return Signal.call(min(0.94, 0.70 + extra),
                               f"gold sweep {label}-low utc{hour:02d}h rsi{rsi0:.0f}",
                               level=sess_lo, session=label)
        return Signal.hold(f"no sweep ({label} {sess_lo:.2f}-{sess_hi:.2f})")
