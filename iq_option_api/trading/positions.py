"""Position management.

Consumes ``portfolio.position-changed`` (the event captured on the wire) plus
``portfolio.get-positions``, and offers close / SL-TP modification / result
waiting on top.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..connection.protocol import (
    MS_CHANGE_TPSL,
    MS_CLOSE_POSITION,
    MS_PORTFOLIO_POSITIONS,
    MS_PORTFOLIO_POSITION_CHANGED,
)
from ..connection.websocket import WebSocketClient
from ..exceptions import PositionError, TimeoutError as IQTimeoutError
from ..models import InstrumentType, Position, PositionState, TradeResult


class PositionManager:
    """Live view of open positions + settlement results."""

    EVENT_POSITION_CHANGED = MS_PORTFOLIO_POSITION_CHANGED

    def __init__(self, client: WebSocketClient,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.log = logger or logging.getLogger("iq_option_api.positions")
        self._positions: Dict[int, Position] = {}
        self._by_order: Dict[int, int] = {}
        self._closed: Dict[int, Position] = {}
        self._lock = threading.RLock()
        self._listeners: List[Callable[[Position], None]] = []
        self._subscriptions: List[Any] = []
        self._settled_events: Dict[int, threading.Event] = {}
        # Remembered from subscribe()/refresh() so the internal polling
        # fallbacks stay scoped to the account that placed the order.
        self._balance_id: Optional[int] = None
        self._user_id: Optional[int] = None

    # ==================================================================
    # Account binding
    # ==================================================================
    def bind_account(self, *, balance_id: Optional[int] = None,
                     user_id: Optional[int] = None) -> None:
        """Remember the active account so queries stay scoped to it.

        ``portfolio.get-positions`` and ``portfolio.position-changed`` are both
        filtered by ``user_balance_id``; binding it here means result polling
        keeps working even when no subscription was opened.
        """
        if balance_id is not None:
            self._balance_id = int(balance_id)
        if user_id is not None:
            self._user_id = int(user_id)

    # ==================================================================
    # Streaming
    # ==================================================================
    def subscribe(self, *, user_id: Optional[int] = None,
                  balance_id: Optional[int] = None,
                  instrument_types: Optional[List[InstrumentType]] = None,
                  callback: Optional[Callable[[Position], None]] = None) -> List[Any]:
        """Subscribe to ``portfolio.position-changed`` with routing filters.

        The captured subscription is v3.0 and routes on
        ``user_id`` + ``user_balance_id`` + ``instrument_type``.
        """
        if callback:
            self._listeners.append(callback)
        self.bind_account(balance_id=balance_id, user_id=user_id)
        balance_id = balance_id if balance_id is not None else self._balance_id
        user_id = user_id if user_id is not None else self._user_id

        types = instrument_types or [
            InstrumentType.BINARY, InstrumentType.TURBO, InstrumentType.DIGITAL,
            InstrumentType.BLITZ, InstrumentType.FOREX, InstrumentType.CFD,
            InstrumentType.CRYPTO,
        ]
        subs = []
        for itype in types:
            params: Dict[str, Any] = {"instrument_type": self._wire_type(itype)}
            if user_id is not None:
                params["user_id"] = user_id
            if balance_id is not None:
                params["user_balance_id"] = balance_id
            sub = self.ws.subscribe(self.EVENT_POSITION_CHANGED,
                                    params=params, version="3.0",
                                    callback=self._on_position_event)
            subs.append(sub)
        self._subscriptions.extend(subs)
        return subs

    def unsubscribe(self) -> None:
        for sub in self._subscriptions:
            self.ws.unsubscribe(sub.subscription_id)
        self._subscriptions.clear()
        self._listeners.clear()

    def on_change(self, callback: Callable[[Position], None]) -> None:
        """Register an extra listener without opening a new subscription."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Position], None]) -> bool:
        if callback in self._listeners:
            self._listeners.remove(callback)
            return True
        return False

    def _on_position_event(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        data = payload.get("position") if isinstance(payload.get("position"), dict) else payload
        position = Position.from_payload(data)
        if position.position_id is None:
            return
        self._store(position)

    def _store(self, position: Position) -> None:
        with self._lock:
            previous = self._positions.get(position.position_id)
            if previous is not None:
                # merge: streamed updates can be partial
                for field_name, value in previous.__dict__.items():
                    if field_name == "raw":
                        continue
                    if getattr(position, field_name, None) in (None, 0, 0.0, "", []) and value:
                        setattr(position, field_name, value)
            self._positions[position.position_id] = position
            for order_id in position.order_ids:
                self._by_order[order_id] = position.position_id
            if position.state is PositionState.CLOSED:
                self._closed[position.position_id] = position
                self._positions.pop(position.position_id, None)
                event = self._settled_events.get(position.position_id)
                if event:
                    event.set()

        for listener in list(self._listeners):
            try:
                listener(position)
            except Exception as exc:  # pragma: no cover
                self.log.warning("position listener failed: %s", exc)

    def on_position(self, callback: Callable[[Position], None]) -> None:
        self._listeners.append(callback)

    # ==================================================================
    # Queries
    # ==================================================================
    def refresh(self, *, instrument_types: Optional[List[InstrumentType]] = None,
                user_balance_id: Optional[int] = None,
                limit: int = 100, offset: int = 0,
                timeout: Optional[float] = None) -> List[Position]:
        types = [self._wire_type(t) for t in (instrument_types or [
            InstrumentType.BINARY, InstrumentType.TURBO, InstrumentType.DIGITAL,
            InstrumentType.BLITZ, InstrumentType.FOREX, InstrumentType.CFD,
            InstrumentType.CRYPTO])]
        # ``portfolio.get-positions`` v4.0 is scoped by balance.  Fall back to
        # the id the subscription was opened with so the internal polling in
        # wait_for_close()/close() does not silently query another account.
        if user_balance_id is None:
            user_balance_id = self._balance_id
        body: Dict[str, Any] = {"instrument_types": types, "limit": limit, "offset": offset}
        if user_balance_id is not None:
            body["user_balance_id"] = int(user_balance_id)
            self._balance_id = int(user_balance_id)

        payload = self.ws.call(MS_PORTFOLIO_POSITIONS, body, version="4.0", timeout=timeout)
        items = payload.get("positions", []) if isinstance(payload, dict) else []
        positions = [Position.from_payload(item) for item in items if isinstance(item, dict)]
        for position in positions:
            if position.state is PositionState.UNKNOWN:
                position.state = PositionState.OPEN
            self._store(position)
        return positions

    def all(self) -> List[Position]:
        with self._lock:
            return list(self._positions.values())

    def open_positions(self, *, instrument_type: Optional[InstrumentType] = None) -> List[Position]:
        positions = [p for p in self.all() if p.state is not PositionState.CLOSED]
        if instrument_type:
            positions = [p for p in positions if p.instrument_type is instrument_type]
        return positions

    def get(self, position_id: int) -> Optional[Position]:
        with self._lock:
            return self._positions.get(int(position_id)) or self._closed.get(int(position_id))

    def by_order_id(self, order_id: int) -> Optional[Position]:
        with self._lock:
            position_id = self._by_order.get(int(order_id))
        if position_id is None:
            return None
        return self.get(position_id)

    def closed_positions(self) -> List[Position]:
        with self._lock:
            return list(self._closed.values())

    def total_floating_pnl(self) -> float:
        return sum(p.floating_pnl or 0.0 for p in self.open_positions())

    # ==================================================================
    # Actions
    # ==================================================================
    def close(self, position_id: int, *, timeout: Optional[float] = None) -> bool:
        position = self.get(position_id)
        if position is None:
            self.refresh()
            position = self.get(position_id)
        if position is None:
            raise PositionError(f"position {position_id} not found")
        if position.state is PositionState.CLOSED:
            return True

        body = {"position_id": int(position_id)}
        try:
            self.ws.call(MS_CLOSE_POSITION, body, version="3.0", timeout=timeout)
        except Exception as exc:
            raise PositionError(f"cannot close position {position_id}: {exc}") from exc
        position.state = PositionState.CLOSING
        self.log.info("close requested for position %s", position_id)
        return True

    def close_all(self, *, instrument_type: Optional[InstrumentType] = None) -> int:
        closed = 0
        for position in self.open_positions(instrument_type=instrument_type):
            if position.position_id is None:
                continue
            try:
                self.close(position.position_id)
                closed += 1
            except PositionError as exc:
                self.log.warning("close failed: %s", exc)
        return closed

    def set_stop_loss_take_profit(self, position_id: int, *,
                                  stop_loss: Optional[float] = None,
                                  take_profit: Optional[float] = None,
                                  use_pnl: bool = False,
                                  timeout: Optional[float] = None) -> Position:
        position = self.get(position_id)
        if position is None:
            raise PositionError(f"position {position_id} not found")

        body: Dict[str, Any] = {"position_id": int(position_id)}
        if stop_loss is not None:
            body["stop_lose_kind"] = "pnl" if use_pnl else "price"
            body["stop_lose_value"] = float(stop_loss)
        if take_profit is not None:
            body["take_profit_kind"] = "pnl" if use_pnl else "price"
            body["take_profit_value"] = float(take_profit)
        if len(body) == 1:
            raise PositionError("nothing to change: pass stop_loss and/or take_profit")

        try:
            self.ws.call(MS_CHANGE_TPSL, body, version="2.0", timeout=timeout)
        except Exception as exc:
            raise PositionError(f"cannot change SL/TP of {position_id}: {exc}") from exc
        if stop_loss is not None:
            position.stop_loss = stop_loss
        if take_profit is not None:
            position.take_profit = take_profit
        return position

    # ==================================================================
    # Settlement
    # ==================================================================
    def wait_for_close(self, position_id: int, *, timeout: float = 300.0,
                       poll_interval: float = 5.0) -> TradeResult:
        """Block until the position is settled, then return the result."""
        position_id = int(position_id)
        with self._lock:
            existing = self._closed.get(position_id)
            if existing is not None:
                return TradeResult.from_position(existing)
            event = self._settled_events.setdefault(position_id, threading.Event())

        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = min(poll_interval, max(0.1, deadline - time.time()))
            if event.wait(remaining):
                break
            # fallback polling in case the stream event was missed
            try:
                self.refresh()
            except Exception as exc:  # pragma: no cover
                self.log.debug("refresh during wait failed: %s", exc)
            with self._lock:
                if position_id in self._closed:
                    break

        with self._lock:
            self._settled_events.pop(position_id, None)
            closed = self._closed.get(position_id)
        if closed is None:
            raise IQTimeoutError(f"position {position_id} not settled within {timeout}s")
        return TradeResult.from_position(closed)

    # ==================================================================
    @staticmethod
    def _wire_type(instrument_type: InstrumentType) -> str:
        return {
            InstrumentType.BINARY: "binary-option",
            InstrumentType.TURBO: "turbo-option",
            InstrumentType.DIGITAL: "digital-option",
            InstrumentType.BLITZ: "blitz-option",
            InstrumentType.FOREX: "marginal-forex",
            InstrumentType.CFD: "marginal-cfd",
            InstrumentType.CRYPTO: "marginal-crypto",
            InstrumentType.STOCK: "marginal-cfd",
            InstrumentType.COMMODITY: "marginal-cfd",
            InstrumentType.ETF: "marginal-cfd",
            InstrumentType.INDEX: "marginal-cfd",
        }.get(instrument_type, "marginal-cfd")
