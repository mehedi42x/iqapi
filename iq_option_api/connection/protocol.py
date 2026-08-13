"""Wire protocol helpers.

The IQ Option websocket speaks JSON frames shaped like::

    {"name": "sendMessage", "request_id": "12", "msg": {...}}

Responses come back either as ``{"name": "<event>", "msg": {...}}`` (a stream
event) or with the same ``request_id`` echoed back (a reply).  ``sendMessage``
wraps a *microservice call*::

    {"name": "sendMessage",
     "request_id": "12",
     "msg": {"name": "digital-option-instruments.get-underlying-list",
             "version": "3.0",
             "body": {...}}}

This module owns: request-id generation, message building, and the registry
that lets a caller block until a matching reply arrives.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from ..exceptions import ProtocolError, TimeoutError as IQTimeoutError

# --------------------------------------------------------------------------
# Microservice names captured from the live protocol
# --------------------------------------------------------------------------
MS_GET_BALANCES = "internal-billing.get-balances"
MS_CHANGE_BALANCE = "internal-billing.change-balance"
MS_PORTFOLIO_STATS = "portfolio.get-stats"
MS_PORTFOLIO_POSITIONS = "portfolio.get-positions"
MS_PORTFOLIO_POSITION_CHANGED = "portfolio.position-changed"
MS_PORTFOLIO_HISTORY = "portfolio.get-history-positions"
MS_DIGITAL_UNDERLYING = "digital-option-instruments.get-underlying-list"
MS_DIGITAL_INSTRUMENTS = "digital-option-instruments.get-instruments"
MS_DIGITAL_PRICE_EVENT = "digital-option-client-price-generated"
MS_DIGITAL_PLACE = "digital-options.place-digital-option"
MS_BINARY_OPEN = "binary-options.open-option"
MS_BLITZ_OPEN = "blitz-options.open-option"
MS_MARGINAL_PLACE = "marginal-instruments.place-order"
MS_MARGINAL_UNDERLYING = "marginal-instruments.get-underlying-list"
MS_MARGINAL_INSTRUMENTS = "marginal-instruments.get-instruments"
MS_MARGINAL_LEVERAGE = "get-leverages"
MS_CLOSE_POSITION = "portfolio.close-position"
MS_CHANGE_TPSL = "portfolio.change-tpsl"
MS_INITIALIZATION_DATA = "get-initialization-data"
MS_UNDERLYING_LIST = "get-underlying-list"
MS_INSTRUMENTS = "instruments.get-instruments"
MS_ACTIVE_LIST = "get-active-list"
MS_TRADERS_MOOD = "traders-mood.get-mood"

# Frame names
FRAME_SEND_MESSAGE = "sendMessage"
FRAME_SUBSCRIBE = "subscribeMessage"
FRAME_UNSUBSCRIBE = "unsubscribeMessage"
FRAME_AUTH = "ssid"
FRAME_HEARTBEAT = "heartbeat"
FRAME_TIME_SYNC = "timeSync"
FRAME_SET_OPTIONS = "setOptions"


def build_message(name: str, msg: Any, *, request_id: Optional[str] = None) -> Dict[str, Any]:
    """Build a raw websocket frame."""
    frame: Dict[str, Any] = {"name": name, "msg": msg}
    if request_id is not None:
        frame["request_id"] = str(request_id)
    return frame


def build_microservice_call(name: str, body: Any, version: str = "1.0") -> Dict[str, Any]:
    """Build the ``msg`` payload of a ``sendMessage`` frame."""
    return {"name": name, "version": version, "body": body}


class Protocol:
    """Serialization + request-id allocation."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def next_request_id(self) -> str:
        with self._lock:
            return str(next(self._counter))

    @staticmethod
    def encode(frame: Dict[str, Any]) -> str:
        try:
            return json.dumps(frame, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"cannot encode frame: {exc}", details=frame) from exc

    @staticmethod
    def decode(raw: str) -> Dict[str, Any]:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"cannot decode frame: {exc}", details=raw[:400]) from exc
        if not isinstance(data, dict):
            raise ProtocolError("frame is not a JSON object", details=raw[:400])
        return data

    @staticmethod
    def event_name(frame: Dict[str, Any]) -> str:
        """Return the most specific name of a frame.

        For a ``{"name": "sendMessage"|"result", "msg": {"name": X}}`` frame the
        inner name X is the interesting one.
        """
        outer = str(frame.get("name", ""))
        msg = frame.get("msg")
        if isinstance(msg, dict):
            inner = msg.get("name")
            if isinstance(inner, str) and inner:
                return inner
        return outer

    @staticmethod
    def payload(frame: Dict[str, Any]) -> Any:
        """Unwrap ``msg`` / ``msg.msg`` / ``msg.body`` to the useful part."""
        msg = frame.get("msg")
        if isinstance(msg, dict):
            if "body" in msg and isinstance(msg.get("name"), str):
                return msg["body"]
            if "msg" in msg and isinstance(msg.get("name"), str):
                return msg["msg"]
        return msg


@dataclass
class _PendingRequest:
    request_id: str
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[BaseException] = None
    matcher: Optional[Callable[[Dict[str, Any]], bool]] = None
    created_at: float = field(default_factory=time.time)


class RequestRegistry:
    """Correlates outgoing requests with incoming replies."""

    def __init__(self) -> None:
        self._pending: Dict[str, _PendingRequest] = {}
        self._lock = threading.Lock()

    def register(self, request_id: str,
                 matcher: Optional[Callable[[Dict[str, Any]], bool]] = None) -> _PendingRequest:
        pending = _PendingRequest(request_id=request_id, matcher=matcher)
        with self._lock:
            self._pending[request_id] = pending
        return pending

    def resolve(self, request_id: str, result: Any) -> bool:
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        pending.result = result
        pending.event.set()
        return True

    def try_match(self, frame: Dict[str, Any]) -> bool:
        """Resolve any pending request whose custom matcher accepts ``frame``."""
        with self._lock:
            candidates = [p for p in self._pending.values() if p.matcher is not None]
        for pending in candidates:
            try:
                matched = bool(pending.matcher(frame))  # type: ignore[misc]
            except Exception:  # pragma: no cover - matcher must never break the loop
                matched = False
            if matched:
                return self.resolve(pending.request_id, frame)
        return False

    def fail_all(self, error: BaseException) -> None:
        with self._lock:
            pending_list = list(self._pending.values())
            self._pending.clear()
        for pending in pending_list:
            pending.error = error
            pending.event.set()

    def cancel(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def wait(self, pending: _PendingRequest, timeout: float) -> Any:
        if not pending.event.wait(timeout):
            self.cancel(pending.request_id)
            raise IQTimeoutError(
                f"no response for request_id={pending.request_id} within {timeout}s"
            )
        if pending.error is not None:
            raise pending.error
        return pending.result

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
