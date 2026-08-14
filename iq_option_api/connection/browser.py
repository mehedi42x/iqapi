"""Browser-like HTTP + WebSocket transport.

IQ Option sits behind Cloudflare.  A stock Python TLS fingerprint (JA3) is
silently dropped — that is the 20 s ``wss://…/echo/websocket`` timeout.  A
User-Agent string alone is not enough: the handshake itself has to look like
a real browser.

This module impersonates **Firefox** (matching UA, headers, cookies, and —
when ``curl_cffi`` is installed — the TLS/JA3 handshake).  ``curl_cffi`` is
optional; without it we still send a real browser UA over ``requests`` /
``websocket-client``.  The known-good snippet that works on Termux is
exactly that: ``requests`` login, then ``WebSocketApp`` with UA + Origin +
``Cookie: ssid=…`` and ``run_forever()`` (no ping thread).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from .compat import patch_thread_is_alive

patch_thread_is_alive()

# Keep this in lockstep with curl_cffi's ``firefox`` alias (currently 147).
FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) "
    "Gecko/20100101 Firefox/147.0"
)

# Exact UA from the known-good IQ Option WebSocketApp snippet.
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_IMPERSONATE = "firefox"
# Tried in order when the preferred profile is not built into this curl_cffi.
_IMPERSONATE_FALLBACKS = (
    "firefox",
    "firefox147",
    "firefox144",
    "firefox135",
    "firefox133",
    "chrome",
    "chrome146",
    "chrome145",
    "chrome131",
)


def impersonation_available() -> bool:
    """True when curl_cffi can actually be imported.

    A Termux/Android wheel can install and still fail at import time
    (missing libcurl).  Catch every exception — not just ImportError —
    so the bot falls back to the known-good ``requests`` + websocket path.
    """
    try:
        from curl_cffi.requests import Session  # noqa: F401
    except Exception:
        return False
    return True


def available_impersonate_profiles() -> List[str]:
    try:
        from curl_cffi.requests.impersonate import BrowserType
    except Exception:
        return []
    try:
        names = [m.value for m in BrowserType]
    except Exception:
        names = []
    # aliases accepted by curl_cffi
    for alias in ("firefox", "chrome", "edge", "safari"):
        if alias not in names:
            names.append(alias)
    return names


def resolve_impersonate(preferred: Optional[str] = None) -> str:
    """Pick a curl_cffi impersonation profile.  Empty string disables it."""
    wanted = (preferred or DEFAULT_IMPERSONATE).strip().lower()
    if wanted in {"", "0", "off", "none", "false", "no"}:
        return ""
    available = set(available_impersonate_profiles())
    if not available:
        return wanted
    if wanted in available:
        return wanted
    for candidate in _IMPERSONATE_FALLBACKS:
        if candidate in available:
            return candidate
    return wanted


def origin_for(url_or_host: str, *, tls: bool = True) -> str:
    raw = (url_or_host or "").strip()
    if raw.startswith("ws://") or raw.startswith("wss://") or "://" in raw:
        host = urlparse(raw).hostname or raw
    else:
        host = raw.split("/")[0]
    scheme = "https" if tls else "http"
    return f"{scheme}://{host}"


def cookie_header(cookies: Optional[Dict[str, str]]) -> str:
    if not cookies:
        return ""
    parts = []
    for key, value in cookies.items():
        if key and value is not None and value != "":
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def extract_cookies(session: Any) -> Dict[str, str]:
    """Best-effort cookie dump from a requests / curl_cffi session."""
    out: Dict[str, str] = {}
    jar = getattr(session, "cookies", None)
    if jar is None:
        return out
    try:
        for cookie in jar:
            name = getattr(cookie, "name", None)
            value = getattr(cookie, "value", None)
            if name and value is not None:
                out[str(name)] = str(value)
                continue
    except TypeError:
        pass
    try:
        if hasattr(jar, "get_dict"):
            out.update({str(k): str(v) for k, v in jar.get_dict().items()})
        elif isinstance(jar, dict):
            out.update({str(k): str(v) for k, v in jar.items()})
    except Exception:
        pass
    return out


def http_headers(user_agent: str, origin: str) -> Dict[str, str]:
    """Headers the IQ Option *web* client sends on XHR / login."""
    return {
        "User-Agent": user_agent or FIREFOX_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": origin,
        "Referer": f"{origin}/en/login",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def ws_header_list(user_agent: str, origin: str,
                   cookies: Optional[Dict[str, str]] = None) -> List[str]:
    """Handshake headers matching the known-good IQ Option snippet.

    Extra ``Sec-Fetch-*`` headers are *not* sent: the working example only
    uses ``User-Agent``, ``Origin`` and ``Cookie: ssid=…``.
    """
    headers = [
        f"User-Agent: {user_agent or FIREFOX_USER_AGENT}",
        f"Origin: {origin}",
    ]
    cookie = cookie_header(cookies)
    if cookie:
        headers.append(f"Cookie: {cookie}")
    return headers


def ws_extra_headers(origin: str) -> Dict[str, str]:
    """Extra headers on top of curl_cffi's impersonated defaults (no UA)."""
    return {
        "Origin": origin,
        "Referer": f"{origin}/en/login",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Accept-Language": "en-US,en;q=0.9",
    }


