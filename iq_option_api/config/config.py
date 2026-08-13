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
    websocket_path: str = "/echo/websocket"
    api_path: str = "/api"
    request_timeout: float = 30.0
    connect_timeout: float = 20.0
    heartbeat_interval: float = 10.0
    ping_interval: float = 20.0
    ping_timeout: float = 10.0
    time_sync_tolerance: float = 5.0
    enable_ssl: bool = True
    proxy: Optional[str] = None
    user_agent: str = "iq-option-api/1.0 (+python)"

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.enable_ssl else "ws"
        return f"{scheme}://{self.host}{self.websocket_path}"

    @property
    def http_base(self) -> str:
        scheme = "https" if self.enable_ssl else "http"
        return f"{scheme}://{self.host}"

    @property
    def auth_url(self) -> str:
        return f"{self.http_base}/v2/login"


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
                request_timeout=_env_float(f"{p}REQUEST_TIMEOUT", default=30.0),
                connect_timeout=_env_float(f"{p}CONNECT_TIMEOUT", default=20.0),
                proxy=_env(f"{p}PROXY"),
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
