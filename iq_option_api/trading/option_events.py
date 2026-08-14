"""Correlation helpers for binary / turbo / blitz order replies.

Opening an option is the one place where the IQ Option gateway does not keep
its request/response contract.  ``binary-options.open-option`` is *supposed* to
answer with an ``option`` frame echoing our ``request_id``; in practice the
platform frequently answers only by broadcasting one of

* ``option-opened`` / ``socket-option-opened`` - accepted, and
* ``option-rejected`` - refused,

with the ``request_id`` buried inside ``msg`` (or missing entirely).  A client
that waits for an envelope-level ``request_id`` therefore blocks until its
request timeout expires - the ``no response for request_id=N within 25.0s``
failure this module exists to prevent.

:func:`option_matcher` builds a predicate for
:meth:`~iq_option_api.trading.orders.OrderManager.submit` that accepts such a
broadcast when it plainly belongs to the order we just sent.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..connection.protocol import (
    EVENT_DIGITAL_PLACED,
    EVENT_DIGITAL_REJECTED,
    OPTION_RESULT_EVENTS,
)

DIGITAL_RESULT_EVENTS = (EVENT_DIGITAL_PLACED, EVENT_DIGITAL_REJECTED)


def _unwrap(frame: Dict[str, Any]) -> Dict[str, Any]:
    """Return the innermost dict of a ``{"name":…,"msg":{…}}`` frame."""
    msg = frame.get("msg")
    if isinstance(msg, dict):
        inner = msg.get("msg") if isinstance(msg.get("msg"), dict) else None
        if inner is not None:
            return inner
        body = msg.get("body") if isinstance(msg.get("body"), dict) else None
        if body is not None:
            return body
        return msg
    return frame


def _frame_names(frame: Dict[str, Any]) -> set:
    names = {str(frame.get("name", ""))}
    msg = frame.get("msg")
    if isinstance(msg, dict) and isinstance(msg.get("name"), str):
        names.add(msg["name"])
    return names


def is_option_event(frame: Dict[str, Any]) -> bool:
    return bool(_frame_names(frame) & set(OPTION_RESULT_EVENTS))


def option_matcher(*, request_id: Optional[str] = None,
                   active_id: Optional[int] = None,
                   expired: Optional[int] = None,
                   direction: Optional[str] = None,
                   balance_id: Optional[int] = None,
                   instrument_id: Optional[str] = None,
                   events: Optional[tuple] = None) -> Callable[[Dict[str, Any]], bool]:
    """Predicate matching the broadcast that answers one open-option request.

    Matching is deliberately conservative: a frame is only accepted when it is
    one of the option lifecycle events *and* it either echoes our
    ``request_id`` or agrees on every order field we know about (asset, expiry,
    direction, balance, instrument).  That keeps a concurrent order placed from
    another session - or from another thread - from stealing this reply.

    ``instrument_id`` is what digital placement correlates on: the digital body
    carries no ``active_id``/``expired``, only the strike symbol, and
    ``digital-option-placed`` echoes it back.  ``events`` narrows the accepted
    event names (e.g. only the digital pair) so a binary broadcast arriving at
    the same moment cannot resolve a digital order.
    """
    wanted_direction = direction.lower() if isinstance(direction, str) else None
    wanted_instrument = str(instrument_id) if instrument_id else None
    accepted = set(events) if events else None

    def _matches(frame: Dict[str, Any]) -> bool:
        if not isinstance(frame, dict):
            return False
        if accepted is not None:
            if not (_frame_names(frame) & accepted):
                return False
        elif not is_option_event(frame):
            return False

        data = _unwrap(frame)
        if not isinstance(data, dict):
            return False

        # 1. The id echoed anywhere in the frame is decisive.
        if request_id is not None:
            for source in (frame, frame.get("msg"), data):
                if isinstance(source, dict):
                    for key in ("request_id", "requestId"):
                        value = source.get(key)
                        if value not in (None, "") and str(value) == str(request_id):
                            return True

        # 2. Otherwise every field we can compare has to agree, and at least
        #    one of them has to actually be present.
        compared = 0
        for value, keys in (
            (active_id, ("active_id", "activeId", "asset_id")),
            (expired, ("expired", "expiration_time", "exp_time")),
            (balance_id, ("user_balance_id", "balance_id")),
        ):
            if value is None:
                continue
            found = _first(data, keys)
            if found is None:
                continue
            compared += 1
            try:
                if int(found) != int(value):
                    return False
            except (TypeError, ValueError):
                return False

        if wanted_instrument is not None:
            found = _first(data, ("instrument_id", "instrumentId", "symbol"))
            if found is not None:
                compared += 1
                if str(found) != wanted_instrument:
                    return False

        if wanted_direction is not None:
            found = _first(data, ("direction", "dir", "instrument_dir"))
            if found is not None:
                compared += 1
                if str(found).lower() != wanted_direction:
                    return False

        return compared > 0

    return _matches


def digital_matcher(*, instrument_id: Optional[str] = None,
                    balance_id: Optional[int] = None,
                    request_id: Optional[str] = None) -> Callable[[Dict[str, Any]], bool]:
    """Broadcast correlator for ``digital-options.place-digital-option``.

    The gateway answers with ``{"name": "digital-option-placed",
    "msg": {"id": 21129472398}}`` - frequently *without* echoing the envelope
    ``request_id``.  A ``digital-option-placed`` frame that carries an id and
    does not contradict our instrument/balance therefore answers our request:
    digital placement is serialised per call site, so there is no other
    in-flight digital order of ours to confuse it with.
    """
    wanted_instrument = str(instrument_id) if instrument_id else None

    def _matches(frame: Dict[str, Any]) -> bool:
        if not isinstance(frame, dict):
            return False
        if not (_frame_names(frame) & set(DIGITAL_RESULT_EVENTS)):
            return False

        data = _unwrap(frame)
        if not isinstance(data, dict):
            return False

        if request_id is not None:
            for source in (frame, frame.get("msg"), data):
                if isinstance(source, dict):
                    for key in ("request_id", "requestId"):
                        value = source.get(key)
                        if value not in (None, "") and str(value) == str(request_id):
                            return True

        for value, keys in ((wanted_instrument,
                             ("instrument_id", "instrumentId", "symbol")),
                            (balance_id, ("user_balance_id", "balance_id"))):
            if value is None:
                continue
            found = _first(data, keys)
            if found is None:
                continue
            if str(found) != str(value):
                return False

        # A placement reply must carry an id, a rejection must carry a reason.
        return bool(_first(data, ("id", "order_id", "position_id"))
                    or _first(data, ("message", "error", "reason")))

    return _matches


def _first(data: Dict[str, Any], keys) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    # option payloads often nest the echo under "result"
    result = data.get("result")
    if isinstance(result, dict):
        for key in keys:
            if key in result and result[key] not in (None, ""):
                return result[key]
    return None
