"""data — timeframe set + all market data traffic.

Candles, live candles, ticks, prices, server time.  The bot asks for data,
this module fetches it — nothing more.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# accepted timeframe aliases -> seconds
TIMEFRAMES: Dict[str, int] = {
    "S5": 5, "S10": 10, "S15": 15, "S30": 30,
    "M1": 60, "M2": 120, "M3": 180, "M5": 300, "M10": 600,
    "M15": 900, "M30": 1800,
    "H1": 3600, "H2": 7200, "H4": 14400, "H8": 28800,
    "D1": 86400, "W1": 604800,
}


def timeframe_to_seconds(timeframe: "int | str") -> int:
    """``"M5"`` -> 300, ``60`` -> 60."""
    if isinstance(timeframe, str):
        key = timeframe.strip().upper()
        if key in TIMEFRAMES:
            return TIMEFRAMES[key]
        if key.isdigit():
            return int(key)
        raise ValueError(f"unknown timeframe {timeframe!r} "
                         f"(use one of {sorted(TIMEFRAMES)} or seconds)")
    return int(timeframe)


class Data:
    """Market data for the bot."""

    def __init__(self, client: Any, state: Any) -> None:
        self._iq = client
        self._state = state

    # ------------------------------------------------------------------
    # timeframe set
    # ------------------------------------------------------------------
    def set_timeframe(self, timeframe: "int | str") -> int:
        """Default candle size — ``"M1"``, ``"M5"``, ``"H1"`` or seconds."""
        self._state.timeframe = timeframe_to_seconds(timeframe)
        return self._state.timeframe

    def get_timeframe(self) -> int:
        return self._state.timeframe

    def timeframes(self) -> Dict[str, int]:
        return dict(TIMEFRAMES)

    def _symbol(self, symbol: Optional[str]) -> str:
        return str(symbol or self._state.symbol).upper()

    def _tf(self, timeframe: "int | str | None") -> int:
        return (timeframe_to_seconds(timeframe) if timeframe is not None
                else self._state.timeframe)

    # ------------------------------------------------------------------
    # candles
    # ------------------------------------------------------------------
    def candles(self, symbol: Optional[str] = None, *,
                timeframe: "int | str | None" = None,
                count: int = 100,
                end_time: Optional[float] = None) -> List[Any]:
        """Historical candles (newest last)."""
        return self._iq.market.get_candles(self._symbol(symbol), self._tf(timeframe),
                                           int(count), end_time=end_time)

    def last_candle(self, symbol: Optional[str] = None, *,
                    timeframe: "int | str | None" = None) -> Optional[Any]:
        rows = self.candles(symbol, timeframe=timeframe, count=2)
        return rows[-1] if rows else None

    def stream_candles(self, symbol: Optional[str] = None, *,
                       timeframe: "int | str | None" = None,
                       callback: Optional[Callable[[Any], None]] = None) -> Any:
        """Subscribe to live candles; ``callback(candle)`` on every update."""
        return self._iq.market.subscribe_candles(self._symbol(symbol),
                                                 self._tf(timeframe), callback)

    # ------------------------------------------------------------------
    # ticks / prices
    # ------------------------------------------------------------------
    def price(self, symbol: Optional[str] = None) -> Any:
        """Current quote of the symbol."""
        return self._iq.price(self._symbol(symbol))

    def bid_ask(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        return self._iq.bid_ask(self._symbol(symbol))

    def stream_ticks(self, symbol: Optional[str] = None,
                     callback: Optional[Callable[[Any], None]] = None) -> Any:
        """Subscribe to live ticks; ``callback(tick)`` on every tick."""
        return self._iq.subscribe_ticks(self._symbol(symbol), callback)

    def traders_mood(self, symbol: Optional[str] = None,
                     callback: Optional[Callable[[Any], None]] = None) -> Any:
        """Live buyer/seller sentiment stream."""
        return self._iq.subscribe_traders_mood(self._symbol(symbol),
                                               callback=callback)

    # ------------------------------------------------------------------
    # time
    # ------------------------------------------------------------------
    def server_time(self) -> float:
        return self._iq.server_time

    def sync_time(self) -> float:
        return self._iq.sync_time()
