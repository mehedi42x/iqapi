"""Userbot engine.

Owns every side-effect: ``.env`` loading, the IQ Option session, candle
download, risk, money-management and order execution.  Strategy modules
are imported as plugins and may only return a :class:`Signal`.

Nothing in this file blocks forever — every wait is chunked so Ctrl+C
and the stop-event always land, and every market-data call is retried
with backoff.  Orders are intentionally *not* retried (a timeout after
the server accepted the ticket would double-spend).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Import path: works as ``python bot.py``, ``python -m userbot``, or
# ``python userbot/bot.py`` from the repo root.
# ---------------------------------------------------------------------------
USERBOT_DIR = Path(__file__).resolve().parent
REPO_DIR = USERBOT_DIR.parent
ENV_PATH = USERBOT_DIR / ".env"
LOG_DIR = USERBOT_DIR / "logs"
DATA_DIR = USERBOT_DIR / "data"


def _bootstrap() -> None:
    for path in (REPO_DIR, USERBOT_DIR):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)


_bootstrap()

try:
    from strategies import discover, list_strategies, load_strategy
    from strategies.base import Signal, Strategy, closed_candles
except ImportError:  # pragma: no cover - package-style invocation
    from userbot.strategies import discover, list_strategies, load_strategy
    from userbot.strategies.base import Signal, Strategy, closed_candles


# ===========================================================================
# Tiny helpers
# ===========================================================================
class Interrupted(Exception):
    """Raised when a wait is cancelled by the stop event / Ctrl+C."""


def _strip_comment(value: str) -> str:
    if value[:1] in {"'", '"'}:
        return value
    if " #" in value:
        return value.split(" #", 1)[0].rstrip()
    return value


def load_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_comment(value.strip())
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[key] = value
    return out


def env_get(file_env: Dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        if os.environ.get(name) not in (None, ""):
            return str(os.environ[name])
        if file_env.get(name) not in (None, ""):
            return str(file_env[name])
    return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


_TF_ALIASES = {
    "1s": 1, "5s": 5, "10s": 10, "15s": 15, "30s": 30,
    "1m": 60, "2m": 120, "3m": 180, "5m": 300, "10m": 600,
    "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400,
    "1d": 86400, "1w": 604800,
}


def parse_timeframe(value: Any, default: int = 60) -> int:
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text in _TF_ALIASES:
        return _TF_ALIASES[text]
    try:
        number = int(float(text))
    except ValueError:
        return default
    # a bare "1" / "5" / "15" almost always means minutes
    if number in {1, 2, 3, 5, 10, 15, 30} and str(value).isdigit():
        return number * 60
    return number if number > 0 else default


def format_tf(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def format_money(value: float, currency: str = "") -> str:
    sign = "+" if value > 0 else ""
    body = f"{sign}{value:,.2f}"
    return f"{body} {currency}".strip()


GOLD_NAMES = {"GOLD", "XAUUSD", "XAU", "XAUUSD-OTC", "GOLD-OTC", "XAU/USD", "GOLDUSD"}
_ASSET_ALIASES = {
    "GOLD": ["XAUUSD", "GOLD"],
    "XAUUSD": ["XAUUSD", "GOLD"],
    "XAU/USD": ["XAUUSD", "GOLD"],
    "XAU": ["XAUUSD", "GOLD"],
    "GOLDUSD": ["XAUUSD", "GOLD"],
    "SILVER": ["XAGUSD", "SILVER"],
    "XAGUSD": ["XAGUSD", "SILVER"],
}


def expand_assets(name: str) -> List[str]:
    key = (name or "").strip().upper().replace(" ", "")
    base = list(_ASSET_ALIASES.get(key, [key or "EURUSD"]))
    # preserve order, unique
    seen = set()
    out: List[str] = []
    for item in base:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def is_gold(name: str) -> bool:
    key = (name or "").upper().replace("/", "").replace(" ", "")
    return key in GOLD_NAMES or key.startswith("XAU") or key.startswith("GOLD")


def pick_auto_strategy(trade_type: str, asset: str) -> str:
    if is_gold(asset):
        return "gold_impulse"
    kind = (trade_type or "binary").lower()
    if kind == "digital":
        return "digital1"
    if kind == "blitz":
        return "blitz_flash"
    return "binary1"


# ===========================================================================
# Configuration
# ===========================================================================
@dataclass
class EnvConfig:
    email: str = ""
    password: str = ""
    account_mode: str = "PRACTICE"
    allow_real: bool = False

    asset: str = "EURUSD"
    otc_fallback: bool = True
    timeframe: int = 60
    trade_type: str = "binary"
    duration: int = 1
    turbo: str = "auto"

    strategy: str = "auto"
    min_confidence: float = 0.62

    amount: float = 1.0
    mm_mode: str = "fixed"
    mm_percent: float = 1.5
    martingale_factor: float = 2.0
    martingale_steps: int = 2

    min_payout: float = 70.0
    max_trades: int = 80
    max_daily_loss: float = 25.0
    max_daily_profit: float = 80.0
    max_consecutive_losses: int = 4
    max_open_positions: int = 1
    cooldown_seconds: float = 4.0
    trade_on_close: bool = True
    dry_run: bool = False
    wait_result: bool = True
    payout_percent: float = 80.0

    candle_count: int = 220
    log_level: str = "INFO"
    request_timeout: float = 25.0
    reconnect_delay: float = 3.0
    max_fetch_retries: int = 4
    heartbeat_seconds: float = 60.0

    # connection endpoints (empty == use the library's live defaults)
    ws_host: str = ""
    ws_hosts: str = ""
    ws_path: str = ""
    auth_host: str = ""
    origin: str = ""
    user_agent: str = ""
    enable_ssl: bool = True

    source: Path = field(default_factory=lambda: ENV_PATH)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "EnvConfig":
        path = Path(path) if path else ENV_PATH
        file_env = load_env_file(path)
        get = lambda *n, default="": env_get(file_env, *n, default=default)

        trade_type = get("TRADE_TYPE", "INSTRUMENT", default="binary").strip().lower()
        if trade_type in {"turbo-option", "turbo_option"}:
            trade_type = "turbo"
        if trade_type in {"digital-option", "digital_option"}:
            trade_type = "digital"
        if trade_type in {"blitz-option", "blitz_option"}:
            trade_type = "blitz"
        if trade_type not in {"binary", "turbo", "digital", "blitz"}:
            trade_type = "binary"

        duration = as_int(get("DURATION", "EXPIRY", default="1"), 1)
        if trade_type == "blitz" and duration not in {5, 10, 15, 30, 60}:
            # ``DURATION=1`` in a binary-minded .env → 60-second blitz
            duration = 60 if duration <= 1 else 30

        mode = get("IQ_ACCOUNT_MODE", "ACCOUNT", "ACCOUNT_MODE", default="PRACTICE").upper()
        if mode in {"DEMO", "PRACTICE", "TRAINING"}:
            mode = "PRACTICE"
        elif mode in {"REAL", "LIVE"}:
            mode = "REAL"
        else:
            mode = "PRACTICE"

        cfg = cls(
            email=get("IQ_EMAIL", "EMAIL"),
            password=get("IQ_PASSWORD", "PASSWORD"),
            account_mode=mode,
            allow_real=as_bool(get("IQ_ALLOW_REAL", "ALLOW_REAL"), False),
            asset=get("ASSET", "SYMBOL", "IQ_DEFAULT_ASSET", default="EURUSD").upper(),
            otc_fallback=as_bool(get("OTC_FALLBACK"), True),
            timeframe=parse_timeframe(get("TIMEFRAME", "TF", default="1m"), 60),
            trade_type=trade_type,
            duration=max(1, duration),
            turbo=get("TURBO", default="auto").strip().lower() or "auto",
            strategy=get("STRATEGY", default="auto").strip() or "auto",
            min_confidence=as_float(get("MIN_CONFIDENCE", default="0.62"), 0.62),
            amount=max(0.01, as_float(get("AMOUNT", "TRADE_AMOUNT", default="1"), 1.0)),
            mm_mode=get("MM_MODE", default="fixed").strip().lower() or "fixed",
            mm_percent=as_float(get("MM_PERCENT", default="1.5"), 1.5),
            martingale_factor=max(1.0, as_float(get("MARTINGALE_FACTOR", default="2.0"), 2.0)),
            martingale_steps=max(0, as_int(get("MARTINGALE_STEPS", default="2"), 2)),
            min_payout=as_float(get("MIN_PAYOUT", default="70"), 70.0),
            max_trades=max(1, as_int(get("MAX_TRADES", default="80"), 80)),
            max_daily_loss=as_float(get("MAX_DAILY_LOSS", default="25"), 25.0),
            max_daily_profit=as_float(get("MAX_DAILY_PROFIT", default="80"), 80.0),
            max_consecutive_losses=max(1, as_int(get("MAX_CONSECUTIVE_LOSSES", default="4"), 4)),
            max_open_positions=max(1, as_int(get("MAX_OPEN_POSITIONS", default="1"), 1)),
            cooldown_seconds=max(0.0, as_float(get("COOLDOWN_SECONDS", default="4"), 4.0)),
            trade_on_close=as_bool(get("TRADE_ON_CLOSE"), True),
            dry_run=as_bool(get("DRY_RUN"), False),
            wait_result=as_bool(get("WAIT_RESULT"), True),
            payout_percent=as_float(get("PAYOUT_PERCENT", default="80"), 80.0),
            candle_count=max(50, as_int(get("CANDLE_COUNT", default="220"), 220)),
            log_level=get("LOG_LEVEL", "IQ_LOG_LEVEL", default="INFO").upper() or "INFO",
            request_timeout=as_float(get("REQUEST_TIMEOUT", "IQ_REQUEST_TIMEOUT", default="25"), 25.0),
            reconnect_delay=as_float(get("RECONNECT_DELAY", default="3"), 3.0),
            max_fetch_retries=max(1, as_int(get("MAX_FETCH_RETRIES", default="4"), 4)),
            heartbeat_seconds=as_float(get("HEARTBEAT_SECONDS", default="60"), 60.0),
            ws_host=get("IQ_HOST", "IQ_OPTION_HOST", "HOST").strip(),
            ws_hosts=get("IQ_WS_HOSTS", "WS_HOSTS").strip(),
            ws_path=get("IQ_WS_PATH", "WS_PATH").strip(),
            auth_host=get("IQ_AUTH_HOST", "AUTH_HOST").strip(),
            origin=get("IQ_ORIGIN", "ORIGIN").strip(),
            user_agent=get("IQ_USER_AGENT", "USER_AGENT").strip(),
            enable_ssl=as_bool(get("IQ_SSL", "SSL"), True),
            source=path,
        )
        return cfg

    # ------------------------------------------------------------------
    def validate_credentials(self) -> None:
        email = (self.email or "").strip()
        password = (self.password or "").strip()
        if (not email or email.startswith("your_email")
                or not password or password in {"your_password", "••••••••"}):
            raise RuntimeError(
                f"Set IQ_EMAIL and IQ_PASSWORD in {self.source} "
                "(credentials are never hardcoded)."
            )
        if self.account_mode == "REAL" and not self.allow_real:
            raise RuntimeError(
                "REAL account selected but IQ_ALLOW_REAL is false. "
                "Set IQ_ALLOW_REAL=true in .env to trade live money."
            )

    def use_turbo(self) -> bool:
        if self.trade_type == "turbo":
            return True
        if self.trade_type != "binary":
            return False
        if self.turbo in {"1", "true", "yes", "on"}:
            return True
        if self.turbo in {"0", "false", "no", "off"}:
            return False
        return self.duration <= 5

    def duration_seconds(self) -> int:
        if self.trade_type == "blitz":
            return int(self.duration)
        return int(self.duration) * 60

    def resolved_strategy_name(self) -> str:
        if (self.strategy or "").strip().lower() in {"", "auto"}:
            return pick_auto_strategy(self.trade_type, self.asset)
        return self.strategy.strip()

    def summary(self) -> Dict[str, Any]:
        return {
            "account": self.account_mode,
            "asset": self.asset,
            "timeframe": format_tf(self.timeframe),
            "trade_type": self.trade_type,
            "duration": self.duration,
            "turbo": self.use_turbo(),
            "strategy": self.resolved_strategy_name(),
            "amount": self.amount,
            "mm_mode": self.mm_mode,
            "min_confidence": self.min_confidence,
            "min_payout": self.min_payout,
            "dry_run": self.dry_run,
            "trade_on_close": self.trade_on_close,
        }


# ===========================================================================
# Money + session risk
# ===========================================================================
class MoneyManager:
    def __init__(self, cfg: EnvConfig) -> None:
        self.mode = (cfg.mm_mode or "fixed").lower()
        self.base = float(cfg.amount)
        self.percent = float(cfg.mm_percent)
        self.factor = float(cfg.martingale_factor)
        self.steps = int(cfg.martingale_steps)
        self.step = 0
        self.current = float(cfg.amount)

    def amount(self, balance: Optional[float] = None) -> float:
        if self.mode == "percent" and balance and balance > 0:
            value = max(1.0, round(float(balance) * self.percent / 100.0, 2))
        else:
            value = round(float(self.current), 2)
        return max(1.0, value)

    def on_result(self, result: str) -> None:
        if self.mode == "martingale":
            if result == "win":
                self.step = 0
                self.current = self.base
            elif result == "loss":
                if self.step < self.steps:
                    self.step += 1
                    self.current = round(self.current * self.factor, 2)
                else:
                    self.step = 0
                    self.current = self.base
        elif self.mode in {"anti_martingale", "anti-martingale"}:
            if result == "win" and self.step < self.steps:
                self.step += 1
                self.current = round(self.current * self.factor, 2)
            else:
                self.step = 0
                self.current = self.base

    def reset(self) -> None:
        self.step = 0
        self.current = self.base


@dataclass
class SessionStats:
    start_balance: float = 0.0
    pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    equals: int = 0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    last_trade_ts: float = 0.0
    stop_reason: str = ""
    started_at: float = field(default_factory=time.time)

    @property
    def win_rate(self) -> float:
        settled = self.wins + self.losses
        return 0.0 if not settled else 100.0 * self.wins / settled


class SessionRisk:
    def __init__(self, cfg: EnvConfig, start_balance: float = 0.0) -> None:
        self.cfg = cfg
        self.stats = SessionStats(start_balance=float(start_balance or 0.0))

    def allow(self) -> tuple:
        s = self.stats
        if s.stop_reason:
            return False, s.stop_reason
        if s.trades >= self.cfg.max_trades:
            s.stop_reason = f"max trades reached ({self.cfg.max_trades})"
            return False, s.stop_reason
        if self.cfg.max_daily_loss > 0 and s.pnl <= -abs(self.cfg.max_daily_loss):
            s.stop_reason = f"max daily loss {s.pnl:.2f}"
            return False, s.stop_reason
        if self.cfg.max_daily_profit > 0 and s.pnl >= abs(self.cfg.max_daily_profit):
            s.stop_reason = f"max daily profit {s.pnl:.2f}"
            return False, s.stop_reason
        if s.consecutive_losses >= self.cfg.max_consecutive_losses:
            s.stop_reason = (
                f"{s.consecutive_losses} consecutive losses "
                f"(limit {self.cfg.max_consecutive_losses})"
            )
            return False, s.stop_reason
        return True, "ok"

    def cooldown_left(self) -> float:
        if self.cfg.cooldown_seconds <= 0 or not self.stats.last_trade_ts:
            return 0.0
        left = self.cfg.cooldown_seconds - (time.time() - self.stats.last_trade_ts)
        return max(0.0, left)

    def register(self, result: str, pnl: float) -> None:
        s = self.stats
        s.trades += 1
        s.pnl += float(pnl or 0.0)
        s.last_trade_ts = time.time()
        tag = (result or "unknown").lower()
        if tag == "win":
            s.wins += 1
            s.consecutive_wins += 1
            s.consecutive_losses = 0
        elif tag == "loss":
            s.losses += 1
            s.consecutive_losses += 1
            s.consecutive_wins = 0
        elif tag == "equal":
            s.equals += 1
        # re-evaluate stop conditions after the fill
        self.allow()

    def snapshot(self) -> Dict[str, Any]:
        s = self.stats
        return {
            "trades": s.trades,
            "wins": s.wins,
            "losses": s.losses,
            "equals": s.equals,
            "win_rate": round(s.win_rate, 2),
            "pnl": round(s.pnl, 2),
            "consecutive_losses": s.consecutive_losses,
            "stop_reason": s.stop_reason,
            "elapsed_sec": int(time.time() - s.started_at),
        }


# ===========================================================================
# Engine
# ===========================================================================
class UserBotCore:
    """Single owner of the broker session and the trading loop primitives."""

    def __init__(self, cfg: Optional[EnvConfig] = None,
                 env_path: Optional[Path] = None,
                 *, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg or EnvConfig.load(env_path)
        self.log = logger or setup_logging(self.cfg.log_level)
        self.client: Any = None
        self.strategy: Optional[Strategy] = None
        self.money = MoneyManager(self.cfg)
        self.risk = SessionRisk(self.cfg)
        self.live_asset: str = self.cfg.asset
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._last_heartbeat = 0.0
        self._catalog: Dict[str, Strategy] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def reset_stop(self) -> None:
        self._stop.clear()

    def interruptible_sleep(self, seconds: float) -> None:
        """Sleep in 200 ms slices.  Raises :class:`Interrupted` on stop/Ctrl+C."""
        if seconds <= 0:
            return
        deadline = time.time() + float(seconds)
        while True:
            if self._stop.is_set():
                raise Interrupted("stop requested")
            left = deadline - time.time()
            if left <= 0:
                return
            time.sleep(min(0.2, left))

    sleep = interruptible_sleep

    def connect(self, *, require_credentials: bool = True) -> Any:
        if require_credentials:
            self.cfg.validate_credentials()
        from iq_option_api import IQConfig, IQOptionClient
        from iq_option_api.config import Credentials, LoggingConfig, TradingLimits

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        iq_cfg = IQConfig(
            credentials=Credentials(email=self.cfg.email, password=self.cfg.password),
            account_mode=self.cfg.account_mode,
            default_asset=self.cfg.asset,
            auto_connect=False,
            limits=TradingLimits(
                min_amount=1.0,
                max_amount=20000.0,
                max_open_positions=max(1, self.cfg.max_open_positions),
                allow_real_account_trading=self.cfg.allow_real,
            ),
            logging=LoggingConfig(
                level=self.cfg.log_level,
                file=str(LOG_DIR / "iq_api.log"),
                to_console=False,
            ),
        )
        iq_cfg.connection.request_timeout = self.cfg.request_timeout
        iq_cfg.connection.connect_timeout = min(20.0, self.cfg.request_timeout)

        # Allow overriding the connection endpoints from .env (empty == defaults).
        conn = iq_cfg.connection
        if self.cfg.ws_host:
            conn.host = self.cfg.ws_host
        if self.cfg.ws_hosts:
            conn.websocket_hosts = tuple(
                h.strip() for h in self.cfg.ws_hosts.replace(",", " ").split() if h.strip())
        if self.cfg.ws_path:
            conn.websocket_path = self.cfg.ws_path
        if self.cfg.auth_host:
            conn.auth_host = self.cfg.auth_host
        if self.cfg.origin:
            conn.origin = self.cfg.origin
        if self.cfg.user_agent:
            conn.user_agent = self.cfg.user_agent
        conn.enable_ssl = self.cfg.enable_ssl

        self.client = IQOptionClient(iq_cfg)
        self.client.connect()
        if not self.client.is_authenticated:
            raise RuntimeError("login failed — check IQ_EMAIL / IQ_PASSWORD")

        if self.cfg.account_mode == "REAL":
            self.client.use_real()
        else:
            self.client.use_practice()

        try:
            self.client.start_streams()
        except Exception as exc:
            self.log.debug("live streams not started: %s", exc)

        try:
            bal = self.balance()
            self.risk = SessionRisk(self.cfg, start_balance=bal)
        except Exception:
            self.risk = SessionRisk(self.cfg)

        self.log.info("connected  account=%s  balance=%s %s",
                      self.account_type(), format_money(self.balance()),
                      self.currency())
        return self.client

    def disconnect(self) -> None:
        client = self.client
        self.client = None
        if client is None:
            return
        try:
            client.close()
        except Exception as exc:
            self.log.debug("disconnect: %s", exc)

    def ensure_alive(self) -> None:
        if self._stop.is_set():
            raise Interrupted("stop requested")
        client = self.client
        if client is not None and getattr(client, "is_connected", False) \
                and getattr(client, "is_authenticated", False):
            return
        self.log.warning("session dead — reconnecting")
        self._reconnect()

    def _reconnect(self) -> None:
        delay = max(1.0, self.cfg.reconnect_delay)
        last_err: Optional[Exception] = None
        for attempt in range(1, 8):
            if self._stop.is_set():
                raise Interrupted("stop requested")
            try:
                self.disconnect()
                self.connect()
                return
            except Interrupted:
                raise
            except Exception as exc:
                last_err = exc
                self.log.warning("reconnect %s/7 failed: %s", attempt, exc)
                self.interruptible_sleep(min(30.0, delay * attempt))
        raise RuntimeError(f"could not reconnect: {last_err}")

    # ------------------------------------------------------------------
    # Account / market helpers
    # ------------------------------------------------------------------
    def balance(self, *, refresh: bool = True) -> float:
        if self.client is None:
            return 0.0
        try:
            return float(self.client.balance(refresh=refresh))
        except Exception as exc:
            self.log.debug("balance: %s", exc)
            return 0.0

    def currency(self) -> str:
        try:
            return str(self.client.currency()) if self.client else ""
        except Exception:
            return ""

    def account_type(self) -> str:
        try:
            return str(getattr(self.client.account_type, "value", self.cfg.account_mode))
        except Exception:
            return self.cfg.account_mode

    def server_time(self) -> float:
        try:
            if self.client is not None and self.client.server_time:
                return float(self.client.server_time)
        except Exception:
            pass
        return time.time()

    def instrument_enum(self):
        from iq_option_api import InstrumentType
        return {
            "binary": InstrumentType.BINARY,
            "turbo": InstrumentType.TURBO,
            "digital": InstrumentType.DIGITAL,
            "blitz": InstrumentType.BLITZ,
        }.get(self.cfg.trade_type, InstrumentType.BINARY)

    def is_open(self, asset: Optional[str] = None) -> bool:
        if self.client is None:
            return False
        name = asset or self.live_asset or self.cfg.asset
        try:
            if self.cfg.use_turbo() and self.cfg.trade_type in {"binary", "turbo"}:
                from iq_option_api import InstrumentType
                if self.client.is_market_open(name, InstrumentType.TURBO):
                    return True
            return bool(self.client.is_market_open(name, self.instrument_enum()))
        except Exception as exc:
            self.log.debug("is_open(%s): %s", name, exc)
            return False

    def resolve_asset(self, preferred: Optional[str] = None) -> str:
        """Pick a tradable symbol, falling back through aliases and OTC."""
        names = expand_assets(preferred or self.cfg.asset)
        if self.cfg.otc_fallback:
            extra = [n if n.endswith("-OTC") else f"{n}-OTC" for n in list(names)]
            for item in extra:
                if item not in names:
                    names.append(item)
        if self.client is None:
            self.live_asset = names[0]
            return names[0]
        for name in names:
            if self._stop.is_set():
                break
            try:
                if self.is_open(name):
                    if name != self.live_asset:
                        self.log.info("asset %s is open — using it", name)
                    self.live_asset = name
                    return name
            except Exception:
                continue
        self.live_asset = names[0]
        return names[0]

    def current_payout(self, asset: Optional[str] = None) -> Optional[float]:
        if self.client is None:
            return self.cfg.payout_percent
        name = asset or self.live_asset
        try:
            if self.cfg.trade_type == "digital":
                value = self.client.digital.payout(name, "call",
                                                   period=self.cfg.duration_seconds())
            elif self.cfg.trade_type == "blitz":
                value = self.client.blitz.payout(name)
            else:
                value = self.client.binary.payout(name, turbo=self.cfg.use_turbo())
            if value is None:
                return self.cfg.payout_percent
            # some books return 0.80 instead of 80
            value = float(value)
            return value * 100.0 if 0 < value <= 2 else value
        except Exception as exc:
            self.log.debug("payout(%s): %s", name, exc)
            return self.cfg.payout_percent

    # ------------------------------------------------------------------
    # Candles
    # ------------------------------------------------------------------
    def fetch_candles(self, asset: Optional[str] = None, *,
                      size: Optional[int] = None, count: Optional[int] = None,
                      end_time: Optional[float] = None,
                      drop_forming: bool = True,
                      progress: Optional[Callable[[int, int], None]] = None) -> List[Any]:
        name = asset or self.live_asset or self.cfg.asset
        size = int(size or self.cfg.timeframe)
        count = int(count or self.cfg.candle_count)
        last_err: Optional[Exception] = None
        for attempt in range(1, self.cfg.max_fetch_retries + 1):
            if self._stop.is_set():
                raise Interrupted("stop requested")
            try:
                self.ensure_alive()
                if end_time is not None or count > 1000:
                    candles = self._page_candles(name, size, count,
                                                end_time=end_time, progress=progress)
                else:
                    candles = list(self.client.candles(name, size=size, count=count))
                if drop_forming:
                    candles = closed_candles(candles, drop_forming=True,
                                             now=self.server_time())
                if candles:
                    return candles
                last_err = RuntimeError("empty candle payload")
            except Interrupted:
                raise
            except Exception as exc:
                last_err = exc
                self.log.warning("candles %s/%s failed: %s",
                                 attempt, self.cfg.max_fetch_retries, exc)
                try:
                    self.interruptible_sleep(self.cfg.reconnect_delay * attempt)
                except Interrupted:
                    raise
                try:
                    self._reconnect()
                except Interrupted:
                    raise
                except Exception as rec_exc:
                    self.log.debug("reconnect after fetch fail: %s", rec_exc)
        raise RuntimeError(f"could not fetch candles for {name}: {last_err}")

    def _page_candles(self, asset: str, size: int, count: int, *,
                      end_time: Optional[float] = None,
                      progress: Optional[Callable[[int, int], None]] = None) -> List[Any]:
        remaining = int(count)
        cursor = end_time
        chunks: List[Any] = []
        seen = set()
        while remaining > 0:
            if self._stop.is_set():
                raise Interrupted("stop requested")
            batch_n = min(1000, remaining)
            batch = self.client.market.get_candles(asset, size, batch_n, end_time=cursor)
            if not batch:
                break
            fresh = []
            for candle in batch:
                key = (getattr(candle, "from_ts", 0), getattr(candle, "close", 0),
                       getattr(candle, "open", 0))
                if key in seen:
                    continue
                seen.add(key)
                fresh.append(candle)
            if not fresh:
                break
            chunks = fresh + chunks
            remaining = int(count) - len(chunks)
            cursor = float(fresh[0].from_ts) - 1.0
            if progress:
                try:
                    progress(len(chunks), int(count))
                except Exception:
                    pass
            if len(batch) < batch_n:
                break
            # yield so Ctrl+C stays responsive and we don't stampede the WS
            self.interruptible_sleep(0.12)
        chunks.sort(key=lambda c: getattr(c, "from_ts", 0.0))
        return chunks[-count:] if count else chunks

    def fetch_history(self, *, asset: Optional[str] = None, size: Optional[int] = None,
                      seconds: int = 86400,
                      end_time: Optional[float] = None,
                      progress: Optional[Callable[[int, int], None]] = None) -> List[Any]:
        """Download enough candles to cover ``seconds`` of wall-clock history."""
        size = int(size or self.cfg.timeframe)
        count = max(self.cfg.candle_count, int(seconds / max(1, size)) + 10)
        return self.fetch_candles(asset, size=size, count=count, end_time=end_time,
                                  drop_forming=True, progress=progress)

    def wait_candle_close(self) -> None:
        if not self.cfg.trade_on_close:
            self.interruptible_sleep(0.35)
            return
        now = self.server_time()
        tf = max(1, int(self.cfg.timeframe))
        nxt = (int(now) // tf + 1) * tf
        wait = (nxt - now) + 0.85
        if wait > 0:
            self.interruptible_sleep(wait)

    # ------------------------------------------------------------------
    # Strategy
    # ------------------------------------------------------------------
    def available_strategies(self) -> Dict[str, Strategy]:
        if not self._catalog:
            self._catalog = discover()
        return self._catalog

    def load_strategy(self, name: Optional[str] = None) -> Strategy:
        wanted = (name or self.cfg.resolved_strategy_name()).strip()
        catalog = self.available_strategies()
        self.strategy = load_strategy(wanted, available=catalog)
        self.log.info("strategy %s — %s", self.strategy.name, self.strategy.description)
        return self.strategy

    def build_context(self, asset: str, candles: Sequence[Any],
                      htf_candles: Optional[Sequence[Any]] = None,
                      payout: Optional[float] = None) -> Dict[str, Any]:
        price = None
        if candles:
            price = getattr(candles[-1], "close", None)
        return {
            "asset": asset,
            "timeframe": self.cfg.timeframe,
            "server_time": self.server_time(),
            "htf_candles": list(htf_candles or []),
            "payout": payout if payout is not None else self.cfg.payout_percent,
            "instrument": self.cfg.trade_type,
            "price": price,
            "dry_run": self.cfg.dry_run,
            "duration": self.cfg.duration,
        }

    def generate_signal(self, candles: Sequence[Any],
                        context: Optional[Dict[str, Any]] = None) -> Signal:
        if self.strategy is None:
            self.load_strategy()
        return self.strategy.safe_analyze(candles, context or {})

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def place_order(self, asset: str, direction: str, amount: float) -> Any:
        """Send exactly one order.  Callers must not retry this blindly."""
        if self.client is None:
            raise RuntimeError("not connected")
        if self.cfg.dry_run:
            raise RuntimeError("place_order called while DRY_RUN=true")
        kind = self.cfg.trade_type
        self.log.info("ORDER  %s %s %s  amount=%s  dur=%s",
                      kind, direction.upper(), asset, amount, self.cfg.duration)
        if kind == "digital":
            return self.client.digital.buy(asset, float(amount), direction,
                                           duration=int(self.cfg.duration))
        if kind == "blitz":
            return self.client.blitz.buy(asset, float(amount), direction,
                                         int(self.cfg.duration))
        return self.client.binary.buy(asset, float(amount), direction,
                                      duration=int(self.cfg.duration),
                                      turbo=self.cfg.use_turbo())

    def wait_result(self, order: Any, *, timeout: Optional[float] = None) -> Any:
        if self.client is None:
            raise RuntimeError("not connected")
        limit = float(timeout if timeout is not None
                      else self.cfg.duration_seconds() + 45.0)
        kind = self.cfg.trade_type
        # poll in slices so stop/Ctrl+C still works if the API wait is long
        slice_to = min(15.0, max(5.0, limit / 4.0))
        deadline = time.time() + limit
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            if self._stop.is_set():
                raise Interrupted("stop requested")
            remaining = max(1.0, deadline - time.time())
            try:
                if kind == "digital":
                    return self.client.digital.check_result(order, timeout=min(slice_to, remaining))
                if kind == "blitz":
                    return self.client.blitz.check_result(order, timeout=min(slice_to, remaining))
                return self.client.binary.check_result(order, timeout=min(slice_to, remaining))
            except Interrupted:
                raise
            except Exception as exc:
                last_err = exc
                # TimeoutError from the API just means "not settled yet"
                name = type(exc).__name__
                if "Timeout" not in name and "timeout" not in str(exc).lower():
                    self.log.warning("check_result: %s", exc)
                self.interruptible_sleep(1.0)
        raise RuntimeError(f"timed out waiting for trade result: {last_err}")

    def apply_result(self, signal: Signal, result: Any, amount: float) -> Dict[str, Any]:
        tag = "unknown"
        pnl = 0.0
        if result is not None:
            tag = str(getattr(result, "result", None) or "unknown").lower()
            try:
                pnl = float(getattr(result, "pnl", 0.0) or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
        self.risk.register(tag, pnl)
        self.money.on_result(tag)
        if self.strategy is not None:
            try:
                self.strategy.on_result(signal, tag, pnl, {"amount": amount})
            except Exception as exc:
                self.log.debug("strategy.on_result: %s", exc)
        return {"result": tag, "pnl": pnl}

    def execute_signal(self, signal: Signal, *, asset: Optional[str] = None,
                       amount: Optional[float] = None) -> Dict[str, Any]:
        name = asset or self.live_asset
        stake = float(amount if amount is not None else self.money.amount(self.balance()))
        if self.cfg.dry_run:
            self.log.info("DRY-RUN  %s %s  $%s  %s",
                          signal.action.upper(), name, stake, signal.reason)
            return {"dry_run": True, "signal": signal, "asset": name, "amount": stake}
        order = self.place_order(name, signal.action, stake)
        payload: Dict[str, Any] = {
            "order": order, "signal": signal, "asset": name, "amount": stake,
        }
        if self.cfg.wait_result:
            result = self.wait_result(order)
            payload["settlement"] = self.apply_result(signal, result, stake)
            payload["result"] = result
        return payload

    # ------------------------------------------------------------------
    # One live cycle
    # ------------------------------------------------------------------
    def run_once(self) -> Dict[str, Any]:
        self.ensure_alive()
        self.maybe_heartbeat()

        ok, why = self.risk.allow()
        if not ok:
            return {"skipped": True, "stop": True, "reason": why}

        cool = self.risk.cooldown_left()
        if cool > 0:
            self.interruptible_sleep(cool)

        asset = self.resolve_asset()
        if not self.is_open(asset):
            self.log.info("market closed for %s — retrying shortly", asset)
            self.interruptible_sleep(12.0)
            return {"skipped": True, "reason": f"market closed ({asset})"}

        payout = self.current_payout(asset)
        if payout is not None and payout < self.cfg.min_payout:
            self.log.info("payout %.1f%% below MIN_PAYOUT=%.1f — skip",
                          payout, self.cfg.min_payout)
            self.interruptible_sleep(8.0)
            return {"skipped": True, "reason": f"payout {payout:.1f}%"}

        self.wait_candle_close()

        need = max(self.cfg.candle_count,
                   (self.strategy.min_candles + 15) if self.strategy else 0)
        candles = self.fetch_candles(asset, count=need)
        htf: List[Any] = []
        try:
            htf_size = max(60, int(self.cfg.timeframe) * 5)
            htf = self.fetch_candles(asset, size=htf_size, count=90, drop_forming=True)
        except Exception as exc:
            self.log.debug("htf candles: %s", exc)

        context = self.build_context(asset, candles, htf, payout)
        signal = self.generate_signal(candles, context)
        if not signal.tradable or signal.confidence < self.cfg.min_confidence:
            return {
                "skipped": True,
                "reason": signal.reason or "low confidence",
                "signal": signal,
                "asset": asset,
                "payout": payout,
            }
        executed = self.execute_signal(signal, asset=asset)
        executed["payout"] = payout
        return executed

    def maybe_heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat < self.cfg.heartbeat_seconds:
            return
        self._last_heartbeat = now
        snap = self.risk.snapshot()
        self.log.info(
            "heartbeat  connected=%s  asset=%s  pnl=%+.2f  trades=%s  wr=%.0f%%",
            bool(self.client and self.client.is_connected),
            self.live_asset, snap["pnl"], snap["trades"], snap["win_rate"],
        )

    def health(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "connected": bool(self.client and getattr(self.client, "is_connected", False)),
            "authenticated": bool(self.client and getattr(self.client, "is_authenticated", False)),
            "account": self.account_type(),
            "asset": self.live_asset,
            "strategy": self.strategy.name if self.strategy else self.cfg.resolved_strategy_name(),
            "risk": self.risk.snapshot(),
        }
        if self.client is not None:
            try:
                info["api"] = self.client.health_check()
            except Exception as exc:
                info["api_error"] = str(exc)
        return info

    # context manager ---------------------------------------------------
    def __enter__(self) -> "UserBotCore":
        self.connect()
        self.load_strategy()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
        self.disconnect()


# ===========================================================================
# Logging
# ===========================================================================
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", *, name: str = "userbot") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(numeric)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(_LOG_FORMAT)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    try:
        file_handler = logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except Exception:
        pass
    logger.propagate = False
    return logger


# Re-exports used by bot.py / backtest.py
__all__ = [
    "EnvConfig", "UserBotCore", "MoneyManager", "SessionRisk", "SessionStats",
    "Interrupted", "Signal", "Strategy",
    "discover", "list_strategies", "load_strategy",
    "setup_logging", "parse_timeframe", "format_tf", "format_money",
    "expand_assets", "is_gold", "pick_auto_strategy",
    "USERBOT_DIR", "REPO_DIR", "ENV_PATH",
]
