"""digital — everything digital options.

Trade place, amount set, call/put, track, results.
"""

from __future__ import annotations

from typing import Any, List, Optional


class Digital:
    """Digital options control."""

    def __init__(self, client: Any, state: Any) -> None:
        self._iq = client
        self._state = state

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def set_amount(self, amount: float) -> float:
        self._state.digital_amount = float(amount)
        return self._state.digital_amount

    def get_amount(self) -> float:
        return self._state.digital_amount

    def set_duration(self, minutes: int) -> int:
        """Expiry in minutes (1, 5, 15)."""
        self._state.digital_duration = int(minutes)
        return self._state.digital_duration

    def get_duration(self) -> int:
        return self._state.digital_duration

    def _symbol(self, symbol: Optional[str]) -> str:
        return str(symbol or self._state.symbol).upper()

    # ------------------------------------------------------------------
    # market info
    # ------------------------------------------------------------------
    def assets(self, *, only_open: bool = False) -> List[Any]:
        return self._iq.digital.assets(only_open=only_open, refresh=True)

    def is_open(self, symbol: Optional[str] = None) -> bool:
        return self._iq.digital.is_open(self._symbol(symbol))

    def payout(self, symbol: Optional[str] = None, direction: str = "call",
               *, duration: Optional[int] = None) -> Optional[float]:
        """Profit percent for the ATM strike."""
        minutes = int(duration if duration is not None else self._state.digital_duration)
        return self._iq.digital.payout(self._symbol(symbol), direction,
                                       period=minutes * 60)

    def strikes(self, symbol: Optional[str] = None,
                *, duration: Optional[int] = None) -> List[Any]:
        minutes = int(duration if duration is not None else self._state.digital_duration)
        return self._iq.digital.strikes(self._symbol(symbol), period=minutes * 60)

    # ------------------------------------------------------------------
    # trade place
    # ------------------------------------------------------------------
    def buy(self, direction: str, *, symbol: Optional[str] = None,
            amount: Optional[float] = None, duration: Optional[int] = None,
            strike: Optional[float] = None) -> Any:
        """Place a digital trade.  ``direction`` = ``"call"`` / ``"put"``,
        ``duration`` in minutes, optional fixed ``strike`` (default ATM)."""
        return self._iq.digital.buy(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.digital_amount),
            direction,
            duration=int(duration if duration is not None else self._state.digital_duration),
            strike=strike,
        )

    place = buy

    def call(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
             duration: Optional[int] = None, strike: Optional[float] = None) -> Any:
        return self.buy("call", symbol=symbol, amount=amount,
                        duration=duration, strike=strike)

    def put(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
            duration: Optional[int] = None, strike: Optional[float] = None) -> Any:
        return self.buy("put", symbol=symbol, amount=amount,
                        duration=duration, strike=strike)

    # ------------------------------------------------------------------
    # track / results
    # ------------------------------------------------------------------
    def track(self, order: Any) -> Optional[Any]:
        """Live position of an order (``None`` until it lands)."""
        order_id = getattr(order, "order_id", order)
        return (self._iq.positions.by_order_id(order_id)
                or self._iq.positions.get(order_id))

    def result(self, order: Any, *, timeout: float = 300.0) -> Any:
        """Block until expiry; returns win/loss/equal + pnl."""
        return self._iq.digital.check_result(order, timeout=timeout)

    def trade_and_wait(self, direction: str, *, symbol: Optional[str] = None,
                       amount: Optional[float] = None,
                       duration: Optional[int] = None) -> Any:
        """Place + wait for the outcome in one call."""
        return self._iq.digital.buy_and_wait(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.digital_amount),
            direction,
            duration=int(duration if duration is not None else self._state.digital_duration),
        )

    def close_early(self, position_id: int) -> bool:
        """Sell a digital position before expiry."""
        return self._iq.digital.close(int(position_id))

    def open_trades(self) -> List[Any]:
        return self._iq.digital.open_positions()

    def history(self, limit: int = 50) -> List[Any]:
        return self._iq.digital.history(limit)
