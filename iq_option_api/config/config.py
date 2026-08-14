"""Configuration objects.

Design rules
------------
* **No credential is ever hardcoded.**  They come from the environment, a JSON
  file, or are passed explicitly by the application.
* Every knob has a sane default so ``IQConfig()`` alone is usable.
* ``repr`` never leaks the password or the SSID.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

from ..exceptions import ConfigurationError

DEFAULT_HOST = "iqoption.com"
# IQ Option serves the same websocket/API off several hostnames.  The client
# tries them in order and sticks with the first one that answers, so a single
# blocked/geo-routed hostname no longer wedges the whole bot.  ``iqbroker.com``
# is the sister brand that shares the exact same platform + protocol.
DEFAULT_WS_HOSTS = ("iqoption.com", "iqbroker.com", "eu.iqoption.com")
# Real Firefox UA.  Must stay in lockstep with curl_cffi's ``firefox`` alias
# (currently Firefox 147) so the User-Agent and the TLS fingerprint match.
# Cloudflare silently drops Python's JA3 — a fake UA alone is not enough.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) "
    "Gecko/20100101 Firefox/147.0"
)
DEFAULT_IMPERSONATE = "firefox"
PRACTICE = "PRACTICE"
REAL = "REAL"


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return default


def _env_float(*names: str, default: float) -> float:
    raw = _env(*names)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigurationError(f"invalid float for {names[0]}: {raw!r}") from exc


def _env_int(*names: str, default: int) -> int:
    return int(_env_float(*names, default=float(default)))


def _env_bool(*names: str, default: bool) -> bool:
    raw = _env(*names)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_hosts(*names: str, default: tuple) -> tuple:
    """Parse a comma/space separated hostname list from the environment."""
    raw = _env(*names)
    if raw is None:
        return default
    hosts = tuple(h.strip() for h in raw.replace(",", " ").split() if h.strip())
    return hosts or default


def _env_tuple(raw: Optional[str]) -> tuple:
    if not raw:
        return ()
    return tuple(v.strip() for v in raw.replace(",", " ").split() if v.strip())


@dataclass
class Credentials:
    """Login material.  Never printed in full."""

    email: Optional[str] = None
    password: Optional[str] = None

    def validate(self) -> None:
        if not self.email or not self.password:
            raise ConfigurationError(
                "email/password missing - set IQ_EMAIL and IQ_PASSWORD "
                "(or pass Credentials explicitly). Credentials are never hardcoded."
            )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        masked = "***" if self.password else None
        return f"Credentials(email={self.email!r}, password={masked!r})"


@dataclass
class ReconnectPolicy:
    enabled: bool = True
    max_attempts: int = 0            # 0 == unlimited
    initial_delay: float = 1.0
    max_delay: float = 60.0
    backoff_factor: float = 2.0
    jitter: float = 0.2
    resubscribe: bool = True
    relogin: bool = True


@dataclass
class ConnectionConfig:
    host: str = DEFAULT_HOST
    # Auth lives on its own subdomain (``auth.iqoption.com``).  ``None``
    # derives it from ``host`` so ``iqbroker.com`` -> ``auth.iqbroker.com``.
    auth_host: Optional[str] = None
    # Ordered fallback hostnames for the websocket (and HTTP) endpoints.
    websocket_hosts: tuple = DEFAULT_WS_HOSTS
    websocket_path: str = "/echo/websocket"
    api_path: str = "/api"
    request_timeout: float = 30.0
    connect_timeout: float = 30.0
    heartbeat_interval: float = 10.0
    ping_interval: float = 20.0
    ping_timeout: float = 10.0
    time_sync_tolerance: float = 5.0
    enable_ssl: bool = True
    proxy: Optional[str] = None
    user_agent: str = DEFAULT_USER_AGENT
    # curl_cffi browser profile used for HTTP login + the websocket handshake.
    # ``firefox`` (default) is what the web client looks like.  Set to ``""``
    # to disable impersonation and fall back to websocket-client.
    impersonate: str = DEFAULT_IMPERSONATE
    # Explicit ``Origin`` header for the handshake.  ``None`` derives it from
    # the host being connected (``https://<host>``), which is what the browser
    # client sends.
    origin: Optional[str] = None
    # Optional ``Sec-WebSocket-Protocol`` subprotocols offered in the handshake.
    subprotocols: tuple = ()

    # ------------------------------------------------------------------
    def websocket_url_for(self, host: str) -> str:
        scheme = "wss" if self.enable_ssl else "ws"
        return f"{scheme}://{host}{self.websocket_path}"

    @property
    def websocket_url(self) -> str:
        """Primary websocket URL (first host in the fallback list)."""
        return self.websocket_url_for(self.host)

    @property
    def websocket_urls(self) -> list:
        """Every candidate websocket URL, primary first, deduplicated."""
        hosts: list = []
        for host in (self.host, *self.websocket_hosts):
            if host and host not in hosts:
                hosts.append(host)
        return [self.websocket_url_for(h) for h in hosts]

    @property
    def origin_header(self) -> str:
        if self.origin:
            return self.origin
        scheme = "https" if self.enable_ssl else "http"
        return f"{scheme}://{self.host}"

    @property
    def http_base(self) -> str:
        scheme = "https" if self.enable_ssl else "http"
        return f"{scheme}://{self.host}"

    @property
    def resolved_auth_host(self) -> str:
        if self.auth_host:
            return self.auth_host
        return f"auth.{self.host}"

    @property
    def auth_url(self) -> str:
        # Canonical endpoint (matches the live web client + iqoptionapi):
        #   POST https://auth.iqoption.com/api/v2/login
        #       {"identifier": email, "password": password}
        scheme = "https" if self.enable_ssl else "http"
        return f"{scheme}://{self.resolved_auth_host}{self.api_path}/v2/login"

    @property
    def auth_urls(self) -> list:
        """Login endpoints, primary first.  Sister-brand hosts are fallbacks."""
        urls: list = [self.auth_url]
        scheme = "https" if self.enable_ssl else "http"
        for host in ("auth.iqoption.com", "auth.iqbroker.com"):
            url = f"{scheme}://{host}{self.api_path}/v2/login"
            if url not in urls:
                urls.append(url)
        return urls

    @property
    def warmup_urls(self) -> list:
        """Pages fetched before login so Cloudflare can issue clearance cookies."""
        scheme = "https" if self.enable_ssl else "http"
        urls: list = []
        for host in (self.host, "login.iqoption.com", *self.websocket_hosts):
            if not host:
                continue
            url = f"{scheme}://{host}/"
            if url not in urls:
                urls.append(url)
        return urls[:4]


@dataclass
class TradingLimits:
    """Consumed by the risk manager."""

    min_amount: float = 1.0
    max_amount: float = 20000.0
    max_open_positions: int = 50
    max_exposure: float = 0.0            # 0 == disabled
    max_exposure_pct_of_balance: float = 0.0
    max_orders_per_minute: int = 30
    duplicate_window: float = 1.0        # seconds
    allow_real_account_trading: bool = False
    trading_enabled: bool = True


@dataclass
class SessionStoreConfig:
    enabled: bool = True
    path: Path = field(default_factory=lambda: Path.home() / ".iq_option_api" / "session.json")
    max_age: float = 60 * 60 * 12        # 12 hours


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_raw_messages: bool = False
    logger_name: str = "iq_option_api"
    to_console: bool = True
    file: Optional[str] = None
    format: str = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


@dataclass
class IQConfig:
    """Root configuration object handed to :class:`~iq_option_api.client.IQOptionAPI`."""

    credentials: Credentials = field(default_factory=Credentials)
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    limits: TradingLimits = field(default_factory=TradingLimits)
    session_store: SessionStoreConfig = field(default_factory=SessionStoreConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    account_mode: str = PRACTICE
    default_asset: str = "EURUSD"
    auto_connect: bool = False

    # ------------------------------------------------------------------
    def validate(self) -> "IQConfig":
        if self.account_mode not in (PRACTICE, REAL):
            raise ConfigurationError(f"account_mode must be {PRACTICE!r} or {REAL!r}")
        if self.limits.min_amount <= 0:
            raise ConfigurationError("limits.min_amount must be > 0")
        if self.limits.max_amount < self.limits.min_amount:
            raise ConfigurationError("limits.max_amount < limits.min_amount")
        return self

    def replace(self, **kwargs: Any) -> "IQConfig":
        return replace(self, **kwargs)

    def to_dict(self, *, redact: bool = True) -> Dict[str, Any]:
        data = asdict(self)
        data["session_store"]["path"] = str(self.session_store.path)
        if redact and data["credentials"].get("password"):
            data["credentials"]["password"] = "***"
        return data

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, *, prefix: str = "IQ_") -> "IQConfig":
        p = prefix
        cfg = cls(
            credentials=Credentials(
                email=_env(f"{p}EMAIL", f"{p}USER"),
                password=_env(f"{p}PASSWORD", f"{p}PASS"),
            ),
            connection=ConnectionConfig(
                host=_env(f"{p}HOST", default=DEFAULT_HOST) or DEFAULT_HOST,
                auth_host=_env(f"{p}AUTH_HOST"),
                websocket_hosts=_env_hosts(f"{p}WS_HOSTS", default=DEFAULT_WS_HOSTS),
                websocket_path=_env(f"{p}WS_PATH", default="/echo/websocket") or "/echo/websocket",
                request_timeout=_env_float(f"{p}REQUEST_TIMEOUT", default=30.0),
                connect_timeout=_env_float(f"{p}CONNECT_TIMEOUT", default=20.0),
                enable_ssl=_env_bool(f"{p}SSL", default=True),
                proxy=_env(f"{p}PROXY"),
                user_agent=_env(f"{p}USER_AGENT", default=DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
                impersonate=_env(f"{p}IMPERSONATE", default=DEFAULT_IMPERSONATE) or DEFAULT_IMPERSONATE,
                origin=_env(f"{p}ORIGIN"),
                subprotocols=_env_tuple(_env(f"{p}WS_PROTOCOL")),
            ),
            limits=TradingLimits(
                min_amount=_env_float(f"{p}MIN_AMOUNT", default=1.0),
                max_amount=_env_float(f"{p}MAX_AMOUNT", default=20000.0),
                max_open_positions=_env_int(f"{p}MAX_OPEN_POSITIONS", default=50),
                max_exposure=_env_float(f"{p}MAX_EXPOSURE", default=0.0),
                allow_real_account_trading=_env_bool(f"{p}ALLOW_REAL", default=False),
            ),
            logging=LoggingConfig(level=_env(f"{p}LOG_LEVEL", default="INFO") or "INFO"),
            account_mode=(_env(f"{p}ACCOUNT_MODE", default=PRACTICE) or PRACTICE).upper(),
            default_asset=_env(f"{p}DEFAULT_ASSET", default="EURUSD") or "EURUSD",
        )
        session_path = _env(f"{p}SESSION_FILE")
        if session_path:
            cfg.session_store.path = Path(session_path)
        return cfg.validate()

    @classmethod
    def from_file(cls, path: "str | Path") -> "IQConfig":
        path = Path(path)
        if not path.exists():
            raise ConfigurationError(f"config file not found: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "IQConfig":
        cfg = cls()
        if "credentials" in raw:
            cfg.credentials = Credentials(**raw["credentials"])
        if "connection" in raw:
            cfg.connection = ConnectionConfig(**raw["connection"])
        if "reconnect" in raw:
            cfg.reconnect = ReconnectPolicy(**raw["reconnect"])
        if "limits" in raw:
            cfg.limits = TradingLimits(**raw["limits"])
        if "session_store" in raw:
            store = dict(raw["session_store"])
            if "path" in store:
                store["path"] = Path(store["path"])
            cfg.session_store = SessionStoreConfig(**store)
        if "logging" in raw:
            cfg.logging = LoggingConfig(**raw["logging"])
        for key in ("account_mode", "default_asset", "auto_connect"):
            if key in raw:
                setattr(cfg, key, raw[key])
        cfg.account_mode = str(cfg.account_mode).upper()
        return cfg.validate()


def load_config(path: "str | Path | None" = None, **overrides: Any) -> IQConfig:
    """Environment first, optional JSON file on top, then explicit overrides."""
    cfg = IQConfig.from_env()
    if path:
        file_cfg = IQConfig.from_file(path)
        # file wins over env only where it actually carries credentials
        if file_cfg.credentials.email:
            cfg.credentials = file_cfg.credentials
        cfg.connection = file_cfg.connection
        cfg.reconnect = file_cfg.reconnect
        cfg.limits = file_cfg.limits
        cfg.session_store = file_cfg.session_store
        cfg.logging = file_cfg.logging
        cfg.account_mode = file_cfg.account_mode
        cfg.default_asset = file_cfg.default_asset
    for key, value in overrides.items():
        if not hasattr(cfg, key):
            raise ConfigurationError(f"unknown config override: {key}")
        setattr(cfg, key, value)
    return cfg.validate()
