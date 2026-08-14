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
# Microservice names
# --------------------------------------------------------------------------
MS_GET_PROFILE = "get-profile"
MS_GET_BALANCES = "get-balances"
MS_CHANGE_BALANCE = "internal-billing.change-balance"
MS_PORTFOLIO_STATS = "portfolio.get-stats"
MS_PORTFOLIO_POSITIONS = "portfolio.get-positions"
MS_PORTFOLIO_POSITION_CHANGED = "portfolio.position-changed"
MS_PORTFOLIO_HISTORY = "portfolio.get-history-positions"
MS_DIGITAL_UNDERLYING = "digital-option-instruments.get-underlying-list"
MS_DIGITAL_INSTRUMENTS = "digital-option-instruments.get-instruments"
MS_DIGITAL_PRICE_EVENT = "instrument-quotes-generated"
MS_DIGITAL_STRIKE_LIST = "get-strike-list"
MS_DIGITAL_PLACE = "digital-options.place-digital-option"
# ``digital-options.place-digital-option`` is a **v3.0** microservice: the
# captured wire traffic shows version 3.0 with a body of
# ``{user_balance_id, instrument_id, amount}`` (+ ``instrument_index`` /
# ``asset_id`` when they are known) and a ``digital-option-placed`` reply.
DIGITAL_PLACE_VERSION = "3.0"
# ``buyV3`` was the pre-2019 channel and the gateway silently drops it: the
# frame is accepted but no reply is ever correlated back, which surfaced as
# ``no response for request_id=N within 25s``.  The live channel is the
# ``binary-options`` microservice below (version 1.0).
MS_BINARY_OPEN = "binary-options.open-option"
# Blitz has **no** microservice of its own.  ``blitz-options.open-option`` is
# not routed by the gateway at all - the frame is accepted and then silently
# dropped, which is exactly the ``no response for request_id=N within 25.0s``
# failure reported for blitz orders.  Blitz rides on the binary channel with
# ``option_type_id = 12`` and version **2.0** (2.0 is the version that accepts
# the extra ``expiration_size`` field).
MS_BLITZ_OPEN = MS_BINARY_OPEN
BLITZ_OPEN_VERSION = "2.0"
BINARY_OPEN_VERSION = "1.0"
MS_MARGINAL_PLACE = "place-order-temp"
MS_MARGINAL_UNDERLYING = "marginal-instruments.get-underlying-list"
MS_MARGINAL_INSTRUMENTS = "marginal-instruments.get-instruments"
MS_MARGINAL_LEVERAGE = "get-leverages"
MS_CLOSE_POSITION = "portfolio.close-position"
MS_CHANGE_TPSL = "portfolio.change-tpsl"
MS_INITIALIZATION_DATA = "get-initialization-data"
MS_UNDERLYING_LIST = "get-underlying-list"
MS_GET_INSTRUMENTS = "get-instruments"
MS_GET_CANDLES = "get-candles"
# Top assets are *not* a request/response microservice - the platform only
# publishes them through the ``top-assets-updated`` subscription below.
MS_TOP_ASSETS = "get-top-assets-info"
MS_HEARTBEAT = "heartbeat"

# Stream events
EVENT_CANDLE_GENERATED = "candle-generated"
EVENT_TRADERS_MOOD = "traders-mood-changed"
EVENT_TOP_ASSETS = "top-assets-updated"
# Digital strike books arrive on **two** different streams depending on the
# gateway/account:
#   * ``instrument-quotes-generated``           (older, quotes[].symbols[])
#   * ``digital-option-client-price-generated`` (current, prices[].call/put)
# Both carry the same information; the client subscribes to both and merges
# whichever answers first, so a gateway that only speaks one of them no longer
# times out with "event 'instrument-quotes-generated' not received".
EVENT_DIGITAL_QUOTES = "instrument-quotes-generated"
EVENT_DIGITAL_CLIENT_PRICE = "digital-option-client-price-generated"
DIGITAL_PRICE_EVENTS = (EVENT_DIGITAL_QUOTES, EVENT_DIGITAL_CLIENT_PRICE)

# Binary / turbo / blitz order lifecycle events.  ``binary-options.open-option``
# answers with an ``option`` frame carrying the echoed ``request_id``; the
# platform *also* broadcasts these events, which is what we fall back to when
# the correlated reply is lost.
EVENT_OPTION = "option"
EVENT_OPTION_OPENED = "option-opened"
EVENT_SOCKET_OPTION_OPENED = "socket-option-opened"
EVENT_OPTION_REJECTED = "option-rejected"
EVENT_BUY_COMPLETE = "buyComplete"
# Digital placement answers with ``digital-option-placed`` (accepted, carries
# ``{"id": <order id>}``) or ``digital-option-rejected``.  Like the binary
# events these are often *broadcast* without echoing our envelope request_id.
EVENT_DIGITAL_PLACED = "digital-option-placed"
EVENT_DIGITAL_REJECTED = "digital-option-rejected"
OPTION_RESULT_EVENTS = (
    EVENT_OPTION,
    EVENT_OPTION_OPENED,
    EVENT_SOCKET_OPTION_OPENED,
    EVENT_OPTION_REJECTED,
    EVENT_BUY_COMPLETE,
    EVENT_DIGITAL_PLACED,
    EVENT_DIGITAL_REJECTED,
)

# ``option_type_id`` values understood by ``*-options.open-option``.
OPTION_TYPE_BINARY = 1
OPTION_TYPE_TURBO = 3
OPTION_TYPE_BLITZ = 12

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
    """Build the ``msg`` payload of a ``sendMessage`` frame.

    ``body`` must be a JSON object (``dict``).  The InternalBilling gateway
    no longer accepts an empty string body and answers with
    ``parse error: expected { near offset 2 of ''`` - pass ``{}`` for
    parameter-less calls instead of ``""`` / ``None``.
    """
    if body is None or body == "":
        body = {}
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
