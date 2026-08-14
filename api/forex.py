"""forex — everything forex trading.

Trade place, buy/sell, SL/TP set, track, leverage set, amount set.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class Forex:
    """Leveraged forex control."""

    def __init__(self, client: Any, state: Any) -> None:
        self._iq = client
        self._state = state

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def set_amount(self, amount: float) -> float:
        self._state.forex_amount = float(amount)
        return self._state.forex_amount

    def get_amount(self) -> float:
        return self._state.forex_amount

    def set_leverage(self, leverage: int) -> int:
        """Leverage used by every forex trade (e.g. 50, 100, 500)."""
        self._state.forex_leverage = int(leverage)
        return self._state.forex_leverage

    def get_leverage(self) -> Optional[int]:
        return self._state.forex_leverage

    def _symbol(self, symbol: Optional[str]) -> str:
        return str(symbol or self._state.symbol).upper()

    def _leverage(self, leverage: Optional[int]) -> Optional[int]:
        return int(leverage) if leverage is not None else self._state.forex_leverage

    # ------------------------------------------------------------------
    # market info
    # ------------------------------------------------------------------
    def pairs(self, *, only_open: bool = False) -> List[str]:
        return self._iq.forex.currency_pairs(only_open=only_open)

    def is_open(self, symbol: Optional[str] = None) -> bool:
        return self._iq.forex.is_open(self._symbol(symbol))

    def price(self, symbol: Optional[str] = None) -> Any:
        return self._iq.forex.price(self._symbol(symbol))

    def bid_ask(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        return self._iq.forex.bid_ask(self._symbol(symbol))

    def leverages(self, symbol: Optional[str] = None) -> List[int]:
        """Leverages the platform allows for this pair."""
        return self._iq.forex.leverages(self._symbol(symbol))

    def market_info(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        return self._iq.forex.market_info(self._symbol(symbol))

    # ------------------------------------------------------------------
    # trade place  (buy / sell with SL/TP)
    # ------------------------------------------------------------------
    def buy(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
            stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
            leverage: Optional[int] = None) -> Any:
        """Open a LONG position.  SL/TP are absolute prices."""
        return self._iq.forex.buy(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.forex_amount),
            stop_loss=stop_loss, take_profit=take_profit,
            leverage=self._leverage(leverage),
        )

    def sell(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
             stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
             leverage: Optional[int] = None) -> Any:
        """Open a SHORT position.  SL/TP are absolute prices."""
        return self._iq.forex.sell(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.forex_amount),
            stop_loss=stop_loss, take_profit=take_profit,
            leverage=self._leverage(leverage),
        )

    def buy_pips(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
                 sl_pips: Optional[float] = None, tp_pips: Optional[float] = None,
                 leverage: Optional[int] = None) -> Any:
        """LONG with SL/TP given as pip distances."""
        return self._iq.forex.buy_with_pips(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.forex_amount),
            stop_loss_pips=sl_pips, take_profit_pips=tp_pips,
            leverage=self._leverage(leverage),
        )

    def sell_pips(self, *, symbol: Optional[str] = None, amount: Optional[float] = None,
                  sl_pips: Optional[float] = None, tp_pips: Optional[float] = None,
                  leverage: Optional[int] = None) -> Any:
        """SHORT with SL/TP given as pip distances."""
        return self._iq.forex.sell_with_pips(
            self._symbol(symbol),
            float(amount if amount is not None else self._state.forex_amount),
            stop_loss_pips=sl_pips, take_profit_pips=tp_pips,
            leverage=self._leverage(leverage),
        )

    # ------------------------------------------------------------------
    # SL / TP on an open position
    # ------------------------------------------------------------------
    def set_sl_tp(self, position_id: int, *, stop_loss: Optional[float] = None,
                  take_profit: Optional[float] = None) -> Any:
        """Change SL/TP of an open position (absolute prices)."""
        return self._iq.forex.set_sl_tp(int(position_id),
                                        stop_loss=stop_loss, take_profit=take_profit)

    def set_stop_loss(self, position_id: int, stop_loss: float) -> Any:
        return self.set_sl_tp(position_id, stop_loss=float(stop_loss))

    def set_take_profit(self, position_id: int, take_profit: float) -> Any:
        return self.set_sl_tp(position_id, take_profit=float(take_profit))

    # ------------------------------------------------------------------
    # track / close
    # ------------------------------------------------------------------
    def track(self, order: Any) -> Optional[Any]:
        """Live position of an order (``None`` until it lands)."""
        return self._iq.forex.position_of_order(order)

    def position(self, position_id: int) -> Any:
        return self._iq.forex.get_position(int(position_id))

    def pnl(self, position_id: int) -> Optional[float]:
        """Floating profit/loss of an open position."""
        return self._iq.forex.floating_pnl(int(position_id))

    def open_trades(self) -> List[Any]:
        return self._iq.forex.open_positions()

    def close(self, position_id: int) -> bool:
        return self._iq.forex.close_position(int(position_id))

    def close_all(self) -> int:
        return self._iq.forex.close_all()

    def history(self, limit: int = 50) -> List[Any]:
        return self._iq.forex.history(limit)
