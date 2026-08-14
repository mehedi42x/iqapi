"""iq_option_api - a modular, layered trading API for IQ Option.

Layers
------
``connection``  websocket transport, protocol frames, subscriptions
``auth``        login, SSID lifecycle, session validation
``account``     accounts, active ``user_balance_id``, balances
``billing``     raw ``get-balances`` (kept apart from trading)
``market``      assets, market status, prices, candles, instruments
``trading``     binary, digital, blitz, forex, cfd, stocks, crypto,
                commodities, etf, indices + order/position management
``portfolio``   ``portfolio.get-positions`` / ``get-stats`` / ``position-changed``
``history``     closed-trade history for every instrument
``risk``        pre-trade validation and the trading kill switch
``models``      standardized dataclasses returned by every layer
``config``      configuration from env / file / kwargs
``exceptions``  the error hierarchy

Quick start::

    from iq_option_api import IQOptionClient

    with IQOptionClient() as iq:          # credentials come from the config/env
        iq.use_practice()
        print(iq.balance(), iq.currency())
"""

from .account import AccountManager, BalanceManager
from .auth import Authenticator, Session
from .billing import BillingManager
from .client import IQOptionClient
from .config import (
    ConnectionConfig,
    Credentials,
    IQConfig,
    LoggingConfig,
    ReconnectPolicy,
    SessionStoreConfig,
    TradingLimits,
    load_config,
)
from .connection import Protocol, Subscription, SubscriptionManager, WebSocketClient
from .exceptions import (
    AccountError,
    AssetError,
    AuthenticationError,
    BalanceError,
    IQConnectionError,
    IQOptionError,
    IQTimeoutError,
    InstrumentError,
    MarketError,
    OrderError,
    PositionError,
    ProtocolError,
    SessionError,
    TwoFactorRequired,
)
from .history import HistoryManager
from .market import MarketManager
from .models import (
    Account,
    AccountType,
    Asset,
    Balance,
    BlitzOption,
    Candle,
    DigitalStrike,
    Direction,
    Expiration,
    History,
    Instrument,
    InstrumentType,
    MarketStatus,
    Order,
    OrderState,
    OrderType,
    Portfolio,
    PortfolioStats,
    Position,
    PositionState,
    Price,
    Tick,
    Trade,
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
    MarginalTrading,
    OrderManager,
    PositionManager,
    StocksTrading,
)

__version__ = "1.0.0"

__all__ = [
    # entry point
    "IQOptionClient",
    # config
    "IQConfig", "load_config", "Credentials", "ConnectionConfig",
    "ReconnectPolicy", "TradingLimits", "SessionStoreConfig", "LoggingConfig",
    # connection
    "WebSocketClient", "Protocol", "Subscription", "SubscriptionManager",
    # auth / account / billing
    "Authenticator", "Session", "AccountManager", "BalanceManager", "BillingManager",
    # market
    "MarketManager",
    # trading
    "OrderManager", "PositionManager", "BinaryOptions", "DigitalOptions",
    "BlitzOptions", "MarginalTrading", "ForexTrading", "CFDTrading",
    "StocksTrading", "CryptoTrading", "CommoditiesTrading", "ETFTrading",
    "IndicesTrading",
    # portfolio / history / risk
    "PortfolioManager", "HistoryManager", "RiskManager",
    # models
    "Account", "AccountType", "Asset", "Balance", "BlitzOption", "Candle",
    "DigitalStrike", "Direction", "Expiration", "History", "Instrument",
    "InstrumentType", "MarketStatus", "Order", "OrderState", "OrderType",
    "Portfolio", "PortfolioStats", "Position", "PositionState", "Price",
    "Tick", "Trade", "TradeResult",
    # exceptions
    "IQOptionError", "AuthenticationError", "TwoFactorRequired", "SessionError",
    "IQConnectionError", "IQTimeoutError", "AccountError", "BalanceError",
    "MarketError", "AssetError", "InstrumentError", "OrderError",
    "PositionError", "ProtocolError",
    "__version__",
]