def looks_like_challenge(response: Any) -> bool:
    """True when Cloudflare (or similar) returned a JS challenge page."""
    if response is None:
        return False
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    content_type = ""
    try:
        content_type = str(headers.get("content-type") or headers.get("Content-Type") or "")
    except Exception:
        content_type = ""
    text = ""
    try:
        text = (getattr(response, "text", None) or "")[:2000]
    except Exception:
        text = ""
    lowered = text.lower()
    if status in (403, 503) and "text/html" in content_type.lower():
        return True
    markers = (
        "just a moment",
        "cf-browser-verification",
        "cf-challenge",
        "checking your browser",
        "attention required",
        "_cf_chl",
        "cloudflare",
    )
    if "text/html" in content_type.lower() and any(m in lowered for m in markers):
        return True
    return False


def create_http_session(config: Any) -> Any:
    """Return a session that impersonates Firefox when curl_cffi is present."""
    user_agent = getattr(config, "user_agent", None) or FIREFOX_USER_AGENT
    origin = getattr(config, "origin_header", None) or origin_for(
        getattr(config, "host", "iqoption.com"),
        tls=bool(getattr(config, "enable_ssl", True)),
    )
    headers = http_headers(user_agent, origin)
    proxy = getattr(config, "proxy", None)
    impersonate = resolve_impersonate(getattr(config, "impersonate", DEFAULT_IMPERSONATE))

    if impersonate and impersonation_available():
        try:
            from curl_cffi.requests import Session as CfSession

            session = CfSession(impersonate=impersonate)
            session.headers.update(headers)
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            session._iq_impersonate = impersonate
            session._iq_transport = f"curl_cffi/{impersonate}"
            return session
        except Exception:
            # Broken Android wheel / unknown impersonate profile — fall through.
            pass

    import requests

    session = requests.Session()
    session.headers.update(headers)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session._iq_impersonate = ""
    session._iq_transport = "requests"
    return session


def _decode_ws_payload(message: Any) -> Any:
    """Normalise curl_cffi / websocket-client recv payloads to text."""
    if isinstance(message, tuple):
        message = message[0] if message else b""
    if isinstance(message, bytes):
        try:
            return message.decode("utf-8")
        except UnicodeDecodeError:
            return message
    return message


