"""WebSocket transport.

Owns the socket lifecycle, the reader thread, request/response correlation,
heartbeat, server-time synchronisation and automatic reconnection with
subscription replay.

Everything above this layer talks to :meth:`WebSocketClient.send_request` /
:meth:`WebSocketClient.subscribe` and never touches the socket directly.
"""

from __future__ import annotations

import logging
import random
import ssl
import threading
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from ..config import ConnectionConfig, ReconnectPolicy
from ..exceptions import (
    ConnectionError as IQConnectionError,
    ProtocolError,
    TimeoutError as IQTimeoutError,
)
from .protocol import (
    FRAME_HEARTBEAT,
    FRAME_SEND_MESSAGE,
    FRAME_SET_OPTIONS,
    FRAME_SUBSCRIBE,
    FRAME_TIME_SYNC,
    FRAME_UNSUBSCRIBE,
    Protocol,
    RequestRegistry,
    build_message,
    build_microservice_call,
)
from .subscription import SubscriptionManager

try:  # pragma: no cover - import guard
    import websocket as _ws
except ImportError:  # pragma: no cover
    _ws = None


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSING = "closing"
    FAILED = "failed"


class WebSocketClient:
    """Threaded websocket client for the IQ Option protocol."""

    def __init__(self,
                 config: Optional[ConnectionConfig] = None,
                 reconnect: Optional[ReconnectPolicy] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.config = config or ConnectionConfig()
        self.reconnect_policy = reconnect or ReconnectPolicy()
        self.log = logger or logging.getLogger("iq_option_api.ws")

        self.protocol = Protocol()
        self.requests = RequestRegistry()
        self.subscriptions = SubscriptionManager(logger=self.log)

        self._ws: Any = None
        self._app: Any = None
        self._thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._state = ConnectionState.DISCONNECTED
        self._state_lock = threading.RLock()
        self._connected_event = threading.Event()
        self._closed_by_user = False
        self._send_lock = threading.Lock()

        # server time sync
        self._server_time: float = 0.0
        self._time_offset: float = 0.0
        self._last_message_at: float = 0.0
        self._last_heartbeat_at: float = 0.0

        # stats
        self.messages_received = 0
        self.messages_sent = 0
        self.reconnect_count = 0
        self.last_error: Optional[str] = None

        # hooks
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_reconnected: Optional[Callable[[], None]] = None
        self._raw_listeners: List[Callable[[Dict[str, Any]], None]] = []

    # ==================================================================
    # State
    # ==================================================================
    @property
    def state(self) -> ConnectionState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: ConnectionState) -> None:
        with self._state_lock:
            if self._state is state:
                return
            self._state = state
        self.log.debug("connection state -> %s", state.value)

    @property
    def is_connected(self) -> bool:
        return self.state is ConnectionState.CONNECTED and self._ws is not None

    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "connected": self.is_connected,
            "url": self.config.websocket_url,
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "pending_requests": self.requests.pending_count,
            "subscriptions": len(self.subscriptions),
            "reconnects": self.reconnect_count,
            "server_time": self.server_time,
            "time_offset": self._time_offset,
            "last_message_age": (time.time() - self._last_message_at) if self._last_message_at else None,
            "last_error": self.last_error,
        }

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def connect(self, timeout: Optional[float] = None) -> bool:
        if _ws is None:  # pragma: no cover
            raise IQConnectionError(
                "websocket-client is required: pip install websocket-client")
        if self.is_connected:
            return True

        timeout = timeout or self.config.connect_timeout
        self._closed_by_user = False
        self._connected_event.clear()
        self._set_state(ConnectionState.CONNECTING)

        headers = [f"User-Agent: {self.config.user_agent}"]
        self._app = _ws.WebSocketApp(
            self.config.websocket_url,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        sslopt = None if self.config.enable_ssl else {"cert_reqs": ssl.CERT_NONE}
        proxy_kwargs: Dict[str, Any] = {}
        if self.config.proxy:
            host, _, port = self.config.proxy.replace("http://", "").replace("https://", "").partition(":")
            proxy_kwargs = {"http_proxy_host": host, "http_proxy_port": int(port or 8080)}

        def _run() -> None:
            try:
                self._app.run_forever(
                    sslopt=sslopt,
                    ping_interval=int(self.config.ping_interval),
                    ping_timeout=int(self.config.ping_timeout),
                    **proxy_kwargs,
                )
            except Exception as exc:  # pragma: no cover - transport level
                self.last_error = str(exc)
                self.log.error("websocket loop crashed: %s", exc)
            finally:
                self._handle_disconnect()

        self._thread = threading.Thread(target=_run, name="iq-ws-reader", daemon=True)
        self._thread.start()

        if not self._connected_event.wait(timeout):
            self._set_state(ConnectionState.FAILED)
            self.close()
            raise IQConnectionError(f"connection to {self.config.websocket_url} timed out after {timeout}s")

        self._start_heartbeat()
        return True

    def close(self) -> None:
        self._closed_by_user = True
        self._set_state(ConnectionState.CLOSING)
        app, self._app, self._ws = self._app, None, None
        if app is not None:
            try:
                app.close()
            except Exception:  # pragma: no cover
                pass
        self.requests.fail_all(IQConnectionError("connection closed"))
        self._connected_event.clear()
        self._set_state(ConnectionState.DISCONNECTED)

    disconnect = close

    # ==================================================================
    # Socket callbacks
    # ==================================================================
    def _on_open(self, app: Any) -> None:
        self._ws = app
        self._last_message_at = time.time()
        self._set_state(ConnectionState.CONNECTED)
        self._connected_event.set()
        self.log.info("websocket connected to %s", self.config.websocket_url)
        try:
            self.send_frame(build_message(FRAME_SET_OPTIONS, {"sendResults": True}),
                            request_id=self.protocol.next_request_id())
        except Exception:  # pragma: no cover
            pass
        if self.on_connected:
            try:
                self.on_connected()
            except Exception as exc:  # pragma: no cover
                self.log.warning("on_connected hook failed: %s", exc)

    def _on_error(self, app: Any, error: Any) -> None:
        self.last_error = str(error)
        self.log.warning("websocket error: %s", error)

    def _on_close(self, app: Any, status_code: Any = None, reason: Any = None) -> None:
        self.log.info("websocket closed (code=%s reason=%s)", status_code, reason)

    def _handle_disconnect(self) -> None:
        was_connected = self.state is ConnectionState.CONNECTED
        self._ws = None
        self._connected_event.clear()
        self.requests.fail_all(IQConnectionError("connection lost"))
        if was_connected and self.on_disconnected:
            try:
                self.on_disconnected()
            except Exception as exc:  # pragma: no cover
                self.log.warning("on_disconnected hook failed: %s", exc)
        if self._closed_by_user:
            self._set_state(ConnectionState.DISCONNECTED)
            return
        if self.reconnect_policy.enabled:
            self._set_state(ConnectionState.RECONNECTING)
            threading.Thread(target=self._reconnect_loop, name="iq-ws-reconnect", daemon=True).start()
        else:
            self._set_state(ConnectionState.DISCONNECTED)

    def _on_message(self, app: Any, message: Any) -> None:
        self.messages_received += 1
        self._last_message_at = time.time()
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:  # pragma: no cover
                return
        try:
            frame = self.protocol.decode(message)
        except ProtocolError as exc:
            self.log.debug("undecodable frame: %s", exc)
            return
        self._route(frame)

    # ==================================================================
    # Routing
    # ==================================================================
    def _route(self, frame: Dict[str, Any]) -> None:
        outer = str(frame.get("name", ""))
        request_id = frame.get("request_id")
        payload = self.protocol.payload(frame)

        if outer == FRAME_TIME_SYNC:
            self._update_server_time(payload)

        if outer == FRAME_HEARTBEAT:
            self._last_heartbeat_at = time.time()
            self._reply_heartbeat(payload)

        for listener in list(self._raw_listeners):
            try:
                listener(frame)
            except Exception:  # pragma: no cover
                pass

        if request_id is not None and self.requests.resolve(str(request_id), frame):
            return
        if self.requests.try_match(frame):
            return

        event_name = self.protocol.event_name(frame)
        self.subscriptions.dispatch(event_name, payload, frame)
        if event_name != outer:
            self.subscriptions.dispatch(outer, payload, frame)

    def add_raw_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        self._raw_listeners.append(listener)

    def remove_raw_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        if listener in self._raw_listeners:
            self._raw_listeners.remove(listener)

    # ==================================================================
    # Sending
    # ==================================================================
    def send_frame(self, frame: Dict[str, Any], *, request_id: Optional[str] = None) -> str:
        if request_id is not None:
            frame["request_id"] = str(request_id)
        if not self.is_connected:
            raise IQConnectionError("websocket is not connected")
        data = self.protocol.encode(frame)
        with self._send_lock:
            try:
                self._ws.send(data)
            except Exception as exc:
                self.last_error = str(exc)
                raise IQConnectionError(f"send failed: {exc}") from exc
        self.messages_sent += 1
        return str(frame.get("request_id", ""))

    def send(self, name: str, msg: Any, *, request_id: Optional[str] = None) -> str:
        rid = request_id or self.protocol.next_request_id()
        return self.send_frame(build_message(name, msg), request_id=rid)

    def send_request(self, name: str, msg: Any, *,
                     timeout: Optional[float] = None,
                     matcher: Optional[Callable[[Dict[str, Any]], bool]] = None,
                     raw: bool = False) -> Any:
        """Send a frame and block until the correlated reply arrives."""
        timeout = timeout or self.config.request_timeout
        rid = self.protocol.next_request_id()
        pending = self.requests.register(rid, matcher=matcher)
        try:
            self.send_frame(build_message(name, msg), request_id=rid)
        except Exception:
            self.requests.cancel(rid)
            raise
        frame = self.requests.wait(pending, timeout)
        return frame if raw else self.protocol.payload(frame)

    def call(self, microservice: str, body: Any, *,
             version: str = "1.0", timeout: Optional[float] = None,
             raw: bool = False) -> Any:
        """Invoke a microservice through ``sendMessage`` and await the reply."""
        msg = build_microservice_call(microservice, body, version=version)
        return self.send_request(FRAME_SEND_MESSAGE, msg, timeout=timeout, raw=raw)

    def wait_for(self, event_name: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 timeout: float = 30.0,
                 predicate: Optional[Callable[[Any], bool]] = None) -> Any:
        """Block until a matching *stream* event arrives."""
        box: Dict[str, Any] = {}
        done = threading.Event()

        def _cb(payload: Any) -> None:
            if predicate is not None:
                try:
                    if not predicate(payload):
                        return
                except Exception:
                    return
            box["payload"] = payload
            done.set()

        sub = self.subscriptions.add(event_name, params=params, callback=_cb)
        try:
            if not done.wait(timeout):
                raise IQTimeoutError(f"event {event_name!r} not received within {timeout}s")
            return box.get("payload")
        finally:
            self.subscriptions.remove(sub.subscription_id)

    # ==================================================================
    # Subscriptions
    # ==================================================================
    def subscribe(self, event_name: str, *,
                  params: Optional[Dict[str, Any]] = None,
                  callback: Optional[Callable[[Any], None]] = None,
                  version: str = "1.0",
                  send_frame: bool = True):
        """Register a routed subscription and tell the server about it."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        msg: Dict[str, Any] = {"name": event_name, "version": version}
        if params:
            msg["params"] = {"routingFilters": dict(params)}
        frame = build_message(FRAME_SUBSCRIBE, msg)
        unsub_frame = build_message(FRAME_UNSUBSCRIBE, dict(msg))

        sub = self.subscriptions.add(
            event_name, params=params, callback=callback,
            frame=frame, unsubscribe_frame=unsub_frame,
        )
        if send_frame and self.is_connected:
            self.send_frame(dict(frame), request_id=self.protocol.next_request_id())
        return sub

    def unsubscribe(self, subscription_id: str) -> bool:
        sub = self.subscriptions.remove(subscription_id)
        if sub is None:
            return False
        if sub.unsubscribe_frame and self.is_connected:
            try:
                self.send_frame(dict(sub.unsubscribe_frame),
                                request_id=self.protocol.next_request_id())
            except Exception as exc:  # pragma: no cover
                self.log.debug("unsubscribe frame failed: %s", exc)
        return True

    def _resubscribe_all(self) -> None:
        for frame in self.subscriptions.replay_frames():
            try:
                self.send_frame(dict(frame), request_id=self.protocol.next_request_id())
            except Exception as exc:  # pragma: no cover
                self.log.warning("resubscribe failed: %s", exc)

    # ==================================================================
    # Heartbeat / time
    # ==================================================================
    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return

        def _loop() -> None:
            while not self._closed_by_user:
                time.sleep(max(1.0, self.config.heartbeat_interval))
                if not self.is_connected:
                    continue
                try:
                    self.send(FRAME_HEARTBEAT, {
                        "userTime": int(time.time() * 1000),
                        "heartbeatTime": int(self.server_time * 1000),
                    })
                except Exception as exc:  # pragma: no cover
                    self.log.debug("heartbeat failed: %s", exc)

        self._heartbeat_thread = threading.Thread(target=_loop, name="iq-ws-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _reply_heartbeat(self, payload: Any) -> None:
        try:
            self.send(FRAME_HEARTBEAT, {
                "userTime": int(time.time() * 1000),
                "heartbeatTime": int(payload) if isinstance(payload, (int, float)) else int(time.time() * 1000),
            })
        except Exception:  # pragma: no cover
            pass

    def _update_server_time(self, payload: Any) -> None:
        try:
            value = float(payload)
        except (TypeError, ValueError):
            return
        while value > 1e11:
            value /= 1000.0
        self._server_time = value
        self._time_offset = value - time.time()

    @property
    def server_time(self) -> float:
        if self._server_time:
            return time.time() + self._time_offset
        return time.time()

    @property
    def time_offset(self) -> float:
        return self._time_offset

    def sync_time(self, timeout: float = 10.0) -> float:
        """Wait for the next ``timeSync`` event (server pushes it every second)."""
        if not self.is_connected:
            raise IQConnectionError("cannot sync time while disconnected")
        try:
            payload = self.wait_for(FRAME_TIME_SYNC, timeout=timeout)
            self._update_server_time(payload)
        except IQTimeoutError:
            self.log.debug("timeSync not received, using local clock")
        return self.server_time

    # ==================================================================
    # Reconnection
    # ==================================================================
    def _reconnect_loop(self) -> None:
        policy = self.reconnect_policy
        attempt = 0
        delay = policy.initial_delay
        while not self._closed_by_user:
            if policy.max_attempts and attempt >= policy.max_attempts:
                self.log.error("giving up reconnecting after %s attempts", attempt)
                self._set_state(ConnectionState.FAILED)
                return
            attempt += 1
            sleep_for = delay * (1 + random.uniform(-policy.jitter, policy.jitter))
            self.log.info("reconnecting in %.1fs (attempt %s)", sleep_for, attempt)
            time.sleep(max(0.1, sleep_for))
            if self._closed_by_user:
                return
            try:
                self.connect()
            except Exception as exc:
                self.last_error = str(exc)
                delay = min(policy.max_delay, delay * policy.backoff_factor)
                continue
            self.reconnect_count += 1
            self.log.info("reconnected (total reconnects: %s)", self.reconnect_count)
            if self.on_reconnected:
                try:
                    self.on_reconnected()          # re-auth happens here
                except Exception as exc:
                    self.log.error("on_reconnected hook failed: %s", exc)
            if policy.resubscribe:
                self._resubscribe_all()
            return

    # ==================================================================
    def __enter__(self) -> "WebSocketClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
