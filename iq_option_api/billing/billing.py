"""Billing / balance information.

**Important separation of concerns.**  This module exposes the raw billing
balances returned by ``internal-billing.get-balances``.  They are *not* mixed
into the trading account balance: :class:`~iq_option_api.account.AccountManager`
remains the only source of truth for the account used to place orders.
Tournament, promo and internal balances are reported here for information
only and are never selected automatically for trading.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..account.balance import BalanceManager
from ..connection.protocol import MS_GET_BALANCES
from ..connection.websocket import WebSocketClient
from ..exceptions import BalanceError
from ..models import AccountType, Balance

#: type ids that are *not* regular tradable accounts
NON_TRADING_TYPE_IDS = (2, 5, 6)


class BillingManager:
    """Read-only view of every balance owned by the user."""

    def __init__(self, client: WebSocketClient,
                 balances: Optional[BalanceManager] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.log = logger or logging.getLogger("iq_option_api.billing")
        self.balances = balances or BalanceManager(client, logger=self.log)
        self._raw: List[Dict[str, Any]] = []
        self._fetched_at: float = 0.0

    # ==================================================================
    # Fetching
    # ==================================================================
    def get_balances(self, *, refresh: bool = True, max_age: float = 5.0,
                     timeout: Optional[float] = None) -> List[Balance]:
        """``internal-billing.get-balances`` - every balance, unfiltered."""
        if not refresh and self._raw and time.time() - self._fetched_at < max_age:
            return [Balance.from_payload(item) for item in self._raw]

        payload = self.ws.call(MS_GET_BALANCES, {}, version="1.0", timeout=timeout)
        items = payload if isinstance(payload, list) else (
            payload.get("balances") or payload.get("items") or []
            if isinstance(payload, dict) else [])
        if not isinstance(items, list):
            raise BalanceError("unexpected internal-billing.get-balances payload",
                               details={"payload": payload})
        self._raw = [i for i in items if isinstance(i, dict)]
        self._fetched_at = time.time()
        self.log.debug("billing: %d balances", len(self._raw))
        return [Balance.from_payload(item) for item in self._raw]

    def raw_balances(self) -> List[Dict[str, Any]]:
        if not self._raw:
            self.get_balances()
        return list(self._raw)

    # ==================================================================
    # Selection helpers (informational only)
    # ==================================================================
    def by_id(self, balance_id: int, *, refresh: bool = False) -> Balance:
        for balance in self.get_balances(refresh=refresh):
            if balance.balance_id == int(balance_id):
                return balance
        raise BalanceError(f"balance {balance_id} not found")

    def by_type(self, account_type: AccountType, *, refresh: bool = False) -> List[Balance]:
        return [b for b in self.get_balances(refresh=refresh) if b.type is account_type]

    def real_balances(self) -> List[Balance]:
        return self.by_type(AccountType.REAL)

    def practice_balances(self) -> List[Balance]:
        return self.by_type(AccountType.PRACTICE)

    def tournament_balances(self) -> List[Balance]:
        return self.by_type(AccountType.TOURNAMENT)

    def internal_balances(self) -> List[Balance]:
        """Balances that are not standard tradable accounts."""
        return [b for b in self.get_balances(refresh=False)
                if b.type_id in NON_TRADING_TYPE_IDS]

    def promo_balances(self) -> List[Balance]:
        return [b for b in self.get_balances(refresh=False)
                if bool(b.raw.get("is_promo") or b.raw.get("promo"))]

    def tradable_balances(self) -> List[Balance]:
        return [b for b in self.get_balances(refresh=False)
                if b.type in (AccountType.REAL, AccountType.PRACTICE)]

    # ==================================================================
    # Amounts
    # ==================================================================
    def amount(self, balance_id: int, *, refresh: bool = True) -> float:
        return self.by_id(balance_id, refresh=refresh).amount

    def currency(self, balance_id: int) -> str:
        return self.by_id(balance_id).currency

    def total_by_currency(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for balance in self.get_balances(refresh=False):
            if balance.currency:
                out[balance.currency] = out.get(balance.currency, 0.0) + balance.amount
        return out

    # ==================================================================
    # Streaming
    # ==================================================================
    def subscribe_updates(self, callback: Optional[Callable[[Balance], None]] = None):
        """Live ``balance-changed`` updates (delegated to BalanceManager)."""
        return self.balances.subscribe_updates(callback)

    def unsubscribe_updates(self) -> None:
        self.balances.unsubscribe_updates()

    # ==================================================================
    # Reporting
    # ==================================================================
    def report(self) -> Dict[str, Any]:
        balances = self.get_balances(refresh=True)
        return {
            "count": len(balances),
            "by_currency": self.total_by_currency(),
            "balances": [
                {
                    "balance_id": b.balance_id,
                    "type": b.type.value,
                    "type_id": b.type_id,
                    "amount": b.amount,
                    "currency": b.currency,
                    "is_fiat": b.is_fiat,
                    "is_marginal": b.is_marginal,
                    "tradable": b.type in (AccountType.REAL, AccountType.PRACTICE),
                }
                for b in balances
            ],
            "note": "billing data - not the active trading balance",
        }
