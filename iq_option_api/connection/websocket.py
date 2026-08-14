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

from urllib.parse import urlparse

from ..config import ConnectionConfig, ReconnectPolicy
from ..exceptions import (
    ConnectionError as IQConnectionError,
    ProtocolError,
    TimeoutError as IQTimeoutError,
)
from .browser import (
    ImpersonatedSocket,
    PlainSocket,
    cookie_header,
    create_http_session,
    impersonation_available,
    origin_for,
    resolve_impersonate,
    ws_extra_headers,
    ws_header_list,
)
from .compat import patch_thread_is_alive, thread_is_alive

patch_thread_is_alive()
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


def _is_fatal_transport_error(message: str) -> bool:
    """True when retrying the same transport on another host is pointless."""
    text = (message or "").lower()
    markers = (
        "isalive",
        "unexpected keyword",
        "got an unexpected",
        "has no attribute",
        "no module named",
        "cannot import",
    )
    return any(marker in text for marker in markers)


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
        self._http: Any = None
        self._connected_url: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._state = ConnectionState.DISCONNECTED
        self._state_lock = threading.RLock()
        self._connected_event = threading.Event()
        self._closed_by_user = False
        self._send_lock = threading.Lock()
        self.cookies: Dict[str, str] = {}
        self._transport: Optional[str] = None

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
            "url": self._connected_url or self.config.websocket_url,
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "pending_requests": self.requests.pending_count,
            "subscriptions": len(self.subscriptions),
            "reconnects": self.reconnect_count,
            "server_time": self.server_time,
            "time_offset": self._time_offset,
            "last_message_age": (time.time() - self._last_message_at) if self._last_message_at else None,
            "last_error": self.last_error,
            "transport": self._transport,
            "impersonate": resolve_impersonate(self.config.impersonate) or None,
            "user_agent": self.config.user_agent,
            "has_ssid_cookie": bool(self.cookies.get("ssid")),
        }

    def browser_session(self) -> Any:
        """Shared Firefox-impersonated HTTP session (login + WS cookies)."""
        if self._http is None:
            self._http = create_http_session(self.config)
            self.log.info("http transport: %s",
                          getattr(self._http, "_iq_transport", "unknown"))
        return self._http

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def connect(self, timeout: Optional[float] = None,
                cookies: Optional[Dict[str, str]] = None) -> bool:
        if cookies:
            self.cookies.update({k: v for k, v in cookies.items() if k and v})
        if _ws is None and not impersonation_available():  # pragma: no cover
            raise IQConnectionError(
                "install a websocket transport: pip install curl_cffi websocket-client")
        if self.is_connected:
            return True

        timeout = timeout or self.config.connect_timeout
        self._closed_by_user = False
        self._set_state(ConnectionState.CONNECTING)

        urls = self.config.websocket_urls
        last_error: Optional[str] = None
        connected_url: Optional[str] = None

        # Prefer Firefox TLS impersonation (curl_cffi).  Python's own JA3 is
        # what Cloudflare drops, which surfaces as a 20s handshake timeout.
        transports: List[str] = []
        if impersonation_available() and resolve_impersonate(self.config.impersonate):
            transports.append("impersonate")
        if _ws is not None:
            transports.append("websocket-client")
        if not transports:
            raise IQConnectionError(
                "no websocket transport available — pip install curl_cffi websocket-client")

        for transport in transports:
            for url in urls:
                if self._closed_by_user:
                    break
                if self._try_connect_url(url, timeout, transport):
                    connected_url = url
                    break
                last_error = self.last_error or (
                    f"connection to {url} via {transport} failed")
                self._teardown_failed_attempt()
                # Programming / compatibility errors will fail on every host
                # the same way — skip to the next transport immediately.
                if last_error and _is_fatal_transport_error(last_error):
                    self.log.warning("giving up %s after fatal error: %s",
                                     transport, last_error)
                    break
            if connected_url:
                break

        if connected_url is None:
            self._set_state(ConnectionState.FAILED)
            self.close()
            raise IQConnectionError(self._connect_error(urls, last_error))

        self._start_heartbeat()
        return True

    def _try_connect_url(self, url: str, timeout: float, transport: str) -> bool:
        self._connected_event.clear()
        host = urlparse(url).hostname or self.config.host
        origin = self.config.origin or origin_for(host, tls=self.config.enable_ssl)
        impersonate = resolve_impersonate(self.config.impersonate)

        if transport == "impersonate":
            self._app = ImpersonatedSocket(
                url,
                session=self.browser_session(),
                impersonate=impersonate,
                headers=ws_extra_headers(origin),
                cookies=dict(self.cookies),
                timeout=timeout,
                proxy=self.config.proxy,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._transport = f"curl_cffi/{impersonate}"
        else:
            # Known-good path: UA + Origin + Cookie: ssid=…, no ping thread.
            headers = ws_header_list(self.config.user_agent, origin,
                                     cookies=self.cookies)
            cookie = cookie_header(self.cookies)
            sslopt = None if self.config.enable_ssl else {"cert_reqs": ssl.CERT_NONE}
            self._app = PlainSocket(
                url,
                headers=headers,
                cookie=cookie or None,
                origin=origin,
                timeout=timeout,
                sslopt=sslopt,
                proxy=self.config.proxy,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._transport = "websocket-client"

        self.log.info("websocket handshake %s via %s (origin=%s cookie=%s)",
                      url, self._transport, origin,
                      "ssid" if self.cookies.get("ssid") else "none")

        def _run(app: Any = self._app, run_url: str = url, how: str = transport) -> None:
            try:
                # Never pass ping_interval — old websocket-client then calls
                # Thread.isAlive() on teardown (removed in Python 3.13).
                app.run_forever()
            except Exception as exc:  # pragma: no cover - transport level
                self.last_error = str(exc)
                self.log.error("websocket loop crashed (%s / %s): %s", run_url, how, exc)
            finally:
                self._handle_disconnect()

        self._thread = threading.Thread(target=_run, name="iq-ws-reader", daemon=True)
        self._thread.start()

        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if self._connected_event.is_set():
                return True
            # Handshake errors (TLS drop, refused, isAlive, etc.) kill the
            # reader thread immediately — don't sit on the full timeout.
            if self._thread is not None and not thread_is_alive(self._thread):
                if not self.last_error:
                    self.last_error = f"connection to {url} failed immediately ({transport})"
                self.log.warning("%s", self.last_error)
                return False
            remaining = deadline - time.time()
            self._connected_event.wait(min(0.2, max(0.01, remaining)))

        self.last_error = f"connection to {url} timed out after {timeout}s ({transport})"
        self.log.warning("%s", self.last_error)
        return False

    def _teardown_failed_attempt(self) -> None:
        self.close()
        if self._thread is not None and thread_is_alive(self._thread):
            self._thread.join(timeout=2.0)
        self._closed_by_user = False
        self._set_state(ConnectionState.CONNECTING)

    @staticmethod
    def _connect_error(urls: List[str], last_error: Optional[str]) -> str:
        err = last_error or "timed out"
        if "isAlive" in err:
            hint = (
                "Old websocket-client called Thread.isAlive() "
                "(removed in Python 3.13). "
                "Upgrade: pip install -U 'websocket-client>=1.6'. "
                "The bot now avoids that code path automatically."
            )
        elif impersonation_available():
            hint = (
                "Tried Firefox TLS impersonation and the stock websocket-client. "
                "Check network / region / IQ_PROXY, or set IQ_WS_HOSTS."
            )
        else:
            hint = (
                "If the handshake hangs, Cloudflare may be dropping Python's "
                "TLS fingerprint — pip install 'curl_cffi>=0.7'. "
                "Otherwise upgrade websocket-client: "
                "pip install -U 'websocket-client>=1.6'."
            )
        return (
            f"could not connect to any websocket endpoint "
            f"({', '.join(urls)}): {err}. {hint}"
        )

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
        url = getattr(app, "url", None) or self.config.websocket_url
        self._connected_url = url
        self.log.info("websocket connected to %s", url)
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
        # A failed *initial* handshake must not start the reconnect storm —
        # only a socket that was actually CONNECTED gets replayed.
        if self._closed_by_user or not was_connected:
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
                sock = self._ws
                if sock is None:
                    raise IQConnectionError("websocket is not connected")
                if isinstance(data, str) and hasattr(sock, "send_str"):
                    sock.send_str(data)
                else:
                    sock.send(data)
            except IQConnectionError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                raise IQConnectionError(f"send failed: {exc}") from exc
        self.messages_sent += 1
        return str(frame.get("request_id", ""))

    def send(self, name: str, msg: Any, *, request_id: Optional[str] = None) -> str:
        rid = request_id or self.protocol.next_request_id()
        return self.send_frame(build_message(name, msg), request_id=rid)

    def send_websocket_request(self, name: str, msg: Any,
                               request_id: str = "") -> str:
        """Send one IQ Option wire frame, compatible with the reference API.

        ``Lu-Yi-Hsun/iqoptionapi`` exposes this small primitive and its
        channel classes build every operation on top of it.  Keeping it here
        makes the protocol genuinely point-to-point: callers can send a raw
        channel (``ssid``, ``heartbeat``, ``subscribeMessage`` or
        ``sendMessage``) without bypassing our locking, encoding and counters.
        An explicitly supplied request id is preserved, including the
        reference implementation's empty-string default.
        """
        frame = build_message(name, msg, request_id=str(request_id))
        return self.send_frame(frame)

    @property
    def websocket(self) -> Any:
        """The active socket adapter (reference API compatibility)."""
        return self._app

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
                  version: Optional[str] = None,
                  send_frame: bool = True):
        """Register a routed subscription and tell the server about it."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        msg: Dict[str, Any] = {"name": event_name}
        if version:
            msg["version"] = version
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
        if self._heartbeat_thread and thread_is_alive(self._heartbeat_thread):
            return

        def _loop() -> None:
            while not self._closed_by_user:
                time.sleep(max(1.0, self.config.heartbeat_interval))
                if not self.is_connected:
                    continue
                try:
                    self.send(FRAME_HEARTBEAT, {
                        "heartbeatTime": int(time.time() * 1000),
                        "userTime": int(self.server_time * 1000),
                    })
                except Exception as exc:  # pragma: no cover
                    self.log.debug("heartbeat failed: %s", exc)

        self._heartbeat_thread = threading.Thread(target=_loop, name="iq-ws-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _reply_heartbeat(self, payload: Any) -> None:
        """Reply on the direct ``heartbeat`` channel (not sendMessage).

        The reference client answers the server's heartbeat frame directly;
        wrapping this in a microservice call is a different wire protocol and
        can leave the session disconnected even though the socket is open.
        """
        try:
            heartbeat_time = payload.get("heartbeatTime") if isinstance(payload, dict) else payload
            self.send(FRAME_HEARTBEAT, {
                "heartbeatTime": int(heartbeat_time or time.time() * 1000),
                "userTime": int(self.server_time * 1000),
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
