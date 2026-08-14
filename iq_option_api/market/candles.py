"""Candles: historical download + realtime ``candle-generated`` stream."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from ..connection.protocol import EVENT_CANDLE_GENERATED, MS_GET_CANDLES
from ..connection.websocket import WebSocketClient
from ..exceptions import MarketError, ProtocolError
from ..models import Candle

VALID_SIZES = (1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
               3600, 7200, 14400, 28800, 43200, 86400, 604800, 2592000)


class CandleManager:
    """``get-candles`` for history, ``candle-generated`` for realtime."""

    EVENT_CANDLE = EVENT_CANDLE_GENERATED

    def __init__(self, client: WebSocketClient, *, buffer_size: int = 1000,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.log = logger or logging.getLogger("iq_option_api.candles")
        self.buffer_size = buffer_size
        self._buffers: Dict[str, Deque[Candle]] = {}
        self._subs: Dict[str, Any] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def get_candles(self, asset_id: int, size: int, count: int = 100,
                    *, end_time: Optional[float] = None,
                    timeout: Optional[float] = None) -> List[Candle]:
        """Historical candles, newest last."""
        if size not in VALID_SIZES:
            self.log.debug("unusual candle size %s (not in the standard set)", size)
        if count <= 0:
            return []
        end_time = int(end_time or self.ws.server_time)
        body = {
            "active_id": int(asset_id),
            "size": int(size),
            "to": end_time,
            "count": int(count),
        }
        payload = self.ws.call(MS_GET_CANDLES, body, version="2.0", timeout=timeout)
        items = payload.get("candles") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ProtocolError("unexpected get-candles payload", details=payload)
        candles = [Candle.from_payload(item) for item in items if isinstance(item, dict)]
        for candle in candles:
            candle.asset_id = candle.asset_id or int(asset_id)
            candle.size = candle.size or int(size)
        candles.sort(key=lambda c: c.from_ts)
        self._store(int(asset_id), int(size), candles)
        return candles

    def history(self, asset_id: int, size: int, count: int,
                *, end_time: Optional[float] = None,
                page_size: int = 1000) -> List[Candle]:
        """Download more candles than one request allows, by paging backwards."""
        remaining = int(count)
        cursor = int(end_time or self.ws.server_time)
        collected: List[Candle] = []
        guard = 0
        while remaining > 0 and guard < 200:
            guard += 1
            batch_size = min(page_size, remaining)
            batch = self.get_candles(asset_id, size, batch_size, end_time=cursor)
            if not batch:
                break
            collected = batch + collected
            remaining -= len(batch)
            cursor = int(batch[0].from_ts) - 1
            if len(batch) < batch_size:
                break
        return collected[-count:] if count else collected

    def last_candle(self, asset_id: int, size: int) -> Optional[Candle]:
        candles = self.get_candles(asset_id, size, 1)
        return candles[-1] if candles else None

    def current_price(self, asset_id: int, size: int = 60) -> float:
        candle = self.last_candle(asset_id, size)
        if candle is None:
            raise MarketError(f"no candle data for asset {asset_id}")
        return candle.close

    # ------------------------------------------------------------------
    def subscribe(self, asset_id: int, size: int,
                  callback: Optional[Callable[[Candle], None]] = None):
        key = self._key(int(asset_id), int(size))
        with self._lock:
            if key in self._subs:
                return self._subs[key]

        def _handler(payload: Any) -> None:
            if not isinstance(payload, dict):
                return
            candle = Candle.from_payload(payload)
            candle.asset_id = candle.asset_id or int(asset_id)
            candle.size = candle.size or int(size)
            self._append(candle)
            if callback:
                callback(candle)

        sub = self.ws.subscribe(
            self.EVENT_CANDLE,
            params={"active_id": int(asset_id), "size": int(size)},
            callback=_handler,
        )
        with self._lock:
            self._subs[key] = sub
        return sub

    def unsubscribe(self, asset_id: int, size: int) -> bool:
        key = self._key(int(asset_id), int(size))
        with self._lock:
            sub = self._subs.pop(key, None)
        return self.ws.unsubscribe(sub.subscription_id) if sub else False

    def unsubscribe_all(self) -> None:
        with self._lock:
            keys = list(self._subs)
        for key in keys:
            asset_id, size = key.split(":")
            self.unsubscribe(int(asset_id), int(size))

    def buffered(self, asset_id: int, size: int, count: int = 100) -> List[Candle]:
        with self._lock:
            buffer = self._buffers.get(self._key(int(asset_id), int(size)))
            return list(buffer)[-count:] if buffer else []

    def wait_for_candle(self, asset_id: int, size: int, timeout: float = 90.0) -> Candle:
        self.subscribe(asset_id, size)
        payload = self.ws.wait_for(
            self.EVENT_CANDLE, timeout=timeout,
            predicate=lambda p: isinstance(p, dict)
            and int(p.get("active_id", 0) or 0) == int(asset_id)
            and int(p.get("size", 0) or 0) == int(size),
        )
        return Candle.from_payload(payload)

    # ------------------------------------------------------------------
    @staticmethod
    def _key(asset_id: int, size: int) -> str:
        return f"{asset_id}:{size}"

    def _store(self, asset_id: int, size: int, candles: List[Candle]) -> None:
        with self._lock:
            buffer = self._buffers.setdefault(self._key(asset_id, size),
                                              deque(maxlen=self.buffer_size))
            buffer.clear()
            buffer.extend(candles[-self.buffer_size:])

    def _append(self, candle: Candle) -> None:
        with self._lock:
            buffer = self._buffers.setdefault(self._key(candle.asset_id, candle.size),
                                              deque(maxlen=self.buffer_size))
            if buffer and buffer[-1].from_ts == candle.from_ts:
                buffer[-1] = candle
            else:
                buffer.append(candle)
