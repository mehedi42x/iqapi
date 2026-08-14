"""binary — everything binary/turbo trading.

Trade place, amount set, call/put, trade track, results.
"""

from __future__ import annotations

from typing import Any, List, Optional


class Binary:
    """Binary (and turbo) options control."""

    def __init__(self, client: Any, state: Any) -> None:
        self._iq = client
        self._state = state

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def set_amount(self, amount: float) -> float:
        self._state.binary_amount = float(amount)
        return self._state.binary_amount

    def get_amount(self) -> float:
        return self._state.binary_amount

    def set_duration(self, minutes: int) -> int:
        """Expiry in minutes (1, 2, 3, 5, 15, ...)."""
        self._state.binary_duration = int(minutes)
        return self._state.binary_duration

    def get_duration(self) -> int:
        return self._state.binary_duration

    def _symbol(self, symbol: Optional[str]) -> str:
        return str(symbol or self._state.symbol).upper()

    # ------------------------------------------------------------------
    # market info
    # ------------------------------------------------------------------
    def assets(self, *, turbo: bool = False, only_open: bool = False) -> List[Any]:
        return self._iq.binary.assets(turbo=turbo, only_open=only_open, refresh=True)

    def is_open(self, symbol: Optional[str] = None, *, turbo: bool = False) -> bool:
        return self._iq.binary.is_open(self._symbol(symbol), turbo=turbo)

    def payout(self, symbol: Optional[str] = None, *, turbo: bool = False) -> Optional[float]:
        """Profit percent, e.g. ``87.0`` means +87%."""
        return self._iq.binary.payout(self._symbol(symbol), turbo=turbo)

    # ------------------------------------------------------------------
    # trade place
    # ------------------------------------------------------------------
    def buy(self, direction: str, *, symbol: Optional[str] = None,
            amount: Optional[float] = None, duration: Optional[int] = None,
            turbo: bool = False) -> Any:
        """Place a binary trade.  ``direction`` = ``"call"`` / ``"put"``,
        ``duration`` in minutes."""
        return self._iq.binary.buy(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.binary_amount),
            direction,
            duration=int(duration if duration is not None else self._state.binary_duration),
            turbo=turbo,
        )

    place = buy

    def call(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
             duration: Optional[int] = None, turbo: bool = False) -> Any:
        return self.buy("call", symbol=symbol, amount=amount,
                        duration=duration, turbo=turbo)

    def put(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
            duration: Optional[int] = None, turbo: bool = False) -> Any:
        return self.buy("put", symbol=symbol, amount=amount,
                        duration=duration, turbo=turbo)

    # ------------------------------------------------------------------
    # track / results
    # ------------------------------------------------------------------
    def track(self, order: Any) -> Optional[Any]:
        """Live position of an order (``None`` until it lands)."""
        return self._iq.binary.position_of(order)

    def result(self, order: Any, *, timeout: float = 300.0) -> Any:
        """Block until expiry; returns win/loss/equal + pnl."""
        return self._iq.binary.check_result(order, timeout=timeout)

    def trade_and_wait(self, direction: str, *, symbol: Optional[str] = None,
                       amount: Optional[float] = None,
                       duration: Optional[int] = None, turbo: bool = False) -> Any:
        """Place + wait for the outcome in one call."""
        return self._iq.binary.buy_and_wait(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.binary_amount),
            direction,
            duration=int(duration if duration is not None else self._state.binary_duration),
            turbo=turbo,
        )

    def open_trades(self) -> List[Any]:
        return self._iq.binary.open_positions()

    def history(self, limit: int = 50) -> List[Any]:
        return self._iq.binary.history(limit)