class ImpersonatedSocket:
    """Thin adapter so curl_cffi's WebSocket looks like ``WebSocketApp``.

    ``run_forever`` blocks the reader thread (same contract as websocket-client).
    ``send`` / ``close`` are used from other threads.

    curl_cffi 0.7–0.13 accepted ``on_*`` callbacks on ``Session.ws_connect``.
    0.16 prefers ``WebSocket(on_*=...).run_forever(url, ...)``.  We try both
    and fall back to a manual recv loop so a signature change cannot wedge
    the bot behind three 30 s timeouts.
    """

    def __init__(
        self,
        url: str,
        *,
        session: Any,
        impersonate: str,
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
        proxy: Optional[str] = None,
        on_open: Optional[Callable[..., None]] = None,
        on_message: Optional[Callable[..., None]] = None,
        on_error: Optional[Callable[..., None]] = None,
        on_close: Optional[Callable[..., None]] = None,
    ) -> None:
        self.url = url
        self._session = session
        self._impersonate = impersonate
        self._headers = dict(headers or {})
        self._cookies = dict(cookies or {})
        self._timeout = float(timeout)
        self._proxy = proxy
        self._on_open = on_open
        self._on_message = on_message
        self._on_error = on_error
        self._on_close = on_close
        self._ws: Any = None
        self._closed = False

    def _connect_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "impersonate": self._impersonate or None,
            "headers": self._headers,
            "timeout": self._timeout,
            "default_headers": True,
        }
        if self._cookies:
            kwargs["cookies"] = self._cookies
        if self._proxy:
            kwargs["proxy"] = self._proxy
        return kwargs

    def _bound_callbacks(self) -> Dict[str, Callable[..., None]]:
        def on_open(ws: Any) -> None:
            self._ws = ws
            if self._on_open is not None:
                self._on_open(self)

        def on_message(ws: Any, message: Any) -> None:
            if self._on_message is not None:
                self._on_message(self, _decode_ws_payload(message))

        def on_error(ws: Any, error: Any) -> None:
            if self._on_error is not None:
                self._on_error(self, error)

        def on_close(ws: Any, *rest: Any) -> None:
            code = rest[0] if rest else None
            reason = rest[1] if len(rest) > 1 else None
            if self._on_close is not None:
                self._on_close(self, code, reason)

        return {
            "on_open": on_open,
            "on_message": on_message,
            "on_error": on_error,
            "on_close": on_close,
        }

    # ------------------------------------------------------------------
    def run_forever(self, **_ignored: Any) -> None:
        if self._closed:
            return
        last_exc: Optional[BaseException] = None

        # A: Session.ws_connect(..., on_*=) — curl_cffi <= 0.13
        try:
            self._ws = self._session.ws_connect(
                self.url, **self._connect_kwargs(), **self._bound_callbacks()
            )
            if self._closed:
                self._safe_close()
                return
            runner = getattr(self._ws, "run_forever", None)
            if callable(runner):
                runner()
            else:
                self._manual_recv_loop()
            return
        except TypeError as exc:
            last_exc = exc
        except Exception as exc:
            last_exc = exc
            # Genuine handshake errors should surface, but API mismatches
            # (unexpected kwargs) must not block the websocket-client fallback.
            if not _is_api_mismatch(exc):
                if self._on_error is not None:
                    try:
                        self._on_error(self, exc)
                    except Exception:
                        pass
                raise

        # B: WebSocket(on_*=).run_forever(url, ...) — curl_cffi 0.16 README
        try:
            from curl_cffi.requests import WebSocket as CfWebSocket

            ws = CfWebSocket(**self._bound_callbacks())
            self._ws = ws
            ws.run_forever(self.url, **self._connect_kwargs())
            return
        except Exception as exc:
            last_exc = exc
            if not _is_api_mismatch(exc) and not isinstance(exc, ImportError):
                if self._on_error is not None:
                    try:
                        self._on_error(self, exc)
                    except Exception:
                        pass
                raise

        # C: connect without callbacks + our own recv loop
        try:
            self._ws = self._session.ws_connect(self.url, **self._connect_kwargs())
            if self._closed:
                self._safe_close()
                return
            if self._on_open is not None:
                self._on_open(self)
            self._manual_recv_loop()
            return
        except Exception as exc:
            last_exc = exc

        if self._on_error is not None and last_exc is not None:
            try:
                self._on_error(self, last_exc)
            except Exception:
                pass
        if last_exc is not None:
            raise last_exc

    def _manual_recv_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        while not self._closed:
            try:
                recv = getattr(ws, "recv", None)
                if recv is None:
                    break
                message = recv()
            except Exception as exc:
                if self._closed:
                    break
                if self._on_error is not None:
                    try:
                        self._on_error(self, exc)
                    except Exception:
                        pass
                break
            if message in (None, "", b""):
                break
            if self._on_message is not None:
                try:
                    self._on_message(self, _decode_ws_payload(message))
                except Exception:
                    pass
        if self._on_close is not None:
            try:
                self._on_close(self, None, None)
            except Exception:
                pass

    def send(self, data: Any) -> Any:
        ws = self._ws
        if ws is None:
            raise RuntimeError("websocket is not connected")
        if isinstance(data, str) and hasattr(ws, "send_str"):
            return ws.send_str(data)
        return ws.send(data)

    def close(self) -> None:
        self._closed = True
        self._safe_close()

    def _safe_close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            ws.close()
        except Exception:
            try:
                terminate = getattr(ws, "terminate", None)
                if terminate:
                    terminate()
            except Exception:
                pass


