"""Browser-like HTTP + WebSocket transport.

IQ Option sits behind Cloudflare.  A stock Python TLS fingerprint (JA3) is
silently dropped — that is the 20 s ``wss://…/echo/websocket`` timeout.  A
User-Agent string alone is not enough: the handshake itself has to look like
a real browser.

This module impersonates **Firefox** (matching UA, headers, cookies, and —
when ``curl_cffi`` is installed — the TLS/JA3 handshake).  ``curl_cffi`` is
optional; without it we still send a real Firefox UA over ``requests`` /
``websocket-client``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

# Keep this in lockstep with curl_cffi's ``firefox`` alias (currently 147).
FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) "
    "Gecko/20100101 Firefox/147.0"
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
    try:
        from curl_cffi.requests import Session  # noqa: F401
    except ImportError:
        return False
    return True


def available_impersonate_profiles() -> List[str]:
    try:
        from curl_cffi.requests.impersonate import BrowserType
    except ImportError:
        return []
    names = [m.value for m in BrowserType]
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


def ws_header_list(user_agent: str, origin: str) -> List[str]:
    """``websocket-client`` wants ``Header: value`` strings."""
    return [
        f"User-Agent: {user_agent or FIREFOX_USER_AGENT}",
        "Accept: */*",
        "Accept-Language: en-US,en;q=0.9",
        "Cache-Control: no-cache",
        "Pragma: no-cache",
        "Sec-Fetch-Dest: empty",
        "Sec-Fetch-Mode: websocket",
        "Sec-Fetch-Site: same-origin",
        f"Origin: {origin}",
    ]


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
        from curl_cffi.requests import Session as CfSession

        session = CfSession(impersonate=impersonate)
        session.headers.update(headers)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        session._iq_impersonate = impersonate
        session._iq_transport = f"curl_cffi/{impersonate}"
        return session

    import requests

    session = requests.Session()
    session.headers.update(headers)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session._iq_impersonate = ""
    session._iq_transport = "requests"
    return session


class ImpersonatedSocket:
    """Thin adapter so curl_cffi's WebSocket looks like ``WebSocketApp``.

    ``run_forever`` blocks the reader thread (same contract as websocket-client).
    ``send`` / ``close`` are used from other threads.
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

    # ------------------------------------------------------------------
    def run_forever(self, **_ignored: Any) -> None:
        if self._closed:
            return
        kwargs: Dict[str, Any] = {
            "impersonate": self._impersonate or None,
            "headers": self._headers,
            "timeout": self._timeout,
            "default_headers": True,
            "on_open": self._on_open,
            "on_message": self._on_message,
            "on_error": self._on_error,
            "on_close": self._on_close,
        }
        if self._cookies:
            kwargs["cookies"] = self._cookies
        if self._proxy:
            kwargs["proxy"] = self._proxy
        try:
            self._ws = self._session.ws_connect(self.url, **kwargs)
        except Exception as exc:
            if self._on_error is not None:
                try:
                    self._on_error(self, exc)
                except Exception:
                    pass
            raise
        if self._closed:
            self._safe_close()
            return
        self._ws.run_forever()

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


def collect_cookie_names(cookies: Iterable[str]) -> List[str]:
    return [name for name in cookies if name]
