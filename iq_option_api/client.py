"""``IQOptionClient`` - the single entry point that wires every layer together.

Canonical flow::

    CONNECT -> AUTHENTICATE -> SESSION -> ACCOUNT -> BALANCE -> MARKET
      -> ASSET -> INSTRUMENT -> PRICE/CANDLE/STREAM -> ORDER -> POSITION
      -> PORTFOLIO -> RESULT -> HISTORY -> MONITOR/RECONNECT

Example::

    from iq_option_api import IQOptionClient, load_config

    with IQOptionClient(load_config()) as iq:
        iq.use_practice()
        print(iq.balance(), iq.currency())
        result = iq.binary.buy_and_wait("EURUSD", 1, "call", 1)
        print(result.result, result.pnl)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .account import AccountManager, BalanceManager
from .auth import Authenticator
from .billing import BillingManager
from .config import IQConfig, load_config
from .connection import WebSocketClient
from .exceptions import AccountError, IQOptionError
from .history import HistoryManager
from .market import MarketManager
from .models import (
    Account,
    AccountType,
    Asset,
    Candle,
    History,
    InstrumentType,
    Order,
    Portfolio,
    Position,
    Price,
    Tick,
    TradeResult,
)
from .portfolio import PortfolioManager
from .risk import RiskManager
from .trading import (
    BinaryOptions,
    BlitzOptions,
    CFDTrading,
    CommoditiesTrading,
    CryptoTrading,
    DigitalOptions,
    ETFTrading,
    ForexTrading,
    IndicesTrading,
    OrderManager,
    PositionManager,
    StocksTrading,
)


def _setup_logging(config: IQConfig) -> logging.Logger:
    logger = logging.getLogger("iq_option_api")
    cfg = config.logging
    level = getattr(logging, str(cfg.level).upper(), logging.INFO)
    logger.setLevel(level)
    if not logger.handlers and getattr(cfg, "to_console", True):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            getattr(cfg, "format", "%(asctime)s %(levelname)-7s %(name)s: %(message)s")))
        logger.addHandler(handler)
    path = getattr(cfg, "file", None)
    if path:
        file_handler = logging.FileHandler(str(path))
        file_handler.setFormatter(logging.Formatter(
            getattr(cfg, "format", "%(asctime)s %(levelname)-7s %(name)s: %(message)s")))
        logger.addHandler(file_handler)
    return logger


class IQOptionClient:
    """High level IQ Option API client."""

    # ==================================================================
    # Construction
    # ==================================================================
    def __init__(self, config: Optional[IQConfig] = None, **overrides: Any) -> None:
        self.config = config or load_config(**overrides)
        self.config.validate()
        self.log = _setup_logging(self.config)

        # --- connection -------------------------------------------------
        self.ws = WebSocketClient(self.config.connection,
                                  reconnect=self.config.reconnect,
                                  logger=self.log.getChild("ws"))

        # --- auth / session --------------------------------------------
        self.auth = Authenticator(self.ws, self.config,
                                  logger=self.log.getChild("auth"))

        # --- account / balance / billing --------------------------------
        self.balances = BalanceManager(self.ws, logger=self.log.getChild("balance"))
        self.accounts = AccountManager(self.ws, self.balances,
                                       logger=self.log.getChild("account"))
        self.billing = BillingManager(self.ws, self.balances,
                                      logger=self.log.getChild("billing"))

        # --- market ------------------------------------------------------
        self.market = MarketManager(self.ws, logger=self.log.getChild("market"))

        # --- positions / risk / orders ------------------------------------
        self.positions = PositionManager(self.ws, logger=self.log.getChild("positions"))
        self.risk = RiskManager(self.config.limits, accounts=self.accounts,
                                market=self.market, positions=self.positions,
                                logger=self.log.getChild("risk"))
        self.orders = OrderManager(self.ws, risk_manager=self.risk,
                                   logger=self.log.getChild("orders"))

        # --- trading products ---------------------------------------------
        common = (self.ws, self.market, self.accounts, self.orders, self.positions)
        self.binary = BinaryOptions(*common, logger=self.log.getChild("binary"))
        self.digital = DigitalOptions(*common, logger=self.log.getChild("digital"))
        self.blitz = BlitzOptions(*common, logger=self.log.getChild("blitz"))
        self.forex = ForexTrading(*common, logger=self.log.getChild("forex"))
        self.cfd = CFDTrading(*common, logger=self.log.getChild("cfd"))
        self.stocks = StocksTrading(*common, logger=self.log.getChild("stocks"))
        self.crypto = CryptoTrading(*common, cfd=self.cfd,
                                    logger=self.log.getChild("crypto"))
        self.commodities = CommoditiesTrading(*common,
                                              logger=self.log.getChild("commodities"))
        self.etf = ETFTrading(*common, logger=self.log.getChild("etf"))
        self.indices = IndicesTrading(*common, logger=self.log.getChild("indices"))

        # --- portfolio / history --------------------------------------------
        self.portfolio = PortfolioManager(self.ws, self.accounts, self.positions,
                                          logger=self.log.getChild("portfolio"))
        self.history = HistoryManager(self.ws, self.accounts, self.positions,
                                      logger=self.log.getChild("history"))

        # --- lifecycle hooks --------------------------------------------------
        self._lock = threading.RLock()
        self._streams_started = False
        self.on_reconnected: Optional[Callable[[], None]] = None
        self.ws.on_reconnected = self._handle_reconnected
        self.ws.on_disconnected = self._handle_disconnected

        if self.config.auto_connect:
            self.connect()

    # ==================================================================
    # 1. Connection
    # ==================================================================
    def connect(self, *, authenticate: bool = True,
                two_factor_code: Optional[str] = None) -> bool:
        """CONNECT (+ AUTHENTICATE) - the first step of every session.

        When ``authenticate`` is true we log in over HTTPS first (Firefox
        impersonation + cookies) and only then open the websocket.  Opening
        the socket first is how Cloudflare used to time the handshake out.
        """
        if authenticate:
            return self.login(two_factor_code=two_factor_code)
        if not self.ws.is_connected:
            self.ws.connect()
        return self.ws.is_connected

    @property
    def is_connected(self) -> bool:
        return self.ws.is_connected

    def connection_status(self) -> Dict[str, Any]:
        return self.ws.status()

    def disconnect(self) -> None:
        self.close()

    def close(self) -> None:
        """Tear everything down in reverse order."""
        try:
            self.portfolio.unsubscribe()
        except Exception:
            pass
        try:
            self.market.close()
        except Exception:
            pass
        try:
            self.balances.unsubscribe_updates()
        except Exception:
            pass
        self.ws.close()
        self.log.info("client closed")

    # ==================================================================
    # 2. Authentication / session
    # ==================================================================
    def login(self, email: Optional[str] = None, password: Optional[str] = None,
              *, force: bool = False, two_factor_code: Optional[str] = None) -> bool:
        if email or password:
            self.auth.login(email, password, two_factor_code=two_factor_code)
            ok = self.auth.authenticate()
        else:
            ok = self.auth.connect_and_authenticate(force_login=force,
                                                    two_factor_code=two_factor_code)
        if ok:
            self._after_login()
        return ok

    def logout(self) -> None:
        self.auth.logout()

    @property
    def is_authenticated(self) -> bool:
        return self.auth.is_authenticated

    @property
    def ssid(self) -> Optional[str]:
        return self.auth.ssid

    def session_status(self) -> Dict[str, Any]:
        return self.auth.state()

    def get_profile(self, *, refresh: bool = False) -> Dict[str, Any]:
        """User profile - cached from the auth handshake, ``get-profile`` on refresh."""
        if refresh or not self.auth.profile:
            return self.auth.fetch_profile()
        return self.auth.profile

    def ensure_authenticated(self) -> bool:
        return self.auth.ensure_authenticated()

    def _after_login(self) -> None:
        """Select the configured account right after a successful auth."""
        try:
            self.use_account(self.config.account_mode)
        except Exception as exc:
            self.log.warning("could not select the configured account: %s", exc)

    # ==================================================================
    # 3. Account / balance
    # ==================================================================
    def list_accounts(self, *, refresh: bool = True) -> List[Account]:
        return self.accounts.list_accounts(refresh=refresh)

    def use_account(self, account_type: "AccountType | str") -> Account:
        """Switch account - always verified against server data."""
        return self.accounts.use_account(account_type)

    def use_practice(self) -> Account:
        return self.use_account(AccountType.PRACTICE)

    def change_balance(self, balance_mode: str = "PRACTICE") -> Account:
        """Switch account by mode name - ``"PRACTICE"`` / ``"REAL"``.

        The balance id is resolved from the server's ``get-balances`` list
        (PRACTICE = type 4, REAL = type 1) and the switch is verified.
        """
        return self.accounts.change_balance(balance_mode)

    use_demo = use_practice

    def use_real(self) -> Account:
        if not self.config.limits.allow_real_account_trading:
            self.log.warning("real account selected while real-account trading is "
                             "disabled in the risk limits - orders will be rejected")
        return self.use_account(AccountType.REAL)

    def active_account(self, *, refresh: bool = False) -> Account:
        return self.accounts.active_account(refresh=refresh)

    @property
    def user_balance_id(self) -> int:
        return self.accounts.user_balance_id

    @property
    def account_type(self) -> AccountType:
        return self.accounts.account_type

    @property
    def is_demo(self) -> bool:
        return self.accounts.is_demo

    def balance(self, *, refresh: bool = True) -> float:
        return self.accounts.balance(refresh=refresh)

    def currency(self) -> str:
        return self.accounts.currency()

    def account_statistics(self) -> Dict[str, Any]:
        return self.accounts.statistics()

    def billing_balances(self) -> List[Any]:
        """Raw billing balances - informational, never the trading balance."""
        return self.billing.get_balances()

    # ==================================================================
    # 4. Market / assets
    # ==================================================================
    @property
    def server_time(self) -> float:
        return self.market.server_time

    def sync_time(self) -> float:
        return self.market.sync_time()

    def assets(self, instrument_type: "InstrumentType | str",
               *, only_open: bool = False, refresh: bool = False) -> List[Asset]:
        itype = (instrument_type if isinstance(instrument_type, InstrumentType)
                 else InstrumentType(str(instrument_type)))
        return self.market.list_assets(itype, only_open=only_open, refresh=refresh)

    def is_market_open(self, asset: "str | int",
                       instrument_type: "InstrumentType | str") -> bool:
        itype = (instrument_type if isinstance(instrument_type, InstrumentType)
                 else InstrumentType(str(instrument_type)))
        return self.market.is_open(asset, itype)

    def price(self, asset: "str | int",
              instrument_type: Optional[InstrumentType] = None) -> Price:
        return self.market.price(asset, instrument_type)

    def bid_ask(self, asset: "str | int",
                instrument_type: Optional[InstrumentType] = None) -> Dict[str, Any]:
        return self.market.bid_ask(asset, instrument_type)

    def candles(self, asset: "str | int", size: int = 60, count: int = 100) -> List[Candle]:
        return self.market.get_candles(asset, size, count)

    def historical_data(self, asset: "str | int", size: int, count: int) -> List[Candle]:
        return self.market.historical_data(asset, size, count)

    def subscribe_ticks(self, asset: "str | int", callback=None):
        return self.market.subscribe_ticks(asset, None, callback)

    def subscribe_candles(self, asset: "str | int", size: int = 60, callback=None):
        return self.market.subscribe_candles(asset, size, callback)

    def get_instruments(self, instrument_type: str = "binary") -> Dict[str, Any]:
        return self.market.get_instruments(instrument_type)

    def top_assets(self, instrument_type: str = "binary") -> Dict[str, Any]:
        return self.market.top_assets(instrument_type)

    def subscribe_traders_mood(self, asset: "str | int",
                               instrument: str = "binary", callback=None):
        return self.market.subscribe_traders_mood(asset, instrument, callback)

    # ==================================================================
    # 5. Portfolio / positions / history
    # ==================================================================
    def start_streams(self, callback: Optional[Callable[[Position], None]] = None) -> None:
        """Subscribe to ``portfolio.position-changed`` and balance updates."""
        with self._lock:
            if self._streams_started:
                return
            self.portfolio.subscribe(callback)
            self.balances.subscribe_updates()
            self._streams_started = True
            self.log.info("live streams started")

    def stop_streams(self) -> None:
        with self._lock:
            self.portfolio.unsubscribe()
            self.balances.unsubscribe_updates()
            self._streams_started = False

    def open_positions(self, instrument_type: Optional[InstrumentType] = None
                       ) -> List[Position]:
        return self.portfolio.open_positions(instrument_type=instrument_type)

    def get_portfolio(self, *, refresh: bool = True) -> Portfolio:
        return self.portfolio.snapshot(refresh=refresh)

    def portfolio_summary(self) -> Dict[str, Any]:
        return self.portfolio.summary()

    def close_position(self, position_id: int) -> bool:
        return self.positions.close(position_id)

    def close_all_positions(self, instrument_type: Optional[InstrumentType] = None) -> int:
        return self.positions.close_all(instrument_type=instrument_type)

    def get_history(self, *, limit: int = 50, **kwargs: Any) -> History:
        return self.history.get_history(limit=limit, **kwargs)

    def trade_statistics(self) -> Dict[str, Any]:
        return self.history.statistics()

    # ==================================================================
    # 6. Orders
    # ==================================================================
    def order_status(self, order_id: int) -> Optional[str]:
        return self.orders.status(order_id)

    def order_history(self, limit: int = 100) -> List[Order]:
        return self.orders.history(limit)

    def cancel_order(self, order_id: int, **kwargs: Any) -> bool:
        return self.orders.cancel(order_id, **kwargs)

    # ==================================================================
    # 7. Risk
    # ==================================================================
    def enable_trading(self) -> None:
        self.risk.enable_trading()

    def disable_trading(self, reason: str = "manually disabled") -> None:
        self.risk.disable_trading(reason)

    emergency_stop = disable_trading

    def risk_status(self) -> Dict[str, Any]:
        return self.risk.status()

    # ==================================================================
    # 8. Monitoring / reconnect
    # ==================================================================
    def _handle_reconnected(self) -> None:
        """Re-authenticate and restore account + streams after a reconnect."""
        self.log.warning("websocket reconnected - restoring session")
        try:
            self.auth.authenticate()
        except Exception as exc:
            self.log.error("re-authentication failed: %s", exc)
            try:
                self.auth.relogin()
            except Exception as exc2:
                self.log.error("re-login failed: %s", exc2)
                return

        balance_id = self.accounts.active_balance_id
        if balance_id:
            try:
                self.accounts.use_balance_id(balance_id, verify=True)
            except Exception as exc:
                self.log.error("could not restore the active account: %s", exc)

        if self._streams_started:
            self._streams_started = False
            try:
                self.start_streams()
            except Exception as exc:
                self.log.error("could not restart streams: %s", exc)

        if self.on_reconnected:
            try:
                self.on_reconnected()
            except Exception as exc:
                self.log.warning("user on_reconnected hook failed: %s", exc)

    def _handle_disconnected(self) -> None:
        self.log.warning("websocket disconnected")

    def health_check(self) -> Dict[str, Any]:
        return {
            "connected": self.is_connected,
            "authenticated": self.is_authenticated,
            "account_type": self.account_type.value,
            "balance_id": self.accounts.active_balance_id,
            "server_time": self.server_time,
            "time_offset": self.ws.time_offset,
            "reconnects": self.ws.reconnect_count,
            "subscriptions": len(self.ws.subscriptions),
            "trading_enabled": self.risk.trading_enabled,
            "open_positions": len(self.positions.all()),
        }

    def status(self) -> Dict[str, Any]:
        return {
            "connection": self.connection_status(),
            "session": self.session_status(),
            "account": self.accounts.status(),
            "risk": self.risk_status(),
        }

    # ==================================================================
    # Context manager
    # ==================================================================
    def __enter__(self) -> "IQOptionClient":
        if not self.is_connected:
            self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"IQOptionClient(connected={self.is_connected}, "
                f"authenticated={self.is_authenticated}, "
                f"account={self.account_type.value})")