class PlainSocket:
    """WebSocketApp-compatible wrapper around ``websocket.create_connection``.

    Avoids ``WebSocketApp.run_forever(ping_interval=…)`` which, on old
    websocket-client, starts a ping thread and then calls ``Thread.isAlive()``
    in teardown — that alias is gone in Python 3.13+ and is the exact error
    the Termux bot prints.

    The known-good snippet is the same handshake (UA + Origin + Cookie) plus
    a blocking recv loop.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Optional[List[str]] = None,
        cookie: Optional[str] = None,
        origin: Optional[str] = None,
        timeout: float = 30.0,
        sslopt: Optional[Dict[str, Any]] = None,
        proxy: Optional[str] = None,
        on_open: Optional[Callable[..., None]] = None,
        on_message: Optional[Callable[..., None]] = None,
        on_error: Optional[Callable[..., None]] = None,
        on_close: Optional[Callable[..., None]] = None,
    ) -> None:
        self.url = url
        self._headers = list(headers or [])
        self._cookie = cookie or ""
        self._origin = origin
        self._timeout = float(timeout)
        self._sslopt = sslopt
        self._proxy = proxy
        self._on_open = on_open
        self._on_message = on_message
        self._on_error = on_error
        self._on_close = on_close
        self._ws: Any = None
        self._closed = False

    def run_forever(self, **_ignored: Any) -> None:
        if self._closed:
            return
        try:
            import websocket as ws_mod
        except ImportError as exc:  # pragma: no cover
            if self._on_error is not None:
                try:
                    self._on_error(self, exc)
                except Exception:
                    pass
            raise

        kwargs: Dict[str, Any] = {
            "header": list(self._headers),
            "timeout": self._timeout,
            "enable_multithread": True,
        }
        if self._origin:
            kwargs["origin"] = self._origin
        if self._cookie:
            kwargs["cookie"] = self._cookie
        if self._sslopt is not None:
            kwargs["sslopt"] = self._sslopt
        if self._proxy:
            host_p, _, port = (self._proxy
                               .replace("http://", "")
                               .replace("https://", "")
                               .partition(":"))
            kwargs["http_proxy_host"] = host_p
            kwargs["http_proxy_port"] = int(port or 8080)

        try:
            self._ws = ws_mod.create_connection(self.url, **kwargs)
        except Exception as exc:
            if self._on_error is not None:
                try:
                    self._on_error(self, exc)
                except Exception:
                    pass
            raise

        try:
            if self._on_open is not None:
                self._on_open(self)
            while not self._closed:
                try:
                    message = self._ws.recv()
                except Exception as exc:
                    if self._closed:
                        break
                    if self._on_error is not None:
                        try:
                            self._on_error(self, exc)
                        except Exception:
                            pass
                    break
                if message in (None, "", b""):
                    break
                if self._on_message is not None:
                    try:
                        self._on_message(self, _decode_ws_payload(message))
                    except Exception:
                        pass
        finally:
            self._safe_close()
            if self._on_close is not None:
                try:
                    self._on_close(self, None, None)
                except Exception:
                    pass

    def send(self, data: Any) -> Any:
        ws = self._ws
        if ws is None:
            raise RuntimeError("websocket is not connected")
        return ws.send(data)

    def close(self) -> None:
        self._closed = True
        self._safe_close()

    def _safe_close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            ws.close()
        except Exception:
            try:
                sock = getattr(ws, "sock", None)
                if sock is not None:
                    sock.close()
            except Exception:
                pass


def _is_api_mismatch(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "unexpected keyword",
        "got an unexpected",
        "takes no keyword",
        "required positional",
        "missing 1 required",
    )
    return isinstance(exc, TypeError) or any(m in text for m in markers)


def collect_cookie_names(cookies: Iterable[str]) -> List[str]:
    return [name for name in cookies if name]
