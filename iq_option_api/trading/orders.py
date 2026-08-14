"""Order management - the common layer for every order type.

Creation -> validation -> submission -> tracking -> cancel/modify/close.
Every trading module funnels its orders through :class:`OrderManager` so the
risk checks, the order registry and the history are shared.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..connection.websocket import WebSocketClient
from ..exceptions import OrderError
from ..models import Direction, Instrument, InstrumentType, Order, OrderState, OrderType


class OrderManager:
    """Registry + submission helper for all orders."""

    def __init__(self, client: WebSocketClient,
                 risk_manager: Any = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.risk = risk_manager
        self.log = logger or logging.getLogger("iq_option_api.orders")
        self._orders: Dict[int, Order] = {}
        self._by_request: Dict[str, Order] = {}
        self._lock = threading.RLock()
        self._listeners: List[Callable[[Order], None]] = []

    # ==================================================================
    # Creation / validation
    # ==================================================================
    def create(self, *, instrument: Instrument, direction: Direction, amount: float,
               balance_id: int, order_type: OrderType = OrderType.MARKET,
               stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
               leverage: Optional[int] = None, limit_price: Optional[float] = None,
               stop_price: Optional[float] = None) -> Order:
        return Order(
            instrument_id=instrument.instrument_id,
            instrument_type=instrument.instrument_type,
            asset_id=instrument.asset_id,
            symbol=instrument.symbol,
            direction=direction,
            amount=float(amount),
            order_type=order_type,
            state=OrderState.CREATED,
            limit_price=limit_price,
            stop_price=stop_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=leverage or instrument.leverage,
            balance_id=int(balance_id),
        )

    def validate(self, order: Order, *, balance: Optional[float] = None) -> Order:
        """Local validation before anything hits the network."""
        if order.amount <= 0:
            raise OrderError("order amount must be > 0")
        if order.direction is None:
            raise OrderError("order direction is required")
        if order.balance_id in (None, 0):
            raise OrderError("order has no user_balance_id - select an account first")
        if order.order_type is OrderType.LIMIT and order.limit_price is None:
            raise OrderError("limit order requires limit_price")
        if order.order_type is OrderType.STOP and order.stop_price is None:
            raise OrderError("stop order requires stop_price")
        if self.risk is not None:
            self.risk.check_order(order, balance=balance)
        return order

    # ==================================================================
    # Submission
    # ==================================================================
    def submit(self, order: Order, microservice: str, body: Dict[str, Any],
               *, version: str = "1.0", timeout: Optional[float] = None,
               matcher: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Order:
        """Send an order and merge the server reply back into the model.

        ``matcher`` is an optional fallback correlator: option microservices
        sometimes answer by *broadcasting* ``option-opened`` / ``option-rejected``
        without echoing our ``request_id``, and without it the call would sit
        there until the request timeout fired.
        """
        self.log.info("placing %s order: %s %s amount=%s balance=%s",
                      order.instrument_type.value, order.symbol or order.asset_id,
                      order.direction.value if order.direction else "?",
                      order.amount, order.balance_id)
        try:
            payload = self.ws.call(microservice, body, version=version,
                                   timeout=timeout, matcher=matcher)
        except Exception as exc:
            order.state = OrderState.REJECTED
            order.message = str(exc)
            self._register(order)
            raise OrderError(f"order submission failed: {exc}", details=body) from exc

        return self.apply_response(order, payload)

    def apply_response(self, order: Order, payload: Any) -> Order:
        data = payload if isinstance(payload, dict) else {}

        # error shapes used by the different microservices
        if data.get("error") or data.get("message") and not data.get("id"):
            reason = data.get("error") or data.get("message")
            if data.get("success") is not False and data.get("id"):
                pass
            else:
                order.state = OrderState.REJECTED
                order.message = str(reason)
                self._register(order)
                raise OrderError(f"order rejected: {reason}{self._hint(reason)}",
                                 details=data)

        inner = data.get("result") if isinstance(data.get("result"), dict) else data
        order = Order.from_payload(inner, **{
            "instrument_id": order.instrument_id,
            "instrument_type": order.instrument_type,
            "asset_id": order.asset_id,
            "symbol": order.symbol,
            "direction": order.direction,
            "amount": order.amount,
            "order_type": order.order_type,
            "balance_id": order.balance_id,
            "stop_loss": order.stop_loss,
            "take_profit": order.take_profit,
            "leverage": order.leverage,
            "created_at": order.created_at,
        })
        if order.order_id is None:
            for key in ("id", "order_id", "position_id", "external_id"):
                if inner.get(key):
                    order.order_id = int(inner[key])
                    break
        if order.order_id is None:
            order.state = OrderState.REJECTED
            order.message = "server did not return an order id"
            self._register(order)
            raise OrderError("server did not return an order id", details=payload)

        if order.state in (OrderState.CREATED, OrderState.UNKNOWN):
            order.state = OrderState.FILLED
        self._register(order)
        self.log.info("order accepted: id=%s state=%s", order.order_id, order.state.value)
        return order

    @staticmethod
    def _hint(reason: Any) -> str:
        """Turn an opaque gateway rejection into something actionable."""
        text = str(reason or "").lower()
        if "not available" in text or "not active" in text:
            return (" - the market is closed or the asset is not enabled for "
                    "this instrument type/account. Check "
                    "iq.<product>.is_open(asset) and pick an open asset "
                    "(OTC pairs stay open at weekends).")
        if "insufficient" in text or "not enough" in text:
            return " - not enough balance on the selected account."
        if "amount" in text and ("min" in text or "max" in text):
            return " - amount outside the asset's minimal/maximal bet."
        if "expiration" in text or "expired" in text:
            return (" - the expiry is not on the ladder the platform offers "
                    "for this option type.")
        return ""

    # ==================================================================
    # Registry
    # ==================================================================
    def _register(self, order: Order) -> None:
        with self._lock:
            if order.order_id is not None:
                self._orders[order.order_id] = order
            if order.request_id:
                self._by_request[order.request_id] = order
        for listener in list(self._listeners):
            try:
                listener(order)
            except Exception as exc:  # pragma: no cover
                self.log.warning("order listener failed: %s", exc)

    def get(self, order_id: int) -> Optional[Order]:
        with self._lock:
            return self._orders.get(int(order_id))

    def all(self) -> List[Order]:
        with self._lock:
            return list(self._orders.values())

    def by_state(self, state: OrderState) -> List[Order]:
        return [o for o in self.all() if o.state is state]

    def pending(self) -> List[Order]:
        return self.by_state(OrderState.PENDING)

    def filled(self) -> List[Order]:
        return self.by_state(OrderState.FILLED)

    def rejected(self) -> List[Order]:
        return self.by_state(OrderState.REJECTED)

    def history(self, limit: int = 100) -> List[Order]:
        return sorted(self.all(), key=lambda o: o.created_at, reverse=True)[:limit]

    def on_order(self, callback: Callable[[Order], None]) -> None:
        self._listeners.append(callback)

    def status(self, order_id: int) -> OrderState:
        order = self.get(order_id)
        return order.state if order else OrderState.UNKNOWN

    # ==================================================================
    # Cancel / modify
    # ==================================================================
    def cancel(self, order_id: int, *, instrument_type: Optional[InstrumentType] = None,
               timeout: Optional[float] = None) -> bool:
        order = self.get(order_id)
        itype = instrument_type or (order.instrument_type if order else InstrumentType.UNKNOWN)
        body = {"order_id": int(order_id)}
        if itype is not InstrumentType.UNKNOWN:
            body["instrument_type"] = itype.value
        try:
            self.ws.call("marginal-instruments.cancel-pending-order", body,
                         version="1.0", timeout=timeout)
        except Exception as exc:
            raise OrderError(f"cannot cancel order {order_id}: {exc}") from exc
        if order is not None:
            order.state = OrderState.CANCELLED
            self._register(order)
        return True

    def modify(self, order_id: int, *, stop_loss: Optional[float] = None,
               take_profit: Optional[float] = None,
               timeout: Optional[float] = None) -> Order:
        order = self.get(order_id)
        if order is None:
            raise OrderError(f"unknown order {order_id}")
        body: Dict[str, Any] = {"order_id": int(order_id)}
        if stop_loss is not None:
            body["stop_lose_value"] = float(stop_loss)
        if take_profit is not None:
            body["take_profit_value"] = float(take_profit)
        try:
            self.ws.call("marginal-instruments.change-order", body, version="1.0", timeout=timeout)
        except Exception as exc:
            raise OrderError(f"cannot modify order {order_id}: {exc}") from exc
        if stop_loss is not None:
            order.stop_loss = stop_loss
        if take_profit is not None:
            order.take_profit = take_profit
        self._register(order)
        return order

    def clear(self) -> None:
        with self._lock:
            self._orders.clear()
            self._by_request.clear()
