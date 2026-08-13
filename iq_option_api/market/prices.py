"""Realtime price / tick streaming (quote-generated & first-candles)."""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from ..connection.websocket import WebSocketClient
from ..exceptions import TimeoutError as IQTimeoutError
from ..models import Price, Tick


class PriceStream:
    """Subscribes to quotes and keeps a rolling buffer per asset."""

    EVENT_QUOTE = "quote-generated"
    EVENT_CANDLE = "candle-generated"
    EVENT_FIRST_CANDLES = "first-candles"

    def __init__(self, client: WebSocketClient, *, buffer_size: int = 500,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.log = logger or logging.getLogger("iq_option_api.prices")
        self.buffer_size = buffer_size
        self._ticks: Dict[int, Deque[Tick]] = {}
        self._latest: Dict[int, Price] = {}
        self._subs: Dict[int, Any] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def subscribe(self, asset_id: int, *,
                  callback: Optional[Callable[[Tick], None]] = None):
        asset_id = int(asset_id)
        with self._lock:
            if asset_id in self._subs:
                sub = self._subs[asset_id]
                if callback:
                    sub.callbacks.append(lambda payload: callback(self._to_tick(payload)))
                return sub

        def _handler(payload: Any) -> None:
            tick = self._to_tick(payload)
            if tick is None:
                return
            self._record(tick)
            if callback:
                callback(tick)

        sub = self.ws.subscribe(self.EVENT_QUOTE,
                                params={"active_id": asset_id},
                                callback=_handler)
        with self._lock:
            self._subs[asset_id] = sub
        return sub

    def unsubscribe(self, asset_id: int) -> bool:
        with self._lock:
            sub = self._subs.pop(int(asset_id), None)
        if sub is None:
            return False
        return self.ws.unsubscribe(sub.subscription_id)

    def unsubscribe_all(self) -> None:
        for asset_id in list(self._subs):
            self.unsubscribe(asset_id)

    # ------------------------------------------------------------------
    def latest(self, asset_id: int) -> Optional[Price]:
        with self._lock:
            return self._latest.get(int(asset_id))

    def ticks(self, asset_id: int, count: int = 50) -> List[Tick]:
        with self._lock:
            buffer = self._ticks.get(int(asset_id))
            return list(buffer)[-count:] if buffer else []

    def wait_for_price(self, asset_id: int, timeout: float = 20.0) -> Price:
        cached = self.latest(asset_id)
        if cached is not None:
            return cached
        self.subscribe(asset_id)
        deadline = threading.Event()

        payload = self.ws.wait_for(
            self.EVENT_QUOTE, timeout=timeout,
            predicate=lambda p: isinstance(p, dict)
            and int(p.get("active_id", p.get("id", 0)) or 0) == int(asset_id),
        )
        deadline.set()
        tick = self._to_tick(payload)
        if tick is None:
            raise IQTimeoutError(f"no price for asset {asset_id} within {timeout}s")
        self._record(tick)
        price = self.latest(asset_id)
        assert price is not None
        return price

    def bid_ask(self, asset_id: int, timeout: float = 20.0) -> Dict[str, Optional[float]]:
        price = self.wait_for_price(asset_id, timeout=timeout)
        return {"bid": price.bid, "ask": price.ask,
                "spread": price.spread, "mid": price.mid, "value": price.value}

    def spread(self, asset_id: int, timeout: float = 20.0) -> Optional[float]:
        return self.wait_for_price(asset_id, timeout=timeout).spread

    # ------------------------------------------------------------------
    def _record(self, tick: Tick) -> None:
        with self._lock:
            buffer = self._ticks.setdefault(tick.asset_id, deque(maxlen=self.buffer_size))
            buffer.append(tick)
            previous = self._latest.get(tick.asset_id)
            price = Price(
                asset_id=tick.asset_id,
                symbol=tick.symbol or (previous.symbol if previous else ""),
                bid=tick.bid if tick.bid is not None else (previous.bid if previous else None),
                ask=tick.ask if tick.ask is not None else (previous.ask if previous else None),
                value=tick.value,
                timestamp=tick.timestamp,
                raw=tick.raw,
            )
            self._latest[tick.asset_id] = price

    @staticmethod
    def _to_tick(payload: Any) -> Optional[Tick]:
        if not isinstance(payload, dict):
            return None
        if "active_id" not in payload and "id" not in payload and "asset_id" not in payload:
            return None
        return Tick.from_payload(payload)
