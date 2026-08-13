"""Standardized data models.

Everything the API returns to the application is one of these objects (or a
list of them) - never a raw protocol dict.  Each model keeps the original
payload in ``raw`` so nothing is lost, and exposes ``from_payload`` builders
that are tolerant to the field-name variations of the IQ Option protocol.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ==========================================================================
# Enums
# ==========================================================================
class AccountType(str, Enum):
    REAL = "REAL"
    PRACTICE = "PRACTICE"
    TOURNAMENT = "TOURNAMENT"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_type_id(cls, type_id: Any) -> "AccountType":
        mapping = {1: cls.REAL, 4: cls.PRACTICE, 2: cls.TOURNAMENT}
        try:
            return mapping.get(int(type_id), cls.UNKNOWN)
        except (TypeError, ValueError):
            return cls.UNKNOWN

    @property
    def type_id(self) -> int:
        return {AccountType.REAL: 1, AccountType.PRACTICE: 4,
                AccountType.TOURNAMENT: 2}.get(self, 0)


class Direction(str, Enum):
    CALL = "call"
    PUT = "put"
    BUY = "buy"
    SELL = "sell"

    @property
    def is_long(self) -> bool:
        return self in (Direction.CALL, Direction.BUY)

    @classmethod
    def parse(cls, value: Any) -> "Direction":
        if isinstance(value, cls):
            return value
        text = str(value).strip().lower()
        aliases = {
            "call": cls.CALL, "up": cls.CALL, "higher": cls.CALL, "1": cls.CALL,
            "put": cls.PUT, "down": cls.PUT, "lower": cls.PUT, "2": cls.PUT,
            "buy": cls.BUY, "long": cls.BUY,
            "sell": cls.SELL, "short": cls.SELL,
        }
        if text not in aliases:
            raise ValueError(f"unknown direction: {value!r}")
        return aliases[text]


class InstrumentType(str, Enum):
    BINARY = "binary"
    TURBO = "turbo"
    DIGITAL = "digital-option"
    BLITZ = "blitz-option"
    FOREX = "forex"
    CFD = "cfd"
    STOCK = "stock"
    CRYPTO = "crypto"
    COMMODITY = "commodity"
    ETF = "etf"
    INDEX = "index"
    UNKNOWN = "unknown"


class OrderState(str, Enum):
    CREATED = "created"
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class PositionState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    UNKNOWN = "unknown"


# ==========================================================================
# Helpers
# ==========================================================================
def _first(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_ts(value: Any) -> Optional[float]:
    """IQ Option mixes seconds, milliseconds, microseconds and nanoseconds."""
    ts = _to_float(value)
    if ts is None or ts == 0:
        return None
    while ts > 1e11:
        ts /= 1000.0
    return ts


@dataclass
class _Base:
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if key == "raw":
                continue
            if isinstance(value, Enum):
                out[key] = value.value
            elif isinstance(value, list):
                out[key] = [v.to_dict() if hasattr(v, "to_dict") else v for v in value]
            elif hasattr(value, "to_dict"):
                out[key] = value.to_dict()
            else:
                out[key] = value
        return out


# ==========================================================================
# Account / balance
# ==========================================================================
@dataclass
class Balance(_Base):
    balance_id: int = 0
    type_id: int = 0
    type: AccountType = AccountType.UNKNOWN
    amount: float = 0.0
    currency: str = ""
    user_id: Optional[int] = None
    enrolled_amount: Optional[float] = None
    enrolled_sum_amount: Optional[float] = None
    is_fiat: bool = True
    is_marginal: bool = False
    auth_amount: Optional[float] = None

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "Balance":
        type_id = _to_int(_first(payload, "type", "type_id"), 0) or 0
        return cls(
            balance_id=_to_int(_first(payload, "id", "balance_id", "user_balance_id"), 0) or 0,
            type_id=type_id,
            type=AccountType.from_type_id(type_id),
            amount=_to_float(_first(payload, "amount", "balance"), 0.0) or 0.0,
            currency=str(_first(payload, "currency", "currency_code", default="")),
            user_id=_to_int(payload.get("user_id")),
            enrolled_amount=_to_float(payload.get("enrolled_amount")),
            enrolled_sum_amount=_to_float(payload.get("enrolled_sum_amount")),
            is_fiat=bool(payload.get("is_fiat", True)),
            is_marginal=bool(payload.get("is_marginal", False)),
            auth_amount=_to_float(payload.get("auth_amount")),
            raw=payload,
        )


@dataclass
class Account(_Base):
    """A tradable account.  ``balance_id`` is the ``user_balance_id`` used by
    every order request - it is always taken from server data."""

    balance_id: int = 0
    type: AccountType = AccountType.UNKNOWN
    currency: str = ""
    amount: float = 0.0
    user_id: Optional[int] = None
    is_active: bool = False
    status: str = "unknown"

    @property
    def user_balance_id(self) -> int:
        return self.balance_id

    @property
    def is_demo(self) -> bool:
        return self.type is AccountType.PRACTICE

    @classmethod
    def from_balance(cls, balance: Balance, *, is_active: bool = False) -> "Account":
        return cls(
            balance_id=balance.balance_id,
            type=balance.type,
            currency=balance.currency,
            amount=balance.amount,
            user_id=balance.user_id,
            is_active=is_active,
            status="active" if is_active else "available",
            raw=balance.raw,
        )


# ==========================================================================
# Market
# ==========================================================================
@dataclass
class MarketStatus(_Base):
    asset_id: int = 0
    name: str = ""
    is_open: bool = False
    instrument_type: InstrumentType = InstrumentType.UNKNOWN
    open_time: Optional[float] = None
    close_time: Optional[float] = None
    schedule: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    @property
    def opens_at(self) -> Optional[datetime]:
        return datetime.fromtimestamp(self.open_time, tz=timezone.utc) if self.open_time else None

    @property
    def closes_at(self) -> Optional[datetime]:
        return datetime.fromtimestamp(self.close_time, tz=timezone.utc) if self.close_time else None


@dataclass
class Asset(_Base):
    asset_id: int = 0
    name: str = ""
    description: str = ""
    instrument_type: InstrumentType = InstrumentType.UNKNOWN
    group: str = ""
    is_enabled: bool = True
    is_suspended: bool = False
    precision: int = 6
    minimal_amount: Optional[float] = None
    maximal_amount: Optional[float] = None
    profit_percent: Optional[float] = None
    schedule: List[Dict[str, Any]] = field(default_factory=list)
    market_status: Optional[MarketStatus] = None

    @property
    def is_open(self) -> bool:
        if self.market_status is not None:
            return self.market_status.is_open
        return self.is_enabled and not self.is_suspended

    @classmethod
    def from_payload(cls, payload: Dict[str, Any],
                     instrument_type: InstrumentType = InstrumentType.UNKNOWN) -> "Asset":
        name = str(_first(payload, "name", "ticker", "symbol", "active", default=""))
        if name.startswith("front."):
            name = name[len("front."):]
        return cls(
            asset_id=_to_int(_first(payload, "id", "active_id", "asset_id"), 0) or 0,
            name=name,
            description=str(_first(payload, "description", "localization_name", default="")),
            instrument_type=instrument_type,
            group=str(_first(payload, "group_name", "group", default="")),
            is_enabled=bool(payload.get("enabled", payload.get("is_enabled", True))),
            is_suspended=bool(payload.get("is_suspended", False)),
            precision=_to_int(payload.get("precision"), 6) or 6,
            minimal_amount=_to_float(_first(payload, "minimal_amount", "min_amount")),
            maximal_amount=_to_float(_first(payload, "maximal_amount", "max_amount")),
            profit_percent=_to_float(_first(payload, "profit_commission", "profit_percent")),
            schedule=list(payload.get("schedule", []) or []),
            raw=payload,
        )


@dataclass
class Expiration(_Base):
    timestamp: float = 0.0
    period: int = 0                     # seconds
    index: Optional[int] = None
    is_available: bool = True

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.timestamp - time.time())

    @property
    def minutes(self) -> int:
        return max(1, int(round(self.period / 60))) if self.period else 0


@dataclass
class DigitalStrike(_Base):
    """One strike of a digital option, both directions."""

    value: float = 0.0
    symbol_call: str = ""
    symbol_put: str = ""
    instrument_id_call: str = ""
    instrument_id_put: str = ""
    price_call: Optional[float] = None
    price_put: Optional[float] = None
    profit_call: Optional[float] = None
    profit_put: Optional[float] = None

    def instrument_id(self, direction: Direction) -> str:
        return self.instrument_id_call if direction.is_long else self.instrument_id_put

    def symbol(self, direction: Direction) -> str:
        return self.symbol_call if direction.is_long else self.symbol_put

    def profit(self, direction: Direction) -> Optional[float]:
        return self.profit_call if direction.is_long else self.profit_put


@dataclass
class Instrument(_Base):
    """Common abstraction over every tradable instrument."""

    instrument_id: str = ""
    asset_id: int = 0
    symbol: str = ""
    instrument_type: InstrumentType = InstrumentType.UNKNOWN
    expiration: Optional[Expiration] = None
    strike: Optional[float] = None
    direction: Optional[Direction] = None
    price: Optional[float] = None
    payout: Optional[float] = None
    leverage: Optional[int] = None
    is_tradable: bool = True
    index: Optional[int] = None
    min_amount: Optional[float] = None
    max_amount: Optional[float] = None

    @property
    def expiration_timestamp(self) -> Optional[float]:
        return self.expiration.timestamp if self.expiration else None


@dataclass
class BlitzOption(_Base):
    asset_id: int = 0
    name: str = ""
    duration: int = 0                 # seconds (5/10/15/30/60)
    profit_percent: Optional[float] = None
    is_open: bool = False
    durations: List[int] = field(default_factory=list)


# ==========================================================================
# Prices
# ==========================================================================
@dataclass
class Price(_Base):
    asset_id: int = 0
    symbol: str = ""
    bid: Optional[float] = None
    ask: Optional[float] = None
    value: Optional[float] = None
    timestamp: float = 0.0
    volume: Optional[float] = None
    phase: str = ""

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.value

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "Price":
        return cls(
            asset_id=_to_int(_first(payload, "active_id", "asset_id", "id"), 0) or 0,
            symbol=str(_first(payload, "symbol", "name", default="")),
            bid=_to_float(payload.get("bid")),
            ask=_to_float(payload.get("ask")),
            value=_to_float(_first(payload, "value", "price", "close")),
            timestamp=_normalize_ts(_first(payload, "at", "time", "timestamp")) or time.time(),
            volume=_to_float(payload.get("volume")),
            phase=str(payload.get("phase", "")),
            raw=payload,
        )


@dataclass
class Tick(_Base):
    asset_id: int = 0
    symbol: str = ""
    value: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    timestamp: float = 0.0

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "Tick":
        return cls(
            asset_id=_to_int(_first(payload, "active_id", "asset_id"), 0) or 0,
            symbol=str(_first(payload, "symbol", "name", default="")),
            value=_to_float(_first(payload, "value", "close", "price"), 0.0) or 0.0,
            bid=_to_float(payload.get("bid")),
            ask=_to_float(payload.get("ask")),
            timestamp=_normalize_ts(_first(payload, "at", "time", "timestamp")) or time.time(),
            raw=payload,
        )


@dataclass
class Candle(_Base):
    asset_id: int = 0
    size: int = 0                      # seconds
    from_ts: float = 0.0
    to_ts: float = 0.0
    open: float = 0.0
    close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    at: Optional[float] = None

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def color(self) -> str:
        if self.close > self.open:
            return "green"
        if self.close < self.open:
            return "red"
        return "doji"

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.from_ts, tz=timezone.utc)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "Candle":
        return cls(
            asset_id=_to_int(_first(payload, "active_id", "asset_id"), 0) or 0,
            size=_to_int(_first(payload, "size", "period"), 0) or 0,
            from_ts=_normalize_ts(_first(payload, "from", "from_ts")) or 0.0,
            to_ts=_normalize_ts(_first(payload, "to", "to_ts")) or 0.0,
            open=_to_float(payload.get("open"), 0.0) or 0.0,
            close=_to_float(payload.get("close"), 0.0) or 0.0,
            high=_to_float(_first(payload, "max", "high"), 0.0) or 0.0,
            low=_to_float(_first(payload, "min", "low"), 0.0) or 0.0,
            volume=_to_float(payload.get("volume"), 0.0) or 0.0,
            at=_normalize_ts(payload.get("at")),
            raw=payload,
        )


# ==========================================================================
# Orders / positions
# ==========================================================================
@dataclass
class Order(_Base):
    order_id: Optional[int] = None
    request_id: Optional[str] = None
    instrument_id: str = ""
    instrument_type: InstrumentType = InstrumentType.UNKNOWN
    asset_id: int = 0
    symbol: str = ""
    direction: Optional[Direction] = None
    amount: float = 0.0
    order_type: OrderType = OrderType.MARKET
    state: OrderState = OrderState.CREATED
    price: Optional[float] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    leverage: Optional[int] = None
    balance_id: Optional[int] = None
    position_id: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    message: str = ""

    @property
    def is_accepted(self) -> bool:
        return self.state in (OrderState.FILLED, OrderState.PENDING) and self.order_id is not None

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], **defaults: Any) -> "Order":
        order = cls(**defaults)
        order.order_id = _to_int(_first(payload, "id", "order_id"), order.order_id)
        order.position_id = _to_int(_first(payload, "position_id", "external_id"), order.position_id)
        order.instrument_id = str(_first(payload, "instrument_id", default=order.instrument_id))
        order.asset_id = _to_int(_first(payload, "active_id", "asset_id"), order.asset_id) or order.asset_id
        order.amount = _to_float(_first(payload, "amount", "count", "price_value"), order.amount) or order.amount
        order.price = _to_float(_first(payload, "price", "open_price"), order.price)
        order.stop_loss = _to_float(payload.get("stop_lose_value"), order.stop_loss)
        order.take_profit = _to_float(payload.get("take_profit_value"), order.take_profit)
        order.leverage = _to_int(payload.get("leverage"), order.leverage)
        order.balance_id = _to_int(_first(payload, "user_balance_id", "balance_id"), order.balance_id)
        status = str(_first(payload, "status", "state", default="")).lower()
        order.state = {
            "filled": OrderState.FILLED, "closed": OrderState.FILLED,
            "pending": OrderState.PENDING, "created": OrderState.CREATED,
            "rejected": OrderState.REJECTED, "canceled": OrderState.CANCELLED,
            "cancelled": OrderState.CANCELLED, "expired": OrderState.EXPIRED,
        }.get(status, order.state if status == "" else OrderState.UNKNOWN)
        order.raw = payload
        return order


@dataclass
class Position(_Base):
    position_id: Optional[int] = None
    external_id: Optional[int] = None
    order_ids: List[int] = field(default_factory=list)
    instrument_id: str = ""
    instrument_type: InstrumentType = InstrumentType.UNKNOWN
    asset_id: int = 0
    symbol: str = ""
    direction: Optional[Direction] = None
    amount: float = 0.0
    quantity: Optional[float] = None
    open_price: Optional[float] = None
    current_price: Optional[float] = None
    close_price: Optional[float] = None
    invest: float = 0.0
    pnl: Optional[float] = None
    pnl_net: Optional[float] = None
    pnl_realized: Optional[float] = None
    sell_profit: Optional[float] = None
    expected_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    leverage: Optional[int] = None
    margin: Optional[float] = None
    swap: Optional[float] = None
    commission: Optional[float] = None
    balance_id: Optional[int] = None
    state: PositionState = PositionState.UNKNOWN
    open_time: Optional[float] = None
    close_time: Optional[float] = None
    expiration_time: Optional[float] = None
    close_reason: str = ""
    currency: str = ""

    @property
    def is_open(self) -> bool:
        return self.state is PositionState.OPEN

    @property
    def floating_pnl(self) -> Optional[float]:
        return self.pnl_net if self.pnl_net is not None else self.pnl

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "Position":
        status = str(_first(payload, "status", "state", default="")).lower()
        state = {
            "open": PositionState.OPEN, "opened": PositionState.OPEN,
            "closing": PositionState.CLOSING,
            "closed": PositionState.CLOSED, "sold": PositionState.CLOSED,
        }.get(status, PositionState.UNKNOWN)

        raw_type = str(_first(payload, "instrument_type", "type", default="")).lower()
        try:
            itype = InstrumentType(raw_type)
        except ValueError:
            itype = {
                "turbo-option": InstrumentType.TURBO,
                "binary-option": InstrumentType.BINARY,
                "digital-option": InstrumentType.DIGITAL,
                "blitz-option": InstrumentType.BLITZ,
                "marginal-forex": InstrumentType.FOREX,
                "marginal-cfd": InstrumentType.CFD,
                "marginal-crypto": InstrumentType.CRYPTO,
            }.get(raw_type, InstrumentType.UNKNOWN)

        direction = None
        raw_dir = _first(payload, "direction", "side", "instrument_dir")
        if raw_dir is not None:
            try:
                direction = Direction.parse(raw_dir)
            except ValueError:
                direction = None

        order_ids = payload.get("order_ids") or []
        if not isinstance(order_ids, list):
            order_ids = [order_ids]

        return cls(
            position_id=_to_int(_first(payload, "id", "position_id")),
            external_id=_to_int(payload.get("external_id")),
            order_ids=[i for i in (_to_int(o) for o in order_ids) if i is not None],
            instrument_id=str(payload.get("instrument_id", "")),
            instrument_type=itype,
            asset_id=_to_int(_first(payload, "active_id", "asset_id"), 0) or 0,
            symbol=str(_first(payload, "symbol", "active", default="")),
            direction=direction,
            amount=_to_float(_first(payload, "amount", "count"), 0.0) or 0.0,
            quantity=_to_float(payload.get("quantity")),
            open_price=_to_float(_first(payload, "open_price", "buy_amount", "avg_price")),
            current_price=_to_float(_first(payload, "current_price", "currentPrice")),
            close_price=_to_float(payload.get("close_price")),
            invest=_to_float(_first(payload, "invest", "amount"), 0.0) or 0.0,
            pnl=_to_float(payload.get("pnl")),
            pnl_net=_to_float(payload.get("pnl_net")),
            pnl_realized=_to_float(payload.get("pnl_realized")),
            sell_profit=_to_float(payload.get("sell_profit")),
            expected_profit=_to_float(payload.get("expected_profit")),
            stop_loss=_to_float(_first(payload, "stop_lose_value", "stop_loss_value")),
            take_profit=_to_float(payload.get("take_profit_value")),
            leverage=_to_int(payload.get("leverage")),
            margin=_to_float(payload.get("margin")),
            swap=_to_float(payload.get("swap")),
            commission=_to_float(_first(payload, "commission", "open_commission")),
            balance_id=_to_int(_first(payload, "user_balance_id", "balance_id")),
            state=state,
            open_time=_normalize_ts(_first(payload, "open_time", "created_at")),
            close_time=_normalize_ts(payload.get("close_time")),
            expiration_time=_normalize_ts(_first(payload, "expiration_time", "expired")),
            close_reason=str(payload.get("close_reason", "")),
            currency=str(payload.get("currency", "")),
            raw=payload,
        )


@dataclass
class Trade(_Base):
    """A completed (historical) trade."""

    trade_id: Optional[int] = None
    position_id: Optional[int] = None
    instrument_type: InstrumentType = InstrumentType.UNKNOWN
    asset_id: int = 0
    symbol: str = ""
    direction: Optional[Direction] = None
    invest: float = 0.0
    payout: Optional[float] = None
    pnl: Optional[float] = None
    open_price: Optional[float] = None
    close_price: Optional[float] = None
    open_time: Optional[float] = None
    close_time: Optional[float] = None
    currency: str = ""
    result: str = ""

    @classmethod
    def from_position(cls, position: Position) -> "Trade":
        pnl = position.pnl_realized if position.pnl_realized is not None else position.pnl
        result = "unknown"
        if pnl is not None:
            result = "win" if pnl > 0 else ("loss" if pnl < 0 else "equal")
        return cls(
            trade_id=position.position_id,
            position_id=position.position_id,
            instrument_type=position.instrument_type,
            asset_id=position.asset_id,
            symbol=position.symbol,
            direction=position.direction,
            invest=position.invest,
            pnl=pnl,
            open_price=position.open_price,
            close_price=position.close_price,
            open_time=position.open_time,
            close_time=position.close_time,
            currency=position.currency,
            result=result,
            raw=position.raw,
        )


@dataclass
class TradeResult(_Base):
    """Outcome of a settled option trade."""

    position_id: Optional[int] = None
    order_id: Optional[int] = None
    asset_id: int = 0
    symbol: str = ""
    direction: Optional[Direction] = None
    invest: float = 0.0
    payout: float = 0.0
    pnl: float = 0.0
    result: str = "unknown"          # win / loss / equal
    open_price: Optional[float] = None
    close_price: Optional[float] = None
    open_time: Optional[float] = None
    close_time: Optional[float] = None
    instrument_type: InstrumentType = InstrumentType.UNKNOWN

    @property
    def is_win(self) -> bool:
        return self.result == "win"

    @classmethod
    def from_position(cls, position: Position) -> "TradeResult":
        pnl = position.pnl_realized
        if pnl is None:
            pnl = position.pnl if position.pnl is not None else 0.0
        result = "win" if pnl > 0 else ("loss" if pnl < 0 else "equal")
        return cls(
            position_id=position.position_id,
            order_id=position.order_ids[0] if position.order_ids else None,
            asset_id=position.asset_id,
            symbol=position.symbol,
            direction=position.direction,
            invest=position.invest,
            payout=max(0.0, position.invest + pnl),
            pnl=pnl,
            result=result,
            open_price=position.open_price,
            close_price=position.close_price,
            open_time=position.open_time,
            close_time=position.close_time,
            instrument_type=position.instrument_type,
            raw=position.raw,
        )


# ==========================================================================
# Portfolio / history
# ==========================================================================
@dataclass
class PortfolioStats(_Base):
    total_positions: int = 0
    total_invest: float = 0.0
    expected_profit: float = 0.0
    sell_profit: float = 0.0
    actual_profit: float = 0.0
    pnl: float = 0.0
    by_instrument: Dict[str, Dict[str, float]] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Portfolio(_Base):
    positions: List[Position] = field(default_factory=list)
    stats: PortfolioStats = field(default_factory=PortfolioStats)
    balance_id: Optional[int] = None

    @property
    def open_positions(self) -> List[Position]:
        return [p for p in self.positions if p.is_open]


@dataclass
class History(_Base):
    """A page of historical trades."""

    trades: List[Trade] = field(default_factory=list)
    instrument_type: InstrumentType = InstrumentType.UNKNOWN
    limit: int = 0
    offset: int = 0
    total: Optional[int] = None

    def __iter__(self):
        return iter(self.trades)

    def __len__(self) -> int:
        return len(self.trades)

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl or 0.0 for t in self.trades)

    @property
    def total_invest(self) -> float:
        return sum(t.invest for t in self.trades)

    @property
    def win_rate(self) -> float:
        settled = [t for t in self.trades if t.result in ("win", "loss")]
        if not settled:
            return 0.0
        return 100.0 * sum(1 for t in settled if t.result == "win") / len(settled)
