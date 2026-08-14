"""Balance management.

Pulls balances from ``get-balances`` and keeps them fresh via
the ``balance-changed`` stream event.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from ..connection.protocol import MS_GET_BALANCES
from ..connection.websocket import WebSocketClient
from ..exceptions import BalanceError, ProtocolError
from ..models import AccountType, Balance


class BalanceManager:
    """Server-side truth about every balance the user owns."""

    EVENT_BALANCE_CHANGED = "balance-changed"

    def __init__(self, client: WebSocketClient, logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.log = logger or logging.getLogger("iq_option_api.balance")
        self._balances: Dict[int, Balance] = {}
        self._lock = threading.RLock()
        self._subscription = None
        self._listeners: List[Callable[[Balance], None]] = []

    # ------------------------------------------------------------------
    def refresh(self, *, timeout: Optional[float] = None,
                types_ids: Optional[List[int]] = None) -> List[Balance]:
        """Fetch balances from the server.  Always the source of truth."""
        body: Any = {"types_ids": types_ids} if types_ids else ""
        payload = self.ws.call(MS_GET_BALANCES, body, version="1.0", timeout=timeout)

        items = payload
        if isinstance(payload, dict):
            # IQ Option has returned all of these wrappers over time:
            # balances, items, result and data.  Accept only a list so a
            # successful-but-different response cannot silently look empty.
            items = payload.get("balances")
            if items is None:
                items = payload.get("items")
            if items is None:
                items = payload.get("result")
            if items is None:
                items = payload.get("data")
            if items is None and any(k in payload for k in ("id", "balance_id")):
                items = [payload]
        if not isinstance(items, list):
            raise ProtocolError("unexpected get-balances payload", details=payload)

        balances = [Balance.from_payload(item) for item in items if isinstance(item, dict)]
        with self._lock:
            self._balances = {b.balance_id: b for b in balances}
        self.log.debug("loaded %s balances", len(balances))
        return balances

    def all(self, *, refresh: bool = False) -> List[Balance]:
        with self._lock:
            cached = list(self._balances.values())
        if refresh or not cached:
            return self.refresh()
        return cached

    def get(self, balance_id: int, *, refresh: bool = False) -> Balance:
        if refresh:
            self.refresh()
        with self._lock:
            balance = self._balances.get(int(balance_id))
        if balance is None:
            self.refresh()
            with self._lock:
                balance = self._balances.get(int(balance_id))
        if balance is None:
            raise BalanceError(f"balance_id {balance_id} not found on the server")
        return balance

    def by_type(self, account_type: AccountType, *, refresh: bool = False) -> Optional[Balance]:
        for balance in self.all(refresh=refresh):
            if balance.type is account_type:
                return balance
        return None

    def amount(self, balance_id: int, *, refresh: bool = True) -> float:
        return self.get(balance_id, refresh=refresh).amount

    def currency(self, balance_id: int) -> str:
        return self.get(balance_id).currency

    # ------------------------------------------------------------------
    def subscribe_updates(self, callback: Optional[Callable[[Balance], None]] = None):
        """Keep the cache live through the ``balance-changed`` event."""
        if callback:
            self._listeners.append(callback)
        if self._subscription is not None:
            return self._subscription

        def _handler(payload: Any) -> None:
            data = payload
            if isinstance(payload, dict) and "current_balance" in payload:
                data = payload["current_balance"]
            if not isinstance(data, dict):
                return
            balance = Balance.from_payload(data)
            if not balance.balance_id:
                return
            with self._lock:
                previous = self._balances.get(balance.balance_id)
                if previous is not None and not data.get("currency"):
                    balance.currency = previous.currency
                    balance.type = previous.type
                self._balances[balance.balance_id] = balance
            for listener in list(self._listeners):
                try:
                    listener(balance)
                except Exception as exc:  # pragma: no cover
                    self.log.warning("balance listener failed: %s", exc)

        self._subscription = self.ws.subscribe(self.EVENT_BALANCE_CHANGED, callback=_handler)
        return self._subscription

    def unsubscribe_updates(self) -> None:
        if self._subscription is not None:
            self.ws.unsubscribe(self._subscription.subscription_id)
            self._subscription = None
        self._listeners.clear()

    def cached(self) -> Dict[int, Balance]:
        with self._lock:
            return dict(self._balances)
