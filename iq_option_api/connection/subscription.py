"""Subscription manager.

Responsibilities
----------------
* keep a registry of ``subscribeMessage`` frames so they can be replayed after
  a reconnect,
* route incoming events to the right callbacks using *routing filters*
  (instrument type, asset id, user id, balance id, ...),
* hand out subscription ids and support clean unsubscription,
* cache the latest event per subscription so a caller can poll instead of
  using a callback.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

Callback = Callable[[Any], None]


def _matches(params: Dict[str, Any], payload: Any) -> bool:
    """Check that every routing filter is satisfied by ``payload``."""
    if not params:
        return True
    if not isinstance(payload, dict):
        return False
    for key, expected in params.items():
        if expected is None:
            continue
        actual = payload.get(key)
        if actual is None:
            # Some events nest the field one level down.
            nested = payload.get("msg") if isinstance(payload.get("msg"), dict) else None
            if isinstance(nested, dict):
                actual = nested.get(key)
        if actual is None:
            return False
        if str(actual) != str(expected):
            return False
    return True


@dataclass
class Subscription:
    subscription_id: str
    event_name: str
    params: Dict[str, Any] = field(default_factory=dict)
    callbacks: List[Callback] = field(default_factory=list)
    frame: Optional[Dict[str, Any]] = None       # replayed on reconnect
    unsubscribe_frame: Optional[Dict[str, Any]] = None
    last_event: Any = None
    event_count: int = 0
    active: bool = True

    def matches(self, event_name: str, payload: Any) -> bool:
        return self.active and self.event_name == event_name and _matches(self.params, payload)


class SubscriptionManager:
    """Thread-safe registry + dispatcher."""

    def __init__(self, logger=None) -> None:
        self._subs: Dict[str, Subscription] = {}
        self._by_event: Dict[str, List[str]] = {}
        self._global: List[Callback] = []
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._log = logger

    # ------------------------------------------------------------------
    def add(self, event_name: str, *, params: Optional[Dict[str, Any]] = None,
            callback: Optional[Callback] = None,
            frame: Optional[Dict[str, Any]] = None,
            unsubscribe_frame: Optional[Dict[str, Any]] = None) -> Subscription:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        with self._lock:
            sub_id = f"sub-{next(self._ids)}"
            sub = Subscription(
                subscription_id=sub_id,
                event_name=event_name,
                params=params,
                callbacks=[callback] if callback else [],
                frame=frame,
                unsubscribe_frame=unsubscribe_frame,
            )
            self._subs[sub_id] = sub
            self._by_event.setdefault(event_name, []).append(sub_id)
        return sub

    def get(self, subscription_id: str) -> Optional[Subscription]:
        with self._lock:
            return self._subs.get(subscription_id)

    def remove(self, subscription_id: str) -> Optional[Subscription]:
        with self._lock:
            sub = self._subs.pop(subscription_id, None)
            if sub is not None:
                sub.active = False
                ids = self._by_event.get(sub.event_name, [])
                if subscription_id in ids:
                    ids.remove(subscription_id)
                if not ids:
                    self._by_event.pop(sub.event_name, None)
            return sub

    def clear(self) -> None:
        with self._lock:
            self._subs.clear()
            self._by_event.clear()

    # ------------------------------------------------------------------
    def add_global_listener(self, callback: Callback) -> None:
        with self._lock:
            self._global.append(callback)

    def remove_global_listener(self, callback: Callback) -> None:
        with self._lock:
            if callback in self._global:
                self._global.remove(callback)

    # ------------------------------------------------------------------
    def dispatch(self, event_name: str, payload: Any, frame: Any = None) -> int:
        """Deliver an event.  Returns how many callbacks were invoked."""
        with self._lock:
            sub_ids = list(self._by_event.get(event_name, []))
            subs = [self._subs[i] for i in sub_ids if i in self._subs]
            global_cbs = list(self._global)

        delivered = 0
        for sub in subs:
            if not sub.matches(event_name, payload):
                continue
            sub.last_event = payload
            sub.event_count += 1
            for cb in list(sub.callbacks):
                try:
                    cb(payload)
                    delivered += 1
                except Exception as exc:  # never let user code kill the reader
                    if self._log:
                        self._log.warning("subscription callback failed (%s): %s",
                                          sub.subscription_id, exc)
        for cb in global_cbs:
            try:
                cb(frame if frame is not None else payload)
            except Exception as exc:
                if self._log:
                    self._log.warning("global listener failed: %s", exc)
        return delivered

    # ------------------------------------------------------------------
    def replay_frames(self) -> List[Dict[str, Any]]:
        """Frames that must be re-sent after a reconnect."""
        with self._lock:
            return [s.frame for s in self._subs.values() if s.active and s.frame]

    def active_subscriptions(self) -> List[Subscription]:
        with self._lock:
            return [s for s in self._subs.values() if s.active]

    def __len__(self) -> int:
        with self._lock:
            return len(self._subs)
