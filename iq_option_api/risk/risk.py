"""Risk management.

Every order passes through :meth:`RiskManager.check_order` before it is sent
(``OrderManager.validate`` calls it).  The manager enforces:

* balance sufficiency and min/max trade amount
* maximum total exposure and maximum open positions
* account / instrument / market validation
* duplicate-order protection and order frequency limits
* explicit opt-in for real-account trading
* an emergency kill switch that blocks all trading
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from ..config import TradingLimits
from ..exceptions import AccountError, MarketError, OrderError
from ..models import AccountType, InstrumentType, Order


class RiskManager:
    """Pre-trade risk validation and trading kill switch."""

    def __init__(self, limits: Optional[TradingLimits] = None,
                 *, accounts: Any = None, market: Any = None,
                 positions: Any = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.limits = limits or TradingLimits()
        self.accounts = accounts
        self.market = market
        self.positions = positions
        self.log = logger or logging.getLogger("iq_option_api.risk")

        self._lock = threading.RLock()
        self._order_times: Deque[float] = deque(maxlen=500)
        self._recent: Dict[str, float] = {}
        self._disabled = not self.limits.trading_enabled
        self._disabled_reason = "" if self.limits.trading_enabled else "disabled by configuration"
        self._blocked_assets: set = set()
        self._session_loss = 0.0

    # ==================================================================
    # Kill switch
    # ==================================================================
    @property
    def trading_enabled(self) -> bool:
        return not self._disabled

    def disable_trading(self, reason: str = "manually disabled") -> None:
        """Emergency stop - every subsequent order is rejected."""
        with self._lock:
            self._disabled = True
            self._disabled_reason = reason
        self.log.warning("TRADING DISABLED: %s", reason)

    emergency_stop = disable_trading

    def enable_trading(self) -> None:
        with self._lock:
            self._disabled = False
            self._disabled_reason = ""
        self.log.info("trading enabled")

    def block_asset(self, asset_id: int) -> None:
        self._blocked_assets.add(int(asset_id))

    def unblock_asset(self, asset_id: int) -> None:
        self._blocked_assets.discard(int(asset_id))

    # ==================================================================
    # Main entry point
    # ==================================================================
    def check_order(self, order: Order, *, balance: Optional[float] = None) -> Order:
        """Raise :class:`OrderError` (or a more specific error) if unsafe."""
        with self._lock:
            if self._disabled:
                raise OrderError(f"trading is disabled: {self._disabled_reason}")

        self.check_account(order)
        self.check_amount(order.amount, balance=balance)
        self.check_asset(order)
        self.check_market(order)
        self.check_exposure(order, balance=balance)
        self.check_open_positions()
        self.check_frequency()
        self.check_duplicate(order)
        self._register(order)
        return order

    # ==================================================================
    # Individual checks
    # ==================================================================
    def check_amount(self, amount: float, *, balance: Optional[float] = None) -> None:
        amount = float(amount)
        if amount <= 0:
            raise OrderError("trade amount must be greater than zero")
        if amount < self.limits.min_amount:
            raise OrderError(
                f"amount {amount} below minimum {self.limits.min_amount}",
                details={"min_amount": self.limits.min_amount})
        if amount > self.limits.max_amount:
            raise OrderError(
                f"amount {amount} above maximum {self.limits.max_amount}",
                details={"max_amount": self.limits.max_amount})
        if balance is not None and amount > balance:
            raise OrderError(
                f"insufficient balance: need {amount}, have {balance}",
                details={"balance": balance, "amount": amount})

    def check_account(self, order: Order) -> None:
        if order.balance_id in (None, 0):
            raise AccountError("no user_balance_id on the order - select an account first")
        if self.accounts is None:
            return
        try:
            account_type = self.accounts.account_type
        except Exception:
            return
        if account_type is AccountType.REAL and not self.limits.allow_real_account_trading:
            raise OrderError(
                "real-account trading is disabled - set limits.allow_real_account_trading=True "
                "to trade with real money",
                details={"balance_id": order.balance_id})
        if account_type is AccountType.UNKNOWN:
            raise AccountError("active account type unknown - refresh the account list")

    def check_asset(self, order: Order) -> None:
        if order.asset_id in self._blocked_assets:
            raise OrderError(f"asset {order.asset_id} is blocked by risk management")
        if order.instrument_type is InstrumentType.UNKNOWN:
            raise OrderError("order has an unknown instrument type")

    def check_market(self, order: Order) -> None:
        if self.market is None or not order.asset_id:
            return
        try:
            is_open = self.market.is_open(order.asset_id, order.instrument_type)
        except Exception as exc:
            self.log.debug("market check skipped: %s", exc)
            return
        if not is_open:
            raise MarketError(
                f"market closed for {order.symbol or order.asset_id}",
                details={"asset_id": order.asset_id,
                         "instrument_type": order.instrument_type.value})

    def check_exposure(self, order: Order, *, balance: Optional[float] = None) -> None:
        limit = self.limits.max_exposure
        pct = self.limits.max_exposure_pct_of_balance
        if self.positions is None or (not limit and not pct):
            return
        try:
            current = sum(p.invest or 0.0 for p in self.positions.open_positions())
        except Exception:
            return
        projected = current + order.amount

        if limit and projected > limit:
            raise OrderError(
                f"max exposure exceeded: {projected:.2f} > {limit:.2f}",
                details={"current_exposure": current, "limit": limit})

        if pct:
            if balance is None and self.accounts is not None:
                try:
                    balance = self.accounts.balance(refresh=False)
                except Exception:
                    balance = None
            if balance:
                allowed = float(balance) * pct / 100.0
                if projected > allowed:
                    raise OrderError(
                        f"max exposure exceeded: {projected:.2f} > {allowed:.2f} "
                        f"({pct}% of balance)",
                        details={"current_exposure": current, "limit": allowed,
                                 "balance": balance})

    def check_open_positions(self) -> None:
        limit = self.limits.max_open_positions
        if not limit or self.positions is None:
            return
        try:
            count = len(self.positions.open_positions())
        except Exception:
            return
        if count >= limit:
            raise OrderError(f"max open positions reached ({count}/{limit})")

    def check_frequency(self) -> None:
        limit = self.limits.max_orders_per_minute
        if not limit:
            return
        now = time.time()
        with self._lock:
            while self._order_times and now - self._order_times[0] > 60.0:
                self._order_times.popleft()
            if len(self._order_times) >= limit:
                raise OrderError(
                    f"order rate limit reached: {limit} orders/minute",
                    details={"window_orders": len(self._order_times)})

    def check_duplicate(self, order: Order) -> None:
        window = self.limits.duplicate_window
        if not window:
            return
        key = self._key(order)
        now = time.time()
        with self._lock:
            last = self._recent.get(key)
            if last is not None and now - last < window:
                raise OrderError(
                    "duplicate order blocked "
                    f"(same order within {window}s)",
                    details={"key": key, "since": now - last})

    # ==================================================================
    # Bookkeeping
    # ==================================================================
    def _register(self, order: Order) -> None:
        now = time.time()
        with self._lock:
            self._order_times.append(now)
            self._recent[self._key(order)] = now
            if len(self._recent) > 500:
                cutoff = now - 300
                self._recent = {k: v for k, v in self._recent.items() if v > cutoff}

    @staticmethod
    def _key(order: Order) -> str:
        return "|".join([
            str(order.balance_id),
            order.instrument_type.value,
            str(order.asset_id),
            order.direction.value if order.direction else "?",
            f"{order.amount:.4f}",
            order.instrument_id or "",
        ])

    # ==================================================================
    # Position sizing helpers
    # ==================================================================
    def max_allowed_amount(self, balance: float) -> float:
        return max(0.0, min(float(balance), self.limits.max_amount))

    def suggest_amount(self, balance: float, risk_percent: float = 1.0) -> float:
        amount = float(balance) * float(risk_percent) / 100.0
        amount = max(self.limits.min_amount, min(amount, self.limits.max_amount))
        return round(min(amount, float(balance)), 2)

    def validate_amount(self, amount: float, balance: Optional[float] = None) -> bool:
        try:
            self.check_amount(amount, balance=balance)
            return True
        except OrderError:
            return False

    # ==================================================================
    # Reporting
    # ==================================================================
    def status(self) -> Dict[str, Any]:
        with self._lock:
            recent = len(self._order_times)
        exposure = None
        open_count = None
        if self.positions is not None:
            try:
                open_positions = self.positions.open_positions()
                exposure = sum(p.invest or 0.0 for p in open_positions)
                open_count = len(open_positions)
            except Exception:
                pass
        return {
            "trading_enabled": self.trading_enabled,
            "disabled_reason": self._disabled_reason,
            "orders_last_minute": recent,
            "open_positions": open_count,
            "exposure": exposure,
            "blocked_assets": sorted(self._blocked_assets),
            "limits": {
                "min_amount": self.limits.min_amount,
                "max_amount": self.limits.max_amount,
                "max_open_positions": self.limits.max_open_positions,
                "max_exposure": self.limits.max_exposure,
                "max_exposure_pct_of_balance": self.limits.max_exposure_pct_of_balance,
                "max_orders_per_minute": self.limits.max_orders_per_minute,
                "allow_real_account_trading": self.limits.allow_real_account_trading,
            },
        }
