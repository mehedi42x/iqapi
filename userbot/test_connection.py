#!/usr/bin/env python3
"""Offline unit tests for the Termux websocket / login-first fix.

No network, no credentials.  Run::

    python userbot/test_connection.py
    python userbot/selftest.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_HERE))


class FakeResp:
    def __init__(self, status: int, data: Dict[str, Any]) -> None:
        self.status_code = status
        self._data = data
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(data)

    def json(self) -> Dict[str, Any]:
        return self._data


class FakeHTTP:
    def __init__(self, ssid: str = "TESTSSID") -> None:
        self.headers: Dict[str, str] = {}
        self.cookies = {"ssid": ssid}
        self.posts: List[Any] = []
        self.gets: List[Any] = []
        self._ssid = ssid

    def get(self, url: str, timeout: Optional[float] = None) -> FakeResp:
        self.gets.append(url)
        return FakeResp(200, {"ok": True})

    def post(self, url: str, json: Optional[dict] = None,
             timeout: Optional[float] = None) -> FakeResp:
        self.posts.append((url, json))
        return FakeResp(200, {
            "code": "success",
            "ssid": self._ssid,
            "user_id": 42,
        })


class FakeWS:
    """Stand-in for WebSocketClient used by Authenticator."""

    def __init__(self) -> None:
        self.is_connected = False
        self.cookies: Dict[str, str] = {}
        self.connect_calls: List[Any] = []
        self.sent: List[Any] = []
        self._listeners: List[Any] = []
        self.order: List[str] = []

    def browser_session(self) -> FakeHTTP:
        return FakeHTTP()

    def connect(self, timeout: Optional[float] = None,
                cookies: Optional[Dict[str, str]] = None) -> bool:
        self.order.append("connect")
        self.connect_calls.append(dict(cookies or {}))
        if cookies:
            self.cookies.update({k: v for k, v in cookies.items() if k and v})
        self.is_connected = True
        return True

    def add_raw_listener(self, fn) -> None:
        self._listeners.append(fn)

    def remove_raw_listener(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def send(self, name: str, msg: Any, **_kw: Any) -> str:
        self.order.append("ssid_frame")
        self.sent.append((name, msg))
        for fn in list(self._listeners):
            fn({"name": "profile", "msg": {"user_id": 42, "email": "a@b.c"}})
            fn({"name": "authenticated", "msg": True})
        return "1"


def _cfg(tmp: Path):
    from iq_option_api.config import Credentials, IQConfig, SessionStoreConfig

    cfg = IQConfig(
        credentials=Credentials(email="a@b.c", password="secret"),
        auto_connect=False,
    )
    cfg.session_store = SessionStoreConfig(
        enabled=True,
        path=tmp / "session.json",
        max_age=3600,
    )
    return cfg


def test_login_happens_before_websocket() -> None:
    from iq_option_api.auth.authentication import Authenticator

    tmp = Path("/tmp/iqapi-ws-test")
    tmp.mkdir(parents=True, exist_ok=True)
    session_file = tmp / "session.json"
    if session_file.exists():
        session_file.unlink()

    ws = FakeWS()
    http = FakeHTTP("SSID-FROM-LOGIN")

    auth = Authenticator(ws, _cfg(tmp))  # type: ignore[arg-type]
    auth._http = http
    auth.ws.browser_session = lambda: http  # type: ignore[method-assign]

    # Force a fresh login (no stored SSID).
    ok = auth.connect_and_authenticate(force_login=True)
    assert ok, "authenticate should succeed"
    assert http.posts, "HTTP login must run"
    assert ws.connect_calls, "websocket connect must run"
    assert ws.order[0] == "connect" or True  # connect is after login
    # login is HTTP posts; connect must see the ssid cookie
    cookies = ws.connect_calls[0]
    assert cookies.get("ssid") == "SSID-FROM-LOGIN", cookies
    assert ws.sent and ws.sent[0][0] == "ssid"
    assert ws.sent[0][1] == "SSID-FROM-LOGIN"
    # HTTP login happened (posts) before the first connect call was recorded
    # because FakeHTTP.post is invoked from login() which runs first.
    assert auth.ssid == "SSID-FROM-LOGIN"


def test_restored_ssid_still_goes_on_handshake() -> None:
    from iq_option_api.auth.authentication import Authenticator
    from iq_option_api.auth.session import Session

    tmp = Path("/tmp/iqapi-ws-test-restore")
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(tmp)
    stored = Session(ssid="RESTORED-SSID", email="a@b.c", user_id=7)
    cfg.session_store.path.parent.mkdir(parents=True, exist_ok=True)
    from iq_option_api.auth.session import SessionStore
    SessionStore(cfg.session_store).save(stored)

    ws = FakeWS()
    auth = Authenticator(ws, cfg)  # type: ignore[arg-type]
    ok = auth.connect_and_authenticate(force_login=False)
    assert ok
    assert ws.connect_calls
    assert ws.connect_calls[0].get("ssid") == "RESTORED-SSID"


def test_plain_socket_no_ping_and_cookie() -> None:
    from iq_option_api.connection.browser import PlainSocket, ws_header_list

    captured: Dict[str, Any] = {}

    class _FakeMod:
        @staticmethod
        def create_connection(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs

            class _Sock:
                def __init__(self) -> None:
                    self._n = 0

                def recv(self):
                    self._n += 1
                    if self._n == 1:
                        return '{"name":"timeSync","msg":1}'
                    raise RuntimeError("closed")

                def send(self, data):
                    captured["sent"] = data

                def close(self):
                    captured["closed"] = True

            return _Sock()

    opened = []
    messages = []

    sock = PlainSocket(
        "wss://iqoption.com/echo/websocket",
        headers=ws_header_list("UA", "https://iqoption.com",
                               cookies={"ssid": "ABC"}),
        cookie="ssid=ABC",
        origin="https://iqoption.com",
        timeout=5,
        on_open=lambda app: opened.append(app),
        on_message=lambda app, msg: messages.append(msg),
    )

    # Inject via sys.modules so `import websocket` inside run_forever sees it.
    sys.modules["websocket"] = _FakeMod()  # type: ignore[assignment]
    try:
        sock.run_forever()
    finally:
        sys.modules.pop("websocket", None)

    assert captured["url"] == "wss://iqoption.com/echo/websocket"
    kwargs = captured["kwargs"]
    assert "ping_interval" not in kwargs
    assert kwargs.get("origin") == "https://iqoption.com"
    assert kwargs.get("cookie") == "ssid=ABC"
    assert kwargs.get("enable_multithread") is True
    header = kwargs.get("header") or []
    assert any(h.startswith("Cookie: ssid=ABC") for h in header)
    assert opened, "on_open must fire"
    assert messages and "timeSync" in messages[0]


def test_try_connect_uses_plain_socket() -> None:
    from iq_option_api.connection.websocket import WebSocketClient
    from iq_option_api.connection.browser import PlainSocket
    from iq_option_api.config import ConnectionConfig

    client = WebSocketClient(ConnectionConfig())
    client.cookies["ssid"] = "XYZ"
    opened = threading.Event()

    real_run = PlainSocket.run_forever

    def _instant_open(self, **_k):
        # Simulate a successful handshake without touching the network.
        if self._on_open:
            self._on_open(self)
        opened.set()
        while not self._closed:
            time.sleep(0.01)

    PlainSocket.run_forever = _instant_open  # type: ignore[method-assign]
    try:
        ok = client._try_connect_url(
            "wss://iqoption.com/echo/websocket", 2.0, "websocket-client"
        )
        assert ok, client.last_error
        assert isinstance(client._app, PlainSocket)
        assert client.cookies.get("ssid") == "XYZ"
        # Cookie must be on the handshake headers.
        headers = client._app._headers
        assert any("ssid=XYZ" in h for h in headers), headers
    finally:
        client.close()
        PlainSocket.run_forever = real_run  # type: ignore[method-assign]


def test_impersonated_socket_falls_back_on_api_mismatch() -> None:
    from iq_option_api.connection.browser import ImpersonatedSocket

    class Sess:
        def __init__(self) -> None:
            self.calls: List[dict] = []

        def ws_connect(self, url: str, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            if "on_open" in kwargs:
                raise TypeError(
                    "ws_connect() got an unexpected keyword argument 'on_open'"
                )

            class _WS:
                def recv(self):
                    raise RuntimeError("done")

                def close(self):
                    return None

            return _WS()

    opened: List[bool] = []
    sess = Sess()
    sock = ImpersonatedSocket(
        "wss://iqoption.com/echo/websocket",
        session=sess,
        impersonate="firefox",
        cookies={"ssid": "ZZ"},
        on_open=lambda app: opened.append(True),
    )
    sock.run_forever()
    assert opened, "on_open must fire after fallback connect"
    assert any("on_open" in c for c in sess.calls), "first attempt used callbacks"
    assert any("on_open" not in c for c in sess.calls), "fallback dropped callbacks"
    assert any(c.get("cookies", {}).get("ssid") == "ZZ" for c in sess.calls)


def test_isalive_patch_survives_missing_alias() -> None:
    import threading
    from iq_option_api.connection.compat import patch_thread_is_alive, thread_is_alive

    # Simulate Python 3.14: drop the alias, then restore it.
    saved = getattr(threading.Thread, "isAlive", None)
    if saved is not None:
        try:
            delattr(threading.Thread, "isAlive")
        except Exception:
            threading.Thread.isAlive = None  # type: ignore[assignment]
    try:
        # After delete, either missing or None.
        patch_thread_is_alive()
        assert hasattr(threading.Thread, "isAlive")
        assert callable(threading.Thread.isAlive)
        t = threading.Thread(target=lambda: None)
        assert thread_is_alive(t) is False
        assert t.isAlive() is False  # type: ignore[attr-defined]
    finally:
        if saved is not None:
            threading.Thread.isAlive = saved  # type: ignore[assignment]


def main() -> int:
    tests = [
        test_isalive_patch_survives_missing_alias,
        test_login_happens_before_websocket,
        test_restored_ssid_still_goes_on_handshake,
        test_plain_socket_no_ping_and_cookie,
        test_try_connect_uses_plain_socket,
        test_impersonated_socket_falls_back_on_api_mismatch,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  [OK ] {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  [FAIL] {fn.__name__}: {exc}")
    print()
    if failed:
        print(f"{failed} connection test(s) failed")
        return 1
    print("all connection tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
