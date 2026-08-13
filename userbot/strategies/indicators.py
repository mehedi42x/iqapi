"""Pure-Python technical indicators shared by every strategy.

No numpy / pandas on purpose — the userbot must run on a bare
``websocket-client`` + ``requests`` install.  Every helper is defensive:
short series return ``None`` instead of raising, so a strategy can just
skip the bar.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, List, Optional, Sequence, Tuple


Number = Optional[float]


# ---------------------------------------------------------------------------
# Candle accessors
# ---------------------------------------------------------------------------
def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def opens(c: Sequence[Any]) -> List[float]:
    return [_f(getattr(x, "open", 0.0)) for x in c]


def highs(c: Sequence[Any]) -> List[float]:
    return [_f(getattr(x, "high", 0.0)) for x in c]


def lows(c: Sequence[Any]) -> List[float]:
    return [_f(getattr(x, "low", 0.0)) for x in c]


def closes(c: Sequence[Any]) -> List[float]:
    return [_f(getattr(x, "close", 0.0)) for x in c]


def volumes(c: Sequence[Any]) -> List[float]:
    return [_f(getattr(x, "volume", 0.0)) for x in c]


def typical(c: Sequence[Any]) -> List[float]:
    return [(_f(getattr(x, "high")) + _f(getattr(x, "low")) + _f(getattr(x, "close"))) / 3.0
            for x in c]


def last(series: Sequence[Number], n: int = 1) -> Number:
    """Last non-None value (``n=1``) or the n-th last non-None value."""
    seen = 0
    for value in reversed(series):
        if value is None:
            continue
        seen += 1
        if seen == n:
            return value
    return None


def nz(value: Number, default: float = 0.0) -> float:
    return default if value is None else float(value)


def has_volume(c: Sequence[Any]) -> bool:
    return any(v > 0 for v in volumes(c))


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def sma(values: Sequence[float], period: int) -> List[Number]:
    n = len(values)
    out: List[Number] = [None] * n
    if period <= 0 or n < period:
        return out
    window = 0.0
    for i, value in enumerate(values):
        window += value
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> List[Number]:
    n = len(values)
    out: List[Number] = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def wma(values: Sequence[float], period: int) -> List[Number]:
    n = len(values)
    out: List[Number] = [None] * n
    if period <= 0 or n < period:
        return out
    denom = period * (period + 1) / 2.0
    for i in range(period - 1, n):
        total = 0.0
        for j in range(period):
            total += values[i - period + 1 + j] * (j + 1)
        out[i] = total / denom
    return out


# ---------------------------------------------------------------------------
# Momentum / oscillators
# ---------------------------------------------------------------------------
def rsi(values: Sequence[float], period: int = 14) -> List[Number]:
    n = len(values)
    out: List[Number] = [None] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return out


def stochastic(h: Sequence[float], l: Sequence[float], c: Sequence[float],
               k_period: int = 14, d_period: int = 3) -> Tuple[List[Number], List[Number]]:
    n = len(c)
    raw: List[Number] = [None] * n
    if n < k_period:
        return raw, [None] * n
    for i in range(k_period - 1, n):
        hh = max(h[i - k_period + 1:i + 1])
        ll = min(l[i - k_period + 1:i + 1])
        raw[i] = 50.0 if hh == ll else 100.0 * (c[i] - ll) / (hh - ll)
    k_line = sma([x if x is not None else 0.0 for x in raw], 3)
    # restore Nones before the first valid raw
    for i, value in enumerate(raw):
        if value is None:
            k_line[i] = None
    d_line = sma([x if x is not None else 0.0 for x in k_line], d_period)
    for i, value in enumerate(k_line):
        if value is None:
            d_line[i] = None
    return k_line, d_line


def macd(values: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[List[Number], List[Number], List[Number]]:
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line: List[Number] = [None] * len(values)
    for i, (a, b) in enumerate(zip(fast_ema, slow_ema)):
        if a is not None and b is not None:
            line[i] = a - b
    usable = [x if x is not None else 0.0 for x in line]
    sig = ema(usable, signal)
    # blank the warmup where macd itself is None
    for i, value in enumerate(line):
        if value is None:
            sig[i] = None
    hist: List[Number] = [None] * len(values)
    for i, (a, b) in enumerate(zip(line, sig)):
        if a is not None and b is not None:
            hist[i] = a - b
    return line, sig, hist


def cci(c: Sequence[Any], period: int = 20) -> List[Number]:
    tp = typical(c)
    n = len(tp)
    out: List[Number] = [None] * n
    if n < period:
        return out
    for i in range(period - 1, n):
        window = tp[i - period + 1:i + 1]
        mean = sum(window) / period
        dev = sum(abs(x - mean) for x in window) / period
        out[i] = 0.0 if dev == 0 else (tp[i] - mean) / (0.015 * dev)
    return out


def williams_r(h: Sequence[float], l: Sequence[float], c: Sequence[float],
               period: int = 14) -> List[Number]:
    n = len(c)
    out: List[Number] = [None] * n
    if n < period:
        return out
    for i in range(period - 1, n):
        hh = max(h[i - period + 1:i + 1])
        ll = min(l[i - period + 1:i + 1])
        out[i] = -50.0 if hh == ll else -100.0 * (hh - c[i]) / (hh - ll)
    return out


def roc(values: Sequence[float], period: int = 10) -> List[Number]:
    n = len(values)
    out: List[Number] = [None] * n
    for i in range(period, n):
        prev = values[i - period]
        out[i] = 0.0 if prev == 0 else 100.0 * (values[i] - prev) / prev
    return out


def momentum(values: Sequence[float], period: int = 10) -> List[Number]:
    n = len(values)
    out: List[Number] = [None] * n
    for i in range(period, n):
        out[i] = values[i] - values[i - period]
    return out


# ---------------------------------------------------------------------------
# Volatility / trend
# ---------------------------------------------------------------------------
def true_range(h: Sequence[float], l: Sequence[float], c: Sequence[float]) -> List[float]:
    n = len(c)
    out = [0.0] * n
    if not n:
        return out
    out[0] = h[0] - l[0]
    for i in range(1, n):
        out[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return out


def atr(h: Sequence[float], l: Sequence[float], c: Sequence[float],
        period: int = 14) -> List[Number]:
    tr = true_range(h, l, c)
    n = len(tr)
    out: List[Number] = [None] * n
    if n < period:
        return out
    seed = sum(tr[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def bollinger(values: Sequence[float], period: int = 20,
              dev: float = 2.0) -> Tuple[List[Number], List[Number], List[Number]]:
    mid = sma(values, period)
    n = len(values)
    upper: List[Number] = [None] * n
    lower: List[Number] = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1:i + 1]
        mean = mid[i]
        if mean is None:
            continue
        var = sum((x - mean) ** 2 for x in window) / period
        sd = math.sqrt(var)
        upper[i] = mean + dev * sd
        lower[i] = mean - dev * sd
    return upper, mid, lower


def bb_width(values: Sequence[float], period: int = 20, dev: float = 2.0) -> List[Number]:
    upper, mid, lower = bollinger(values, period, dev)
    out: List[Number] = [None] * len(values)
    for i, (u, m, l) in enumerate(zip(upper, mid, lower)):
        if u is None or m is None or l is None or m == 0:
            continue
        out[i] = (u - l) / m
    return out


def percent_b(values: Sequence[float], period: int = 20, dev: float = 2.0) -> List[Number]:
    upper, _, lower = bollinger(values, period, dev)
    out: List[Number] = [None] * len(values)
    for i, (u, l, v) in enumerate(zip(upper, lower, values)):
        if u is None or l is None or u == l:
            continue
        out[i] = (v - l) / (u - l)
    return out


def donchian(h: Sequence[float], l: Sequence[float],
             period: int = 20) -> Tuple[List[Number], List[Number]]:
    n = len(h)
    up: List[Number] = [None] * n
    dn: List[Number] = [None] * n
    if n < period:
        return up, dn
    for i in range(period - 1, n):
        up[i] = max(h[i - period + 1:i + 1])
        dn[i] = min(l[i - period + 1:i + 1])
    return up, dn


def adx(h: Sequence[float], l: Sequence[float], c: Sequence[float],
        period: int = 14) -> Tuple[List[Number], List[Number], List[Number]]:
    """Returns ``(adx, plus_di, minus_di)``."""
    n = len(c)
    adx_line: List[Number] = [None] * n
    pdi: List[Number] = [None] * n
    mdi: List[Number] = [None] * n
    if n < period * 2:
        return adx_line, pdi, mdi

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = true_range(h, l, c)
    for i in range(1, n):
        up_move = h[i] - h[i - 1]
        down_move = l[i - 1] - l[i]
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0.0

    def _wilder(series: Sequence[float]) -> List[Number]:
        out: List[Number] = [None] * n
        if n < period:
            return out
        seed = sum(series[1:period + 1])
        out[period] = seed
        prev = seed
        for i in range(period + 1, n):
            prev = prev - prev / period + series[i]
            out[i] = prev
        return out

    sm_tr = _wilder(tr)
    sm_p = _wilder(plus_dm)
    sm_m = _wilder(minus_dm)
    dx: List[Number] = [None] * n
    for i in range(n):
        if not sm_tr[i]:
            continue
        pdi[i] = 100.0 * (sm_p[i] or 0.0) / sm_tr[i]
        mdi[i] = 100.0 * (sm_m[i] or 0.0) / sm_tr[i]
        s = (pdi[i] or 0.0) + (mdi[i] or 0.0)
        dx[i] = 0.0 if s == 0 else 100.0 * abs((pdi[i] or 0.0) - (mdi[i] or 0.0)) / s

    # ADX = Wilder average of DX
    start = period * 2
    if start < n:
        window = [dx[i] for i in range(period + 1, start + 1) if dx[i] is not None]
        if window:
            prev = sum(window) / len(window)
            adx_line[start] = prev
            for i in range(start + 1, n):
                if dx[i] is None:
                    continue
                prev = (prev * (period - 1) + dx[i]) / period
                adx_line[i] = prev
    return adx_line, pdi, mdi


def supertrend(h: Sequence[float], l: Sequence[float], c: Sequence[float],
               period: int = 10, multiplier: float = 3.0) -> Tuple[List[Number], List[Number]]:
    """Returns ``(line, direction)`` where direction is +1 (bull) / -1 (bear)."""
    n = len(c)
    line: List[Number] = [None] * n
    direction: List[Number] = [None] * n
    atr_line = atr(h, l, c, period)
    if n < period + 1:
        return line, direction

    upper = [None] * n
    lower = [None] * n
    for i in range(n):
        if atr_line[i] is None:
            continue
        mid = (h[i] + l[i]) / 2.0
        upper[i] = mid + multiplier * atr_line[i]
        lower[i] = mid - multiplier * atr_line[i]

    # first valid
    i0 = next((i for i in range(n) if upper[i] is not None), None)
    if i0 is None:
        return line, direction
    line[i0] = lower[i0]
    direction[i0] = 1.0
    for i in range(i0 + 1, n):
        if upper[i] is None or lower[i] is None:
            continue
        prev_dir = direction[i - 1] or 1.0
        prev_line = line[i - 1]
        if prev_dir >= 0:
            lvl = max(lower[i], prev_line) if prev_line is not None else lower[i]
            if c[i] < lvl:
                direction[i] = -1.0
                line[i] = upper[i]
            else:
                direction[i] = 1.0
                line[i] = lvl
        else:
            lvl = min(upper[i], prev_line) if prev_line is not None else upper[i]
            if c[i] > lvl:
                direction[i] = 1.0
                line[i] = lower[i]
            else:
                direction[i] = -1.0
                line[i] = lvl
    return line, direction


def vwap(c: Sequence[Any], period: Optional[int] = None) -> List[Number]:
    """Rolling VWAP.  Falls back to typical-price SMA when volume is missing."""
    tp = typical(c)
    vol = volumes(c)
    n = len(tp)
    out: List[Number] = [None] * n
    use_vol = any(v > 0 for v in vol)
    if period is None:
        cum_pv = 0.0
        cum_v = 0.0
        for i in range(n):
            v = vol[i] if use_vol else 1.0
            cum_pv += tp[i] * v
            cum_v += v
            out[i] = tp[i] if cum_v == 0 else cum_pv / cum_v
        return out
    if n < period:
        return out
    for i in range(period - 1, n):
        pv = 0.0
        vv = 0.0
        for j in range(i - period + 1, i + 1):
            v = vol[j] if use_vol else 1.0
            pv += tp[j] * v
            vv += v
        out[i] = tp[i] if vv == 0 else pv / vv
    return out


# ---------------------------------------------------------------------------
# Structure / candles
# ---------------------------------------------------------------------------
def body(candle: Any) -> float:
    return abs(_f(candle.close) - _f(candle.open))


def range_(candle: Any) -> float:
    return max(1e-12, _f(candle.high) - _f(candle.low))


def upper_wick(candle: Any) -> float:
    return _f(candle.high) - max(_f(candle.open), _f(candle.close))


def lower_wick(candle: Any) -> float:
    return min(_f(candle.open), _f(candle.close)) - _f(candle.low)


def is_bull(candle: Any) -> bool:
    return _f(candle.close) > _f(candle.open)


def is_bear(candle: Any) -> bool:
    return _f(candle.close) < _f(candle.open)


def body_ratio(candle: Any) -> float:
    return body(candle) / range_(candle)


def is_doji(candle: Any, thresh: float = 0.12) -> bool:
    return body_ratio(candle) <= thresh


def is_pin_bull(candle: Any) -> bool:
    """Hammer / dragonfly: long lower wick, close in the top third."""
    r = range_(candle)
    return (lower_wick(candle) >= 2.0 * body(candle)
            and upper_wick(candle) <= 0.35 * r
            and _f(candle.close) >= _f(candle.low) + 0.6 * r)


def is_pin_bear(candle: Any) -> bool:
    r = range_(candle)
    return (upper_wick(candle) >= 2.0 * body(candle)
            and lower_wick(candle) <= 0.35 * r
            and _f(candle.close) <= _f(candle.high) - 0.6 * r)


def is_bull_engulf(prev: Any, curr: Any) -> bool:
    return (is_bear(prev) and is_bull(curr)
            and _f(curr.open) <= _f(prev.close)
            and _f(curr.close) >= _f(prev.open)
            and body(curr) > body(prev))


def is_bear_engulf(prev: Any, curr: Any) -> bool:
    return (is_bull(prev) and is_bear(curr)
            and _f(curr.open) >= _f(prev.close)
            and _f(curr.close) <= _f(prev.open)
            and body(curr) > body(prev))


def consecutive_color(c: Sequence[Any]) -> int:
    """+N green candles in a row, -N red candles in a row."""
    if not c:
        return 0
    sign = 1 if is_bull(c[-1]) else (-1 if is_bear(c[-1]) else 0)
    if sign == 0:
        return 0
    count = 0
    for candle in reversed(c):
        if sign > 0 and is_bull(candle):
            count += 1
        elif sign < 0 and is_bear(candle):
            count += 1
        else:
            break
    return sign * count


def swing_highs(h: Sequence[float], left: int = 3, right: int = 3) -> List[int]:
    idx: List[int] = []
    n = len(h)
    for i in range(left, n - right):
        window = h[i - left:i + right + 1]
        if h[i] >= max(window) and window.count(h[i]) == 1:
            idx.append(i)
    return idx


def swing_lows(l: Sequence[float], left: int = 3, right: int = 3) -> List[int]:
    idx: List[int] = []
    n = len(l)
    for i in range(left, n - right):
        window = l[i - left:i + right + 1]
        if l[i] <= min(window) and window.count(l[i]) == 1:
            idx.append(i)
    return idx


def last_swing_high(h: Sequence[float], left: int = 3, right: int = 3) -> Optional[float]:
    pts = swing_highs(h, left, right)
    return h[pts[-1]] if pts else None


def last_swing_low(l: Sequence[float], left: int = 3, right: int = 3) -> Optional[float]:
    pts = swing_lows(l, left, right)
    return l[pts[-1]] if pts else None


def ema_aligned(values: Sequence[float], periods: Sequence[int],
                direction: int) -> bool:
    """``direction`` +1 = fast > ... > slow (bull), -1 = the reverse."""
    stack = [last(ema(values, p)) for p in periods]
    if any(v is None for v in stack):
        return False
    if direction > 0:
        return all(stack[i] > stack[i + 1] for i in range(len(stack) - 1))
    return all(stack[i] < stack[i + 1] for i in range(len(stack) - 1))


def slope(series: Sequence[Number], lookback: int = 3) -> Number:
    a = last(series, lookback)
    b = last(series)
    if a is None or b is None:
        return None
    return b - a


def crossed_up(series: Sequence[Number], level: float) -> bool:
    a, b = last(series, 2), last(series)
    return a is not None and b is not None and a <= level < b


def crossed_down(series: Sequence[Number], level: float) -> bool:
    a, b = last(series, 2), last(series)
    return a is not None and b is not None and a >= level > b


def crossed_over(a: Sequence[Number], b: Sequence[Number]) -> bool:
    a1, a0 = last(a, 2), last(a)
    b1, b0 = last(b, 2), last(b)
    return None not in (a1, a0, b1, b0) and a1 <= b1 and a0 > b0


def crossed_under(a: Sequence[Number], b: Sequence[Number]) -> bool:
    a1, a0 = last(a, 2), last(a)
    b1, b0 = last(b, 2), last(b)
    return None not in (a1, a0, b1, b0) and a1 >= b1 and a0 < b0


def heikin_ashi(c: Sequence[Any]) -> List[Tuple[float, float, float, float]]:
    """Return ``(open, high, low, close)`` HA candles."""
    out: List[Tuple[float, float, float, float]] = []
    ha_open = None
    for candle in c:
        ha_close = (_f(candle.open) + _f(candle.high) + _f(candle.low) + _f(candle.close)) / 4.0
        if ha_open is None:
            ha_open = (_f(candle.open) + _f(candle.close)) / 2.0
        ha_high = max(_f(candle.high), ha_open, ha_close)
        ha_low = min(_f(candle.low), ha_open, ha_close)
        out.append((ha_open, ha_high, ha_low, ha_close))
        ha_open = (ha_open + ha_close) / 2.0
    return out


def session_of(ts: float) -> str:
    """Very small UTC session classifier: asia / london / ny / overlap."""
    try:
        hour = int((__import__("datetime").datetime.utcfromtimestamp(float(ts))).hour)
    except (TypeError, ValueError, OSError):
        return "unknown"
    # overlap 12-16 UTC is the gold "money" window
    if 12 <= hour < 16:
        return "overlap"
    if 7 <= hour < 16:
        return "london"
    if 12 <= hour < 21:
        return "ny"
    return "asia"


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if value < lo else hi if value > hi else value


def score_to_confidence(score: float, *, floor: float = 0.50, ceiling: float = 0.95,
                        scale: float = 4.0) -> float:
    """Map an unbounded signed/unsigned score onto ``[floor, ceiling]``."""
    x = abs(float(score))
    # squash
    unit = 1.0 - math.exp(-x / max(1e-9, scale))
    return clamp(floor + (ceiling - floor) * unit, floor, ceiling)


def linear_slope(values: Sequence[float], period: int) -> Number:
    """Least-squares slope of the last ``period`` values."""
    if len(values) < period or period < 2:
        return None
    ys = list(values[-period:])
    n = float(period)
    sum_x = (n - 1.0) * n / 2.0
    sum_xx = (n - 1.0) * n * (2.0 * n - 1.0) / 6.0
    sum_y = sum(ys)
    sum_xy = sum(i * y for i, y in enumerate(ys))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    return (n * sum_xy - sum_x * sum_y) / denom


def percentile_rank(series: Sequence[Number], period: int) -> Number:
    """Percentile of the last value inside the last ``period`` values (0..1)."""
    clean = [float(x) for x in series[-period:] if x is not None]
    if len(clean) < max(5, period // 3):
        return None
    current = clean[-1]
    below = sum(1 for x in clean if x <= current)
    return below / len(clean)


def htf_bias(htf: Sequence[Any], fast: int = 8, slow: int = 21) -> int:
    """+1 / -1 / 0 from a higher-timeframe EMA stack."""
    if not htf or len(htf) < slow + 2:
        return 0
    c = closes(htf)
    e1, e2 = last(ema(c, fast)), last(ema(c, slow))
    if e1 is None or e2 is None:
        return 0
    price = c[-1]
    if price > e1 > e2:
        return 1
    if price < e1 < e2:
        return -1
    return 0
