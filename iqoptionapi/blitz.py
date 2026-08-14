"""blitz — everything blitz options.

Amount set, duration set, call/put, trade place, track, result.
"""

from __future__ import annotations

from typing import Any, List, Optional


class Blitz:
    """Blitz options control (5s..60s expiries)."""

    def __init__(self, client: Any, state: Any) -> None:
        self._iq = client
        self._state = state

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def set_amount(self, amount: float) -> float:
        self._state.blitz_amount = float(amount)
        return self._state.blitz_amount

    def get_amount(self) -> float:
        return self._state.blitz_amount

    def set_duration(self, seconds: int) -> int:
        """Expiry in seconds (5, 10, 15, 30, 60 — asset dependent)."""
        self._state.blitz_duration = int(seconds)
        return self._state.blitz_duration

    def get_duration(self) -> int:
        return self._state.blitz_duration

    def _symbol(self, symbol: Optional[str]) -> str:
        return str(symbol or self._state.symbol).upper()

    # ------------------------------------------------------------------
    # market info
    # ------------------------------------------------------------------
    def assets(self, *, only_open: bool = False) -> List[Any]:
        return self._iq.blitz.assets(only_open=only_open, refresh=True)

    def is_open(self, symbol: Optional[str] = None) -> bool:
        return self._iq.blitz.is_open(self._symbol(symbol))

    def durations(self, symbol: Optional[str] = None) -> List[int]:
        """Valid expiries (seconds) the platform offers for this asset."""
        return self._iq.blitz.durations(self._symbol(symbol))

    def payout(self, symbol: Optional[str] = None) -> Optional[float]:
        """Profit percent, e.g. ``80.0`` means +80%."""
        return self._iq.blitz.payout(self._symbol(symbol))

    # ------------------------------------------------------------------
    # trade place
    # ------------------------------------------------------------------
    def buy(self, direction: str, *, symbol: Optional[str] = None,
            amount: Optional[float] = None,
            duration: Optional[int] = None) -> Any:
        """Place a blitz trade.  ``direction`` = ``"call"`` / ``"put"``."""
        return self._iq.blitz.buy(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.blitz_amount),
            direction,
            int(duration if duration is not None else self._state.blitz_duration),
        )

    place = buy

    def call(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
             duration: Optional[int] = None) -> Any:
        return self.buy("call", symbol=symbol, amount=amount, duration=duration)

    def put(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
            duration: Optional[int] = None) -> Any:
        return self.buy("put", symbol=symbol, amount=amount, duration=duration)

    # ------------------------------------------------------------------
    # track / results
    # ------------------------------------------------------------------
    def track(self, order: Any) -> Optional[Any]:
        """Live position of an order (``None`` until it lands)."""
        return self._iq.blitz.position_of(order)

    def result(self, order: Any, *, timeout: float = 120.0) -> Any:
        """Block until the trade settles; returns win/loss/equal + pnl."""
        return self._iq.blitz.check_result(order, timeout=timeout)

    def trade_and_wait(self, direction: str, *, symbol: Optional[str] = None,
                       amount: Optional[float] = None,
                       duration: Optional[int] = None) -> Any:
        """Place + wait for the outcome in one call."""
        return self._iq.blitz.buy_and_wait(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.blitz_amount),
            direction,
            int(duration if duration is not None else self._state.blitz_duration),
        )

    def open_trades(self) -> List[Any]:
        return self._iq.blitz.open_positions()

    def history(self, limit: int = 50) -> List[Any]:
        return self._iq.blitz.history(limit)
