"""Authentication layer.

Flow
----
1. ``login()`` -> HTTPS ``POST https://auth.iqoption.com/api/v2/login``
   (``{"identifier": email, "password": password}``) -> SSID (the ``ssid`` cookie).
2. The SSID is sent over the websocket as an ``ssid`` frame; the server answers
   with ``profile`` / ``authenticated``.
3. The SSID is persisted so the next run can *restore* the session instead of
   logging in again.
4. ``ensure_authenticated()`` validates, and re-logins automatically when the
   session is expired or was rejected.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from ..config import Credentials, IQConfig
from ..connection.protocol import FRAME_AUTH, MS_GET_PROFILE
from ..connection.websocket import WebSocketClient
from ..exceptions import (
    AuthenticationError,
    ConnectionError as IQConnectionError,
    SessionError,
    TimeoutError as IQTimeoutError,
    TwoFactorRequired,
)
from .session import Session, SessionStore

try:  # pragma: no cover
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


class Authenticator:
    """Owns credentials, the SSID and the authentication state machine."""

    def __init__(self,
                 client: WebSocketClient,
                 config: IQConfig,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.config = config
        self.log = logger or logging.getLogger("iq_option_api.auth")
        self.store = SessionStore(config.session_store)
        self.session = Session(email=config.credentials.email,
                               max_age=config.session_store.max_age)
        self._lock = threading.RLock()
        self._profile: Dict[str, Any] = {}
        self._http: Any = None

    # ==================================================================
    # Properties
    # ==================================================================
    @property
    def credentials(self) -> Credentials:
        return self.config.credentials

    @property
    def ssid(self) -> Optional[str]:
        return self.session.ssid

    @property
    def is_authenticated(self) -> bool:
        return self.session.is_valid

    @property
    def user_id(self) -> Optional[int]:
        return self.session.user_id

    @property
    def profile(self) -> Dict[str, Any]:
        return dict(self._profile)

    def state(self) -> Dict[str, Any]:
        return {
            "authenticated": self.is_authenticated,
            "user_id": self.session.user_id,
            "email": self.session.email,
            "session_age": self.session.age,
            "expired": self.session.is_expired,
            "has_ssid": bool(self.session.ssid),
        }

    # ==================================================================
    # HTTP login
    # ==================================================================
    def _session_http(self):
        if requests is None:  # pragma: no cover
            raise AuthenticationError("requests is required: pip install requests")
        if self._http is None:
            self._http = requests.Session()
            self._http.headers.update({
                "User-Agent": self.config.connection.user_agent,
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            if self.config.connection.proxy:
                self._http.proxies = {
                    "http": self.config.connection.proxy,
                    "https": self.config.connection.proxy,
                }
        return self._http

    def login(self, email: Optional[str] = None, password: Optional[str] = None,
              *, two_factor_code: Optional[str] = None) -> str:
        """Acquire a fresh SSID over HTTPS.  Returns the SSID."""
        creds = Credentials(email or self.credentials.email,
                            password or self.credentials.password)
        creds.validate()

        http = self._session_http()
        payload: Dict[str, Any] = {"identifier": creds.email, "password": creds.password}
        if two_factor_code:
            payload["code"] = two_factor_code

        url = self.config.connection.auth_url
        self.log.info("logging in as %s", creds.email)
        try:
            response = http.post(url, json=payload,
                                 timeout=self.config.connection.connect_timeout)
        except Exception as exc:
            raise IQConnectionError(f"login request failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            data = {}

        if response.status_code in (401, 403):
            raise AuthenticationError(
                data.get("message") or "invalid credentials", details=data)
        if response.status_code == 429:
            raise AuthenticationError("too many login attempts, try later", details=data)

        code = str(data.get("code", "")).lower()
        if code in ("verify", "2fa", "token_required"):
            raise TwoFactorRequired(data.get("message", "2FA required"),
                                    token=data.get("token"),
                                    method=data.get("method"))
        if response.status_code >= 400 or code not in ("success", ""):
            raise AuthenticationError(
                data.get("message") or f"login failed with HTTP {response.status_code}",
                details=data)

        ssid = data.get("ssid") or http.cookies.get("ssid")
        if not ssid:
            raise AuthenticationError("login succeeded but no SSID was returned", details=data)

        with self._lock:
            self.session = Session(
                ssid=ssid,
                user_id=data.get("user_id"),
                email=creds.email,
                max_age=self.config.session_store.max_age,
            )
        self.log.info("SSID acquired (user_id=%s)", data.get("user_id"))
        self.persist()
        return ssid

    # ==================================================================
    # SSID persistence
    # ==================================================================
    def persist(self) -> bool:
        try:
            return self.store.save(self.session)
        except SessionError as exc:
            self.log.warning("could not persist session: %s", exc)
            return False

    def restore(self) -> bool:
        """Load a previously persisted SSID (does not contact the server)."""
        stored = self.store.load(email=self.credentials.email)
        if stored is None:
            return False
        with self._lock:
            self.session = stored
        self.log.info("session restored from %s", self.store.path)
        return True

    def set_ssid(self, ssid: str, *, user_id: Optional[int] = None) -> None:
        """Inject an externally obtained SSID."""
        with self._lock:
            self.session = Session(ssid=ssid, user_id=user_id,
                                   email=self.credentials.email,
                                   max_age=self.config.session_store.max_age)

    # ==================================================================
    # WebSocket authentication handshake
    # ==================================================================
    def authenticate(self, timeout: Optional[float] = None) -> bool:
        """Send the SSID frame and wait for the server to accept it."""
        if not self.session.ssid:
            raise SessionError("no SSID available - call login() or restore() first")
        if not self.ws.is_connected:
            raise IQConnectionError("websocket must be connected before authenticating")

        timeout = timeout or self.config.connection.request_timeout
        result: Dict[str, Any] = {}
        done = threading.Event()

        def _on_frame(frame: Dict[str, Any]) -> None:
            name = str(frame.get("name", ""))
            msg = frame.get("msg")
            if name == "profile" and isinstance(msg, dict):
                result["profile"] = msg
                done.set()
            elif name in ("authenticated", "ssid") and msg is not None:
                result["authenticated"] = msg
                if msg is False:
                    done.set()
            elif name == "socket-option-closed":  # pragma: no cover
                result["error"] = msg

        self.ws.add_raw_listener(_on_frame)
        try:
            self.ws.send(FRAME_AUTH, self.session.ssid)
            if not done.wait(timeout):
                if result.get("authenticated") is True:
                    pass  # accepted, profile just did not arrive
                else:
                    raise IQTimeoutError(f"authentication timed out after {timeout}s")
            if result.get("authenticated") is False:
                self.session.invalidate("rejected by server")
                raise SessionError("SSID rejected by server")
        finally:
            self.ws.remove_raw_listener(_on_frame)

        profile = result.get("profile") or {}
        self._profile = profile if isinstance(profile, dict) else {}
        user_id = self._profile.get("user_id") or self._profile.get("id")
        self.session.mark_authenticated(user_id=user_id)
        self.session.email = self._profile.get("email") or self.session.email
        self.persist()
        self.log.info("authenticated (user_id=%s)", self.session.user_id)
        return True

    def fetch_profile(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """``get-profile`` over the websocket. Updates the cached profile."""
        payload = self.ws.call(MS_GET_PROFILE, "", version="1.0",
                               timeout=timeout or self.config.connection.request_timeout)
        if isinstance(payload, dict):
            self._profile = payload
        return self.profile

    # ==================================================================
    # High level helpers
    # ==================================================================
    def connect_and_authenticate(self, *, force_login: bool = False,
                                 two_factor_code: Optional[str] = None) -> bool:
        """Full path: restore-or-login, connect, authenticate."""
        if not self.ws.is_connected:
            self.ws.connect()

        if force_login or not self.session.ssid:
            if not force_login and self.restore():
                self.log.debug("using restored SSID")
            else:
                self.login(two_factor_code=two_factor_code)

        try:
            return self.authenticate()
        except (SessionError, IQTimeoutError) as exc:
            self.log.warning("authentication with existing SSID failed (%s), re-login", exc)
            self.store.clear()
            self.session.clear()
            self.login(two_factor_code=two_factor_code)
            return self.authenticate()

    def validate_session(self, *, max_idle: float = 300.0) -> bool:
        """Cheap liveness check of the current session."""
        if not self.session.is_valid:
            return False
        if time.time() - self.session.last_validated_at < max_idle:
            return True
        if not self.ws.is_connected:
            return False
        self.session.last_validated_at = time.time()
        return True

    def ensure_authenticated(self) -> bool:
        """Idempotent guard called before every privileged operation."""
        with self._lock:
            if self.ws.is_connected and self.validate_session():
                return True
            self.log.info("session not usable, re-authenticating")
            return self.connect_and_authenticate()

    def relogin(self, *, two_factor_code: Optional[str] = None) -> bool:
        """Force a brand new SSID."""
        self.store.clear()
        self.session.clear()
        return self.connect_and_authenticate(force_login=True, two_factor_code=two_factor_code)

    def logout(self) -> None:
        self.store.clear()
        self.session.clear()
        self._profile = {}
        self.log.info("logged out, stored session cleared")
