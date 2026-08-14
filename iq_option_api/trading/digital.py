"""Digital options - completely separate from binary options.

Captured workflow this module implements::

    digital-option-client-price-generated
            v
    instrument_index
            v
    asset_id
            v
    strike
            v
    CALL / PUT symbol
            v
    instrument_id
            v
    trade

The strike symbols look like ``doEURUSD202401151230PT1MCS10800`` (call) and
``...PS10800`` (put); the ``instrument_id`` used to trade is taken from the
price stream, never guessed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ..account import AccountManager
from ..connection.protocol import (
    MS_DIGITAL_INSTRUMENTS,
    MS_DIGITAL_PLACE,
    MS_DIGITAL_PRICE_EVENT,
    MS_DIGITAL_UNDERLYING,
)
from ..connection.websocket import WebSocketClient
from ..exceptions import (
    AssetError,
    InstrumentError,
    MarketError,
    OrderError,
    TimeoutError as IQTimeoutError,
)
from ..market import MarketManager
from ..models import (
    Asset,
    Direction,
    DigitalStrike,
    Expiration,
    Instrument,
    InstrumentType,
    Order,
    Position,
    TradeResult,
)
from .orders import OrderManager
from .positions import PositionManager

_SYMBOL_RE = re.compile(
    r"^do(?P<asset>[A-Z0-9]+?)(?P<expiry>\d{12})PT(?P<period>\d+[MH])(?P<dir>C|P)SPT?(?P<strike>[\d.]+)?$"
)


class DigitalOptions:
    """Strike-based options priced by ``digital-option-client-price-generated``."""

    EVENT_PRICE = MS_DIGITAL_PRICE_EVENT

    def __init__(self, client: WebSocketClient, market: MarketManager,
                 accounts: AccountManager, orders: OrderManager,
                 positions: PositionManager,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.market = market
        self.accounts = accounts
        self.orders = orders
        self.positions = positions
        self.log = logger or logging.getLogger("iq_option_api.digital")

        # instrument_index -> {asset_id, period, expiration, strikes{...}}
        self._books: Dict[str, Dict[str, Any]] = {}
        self._by_asset: Dict[Tuple[int, int], str] = {}   # (asset_id, period) -> index
        self._subs: Dict[int, Any] = {}
        self._lock = threading.RLock()

    # ==================================================================
    # 1. Assets
    # ==================================================================
    def assets(self, *, only_open: bool = False, refresh: bool = True) -> List[Asset]:
        assets = self.market.assets.digital_assets(refresh=refresh)
        return [a for a in assets if a.is_open] if only_open else assets

    def get_asset(self, name: "str | int") -> Asset:
        return self.market.get_asset(name, InstrumentType.DIGITAL)

    def is_open(self, asset: "str | int") -> bool:
        return self.market.is_open(asset, InstrumentType.DIGITAL)

    # ==================================================================
    # 2. Instrument discovery (underlying -> instruments)
    # ==================================================================
    def instruments(self, asset: "str | int", *,
                    timeout: Optional[float] = None) -> Dict[str, Any]:
        """Raw ``digital-option-instruments.get-instruments`` payload."""
        asset_id = self.market.asset_id(asset, InstrumentType.DIGITAL)
        payload = self.ws.call(MS_DIGITAL_INSTRUMENTS,
                               {"asset_id": asset_id, "instrument_type": "digital-option"},
                               version="3.0", timeout=timeout)
        return payload if isinstance(payload, dict) else {}

    # ==================================================================
    # 3. Price stream -> strike book
    # ==================================================================
    def subscribe_prices(self, asset: "str | int", *, period: int = 60,
                         callback=None):
        """Subscribe to ``digital-option-client-price-generated`` for an asset."""
        asset_id = self.market.asset_id(asset, InstrumentType.DIGITAL)
        with self._lock:
            if asset_id in self._subs:
                return self._subs[asset_id]

        def _handler(payload: Any) -> None:
            book = self._ingest_price_event(payload)
            if book and callback:
                callback(book)

        sub = self.ws.subscribe(self.EVENT_PRICE,
                                params={"asset_id": asset_id, "instrument_type": "digital-option"},
                                version="1.0", callback=_handler)
        with self._lock:
            self._subs[asset_id] = sub
        return sub

    def unsubscribe_prices(self, asset: "str | int") -> bool:
        asset_id = self.market.asset_id(asset, InstrumentType.DIGITAL)
        with self._lock:
            sub = self._subs.pop(asset_id, None)
        return self.ws.unsubscribe(sub.subscription_id) if sub else False

    def _ingest_price_event(self, payload: Any) -> Optional[Dict[str, Any]]:
        """Parse one price event into a strike book keyed by instrument_index."""
        if not isinstance(payload, dict):
            return None
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        instrument_index = msg.get("instrument_index")
        asset_id = msg.get("asset_id", msg.get("active_id"))
        if instrument_index is None or asset_id is None:
            return None

        period = int(msg.get("period", msg.get("expiration_size", 60)) or 60)
        expiration = msg.get("expiration_time", msg.get("expiration"))
        prices = msg.get("prices") or msg.get("quotes") or []

        strikes: Dict[str, DigitalStrike] = {}
        for entry in prices:
            if not isinstance(entry, dict):
                continue
            strike_value = entry.get("strike", entry.get("strike_value"))
            call = entry.get("call") if isinstance(entry.get("call"), dict) else {}
            put = entry.get("put") if isinstance(entry.get("put"), dict) else {}
            key = str(strike_value)
            strikes[key] = DigitalStrike(
                value=self._to_float(strike_value) or 0.0,
                symbol_call=str(call.get("symbol", "")),
                symbol_put=str(put.get("symbol", "")),
                instrument_id_call=str(call.get("instrument_id", call.get("id", ""))),
                instrument_id_put=str(put.get("instrument_id", put.get("id", ""))),
                price_call=self._to_float(call.get("price", call.get("ask"))),
                price_put=self._to_float(put.get("price", put.get("ask"))),
                profit_call=self._to_float(call.get("profit", call.get("profit_percent"))),
                profit_put=self._to_float(put.get("profit", put.get("profit_percent"))),
                raw=entry,
            )

        book = {
            "instrument_index": str(instrument_index),
            "asset_id": int(asset_id),
            "period": period,
            "expiration": self._normalize_ts(expiration),
            "strikes": strikes,
            "updated_at": time.time(),
            "raw": msg,
        }
        with self._lock:
            self._books[str(instrument_index)] = book
            self._by_asset[(int(asset_id), period)] = str(instrument_index)
        return book

    def price_book(self, asset: "str | int", *, period: int = 60,
                   timeout: float = 30.0, max_age: float = 10.0) -> Dict[str, Any]:
        """Latest strike book for (asset, period), subscribing if necessary."""
        asset_id = self.market.asset_id(asset, InstrumentType.DIGITAL)
        with self._lock:
            index = self._by_asset.get((asset_id, period))
            book = self._books.get(index) if index else None
        if book and (time.time() - book["updated_at"]) < max_age:
            return book

        self.subscribe_prices(asset_id, period=period)
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = self.ws.wait_for(
                self.EVENT_PRICE, timeout=max(1.0, deadline - time.time()),
                predicate=lambda p: isinstance(p, dict)
                and int((p.get("msg", p) or {}).get("asset_id",
                        (p.get("msg", p) or {}).get("active_id", 0)) or 0) == asset_id,
            )
            book = self._ingest_price_event(payload)
            if book and (period is None or book["period"] == period):
                return book
        raise IQTimeoutError(
            f"no digital price data for asset {asset_id} period {period}s within {timeout}s")

    # ==================================================================
    # 4-6. Strikes -> instrument_id
    # ==================================================================
    def strikes(self, asset: "str | int", *, period: int = 60,
                timeout: float = 30.0) -> List[DigitalStrike]:
        book = self.price_book(asset, period=period, timeout=timeout)
        return sorted(book["strikes"].values(), key=lambda s: s.value)

    def atm_strike(self, asset: "str | int", *, period: int = 60,
                   timeout: float = 30.0) -> DigitalStrike:
        """At-the-money strike (the ``SPT`` entry, or the one closest to spot)."""
        book = self.price_book(asset, period=period, timeout=timeout)
        strikes = book["strikes"]
        for key in ("SPT", "spt", "None", "null"):
            if key in strikes:
                return strikes[key]
        try:
            spot = self.market.current_price(book["asset_id"], InstrumentType.DIGITAL)
        except Exception:
            spot = None
        values = [s for s in strikes.values() if s.value]
        if not values:
            raise InstrumentError(f"no strikes available for asset {book['asset_id']}")
        if spot is None:
            return values[len(values) // 2]
        return min(values, key=lambda s: abs(s.value - spot))

    def find_strike(self, asset: "str | int", strike_value: float, *,
                    period: int = 60, tolerance: float = 1e-6) -> DigitalStrike:
        for strike in self.strikes(asset, period=period):
            if abs(strike.value - float(strike_value)) <= tolerance:
                return strike
        raise InstrumentError(f"strike {strike_value} not available for {asset}")

    def get_instrument(self, asset: "str | int", direction: "Direction | str",
                       *, period: int = 60, strike: Optional[float] = None,
                       timeout: float = 30.0) -> Instrument:
        """Resolve the tradable ``instrument_id`` for a digital option."""
        direction = Direction.parse(direction)
        if direction not in (Direction.CALL, Direction.PUT):
            raise OrderError("digital options accept only CALL or PUT")

        book = self.price_book(asset, period=period, timeout=timeout)
        strike_obj = (self.find_strike(asset, strike, period=period)
                      if strike is not None
                      else self.atm_strike(asset, period=period, timeout=timeout))

        instrument_id = strike_obj.instrument_id(direction)
        if not instrument_id:
            instrument_id = strike_obj.symbol(direction)
        if not instrument_id:
            raise InstrumentError(
                f"no instrument_id for {asset} {direction.value} strike={strike_obj.value}")

        expiration = None
        if book.get("expiration"):
            expiration = Expiration(timestamp=float(book["expiration"]), period=period)

        return Instrument(
            instrument_id=str(instrument_id),
            asset_id=book["asset_id"],
            symbol=strike_obj.symbol(direction),
            instrument_type=InstrumentType.DIGITAL,
            expiration=expiration,
            strike=strike_obj.value or None,
            direction=direction,
            price=strike_obj.price_call if direction.is_long else strike_obj.price_put,
            payout=strike_obj.profit(direction),
            index=int(book["instrument_index"]) if str(book["instrument_index"]).isdigit() else None,
            raw=book.get("raw", {}),
        )

    def payout(self, asset: "str | int", direction: "Direction | str" = Direction.CALL,
               *, period: int = 60) -> Optional[float]:
        direction = Direction.parse(direction)
        return self.atm_strike(asset, period=period).profit(direction)

    # ==================================================================
    # 7. Trade
    # ==================================================================
    def buy(self, asset: "str | int", amount: float, direction: "Direction | str",
            *, duration: int = 1, strike: Optional[float] = None,
            check_market: bool = True, timeout: Optional[float] = None) -> Order:
        """Place a digital option.  ``duration`` in **minutes**."""
        direction = Direction.parse(direction)
        period = int(duration) * 60
        if check_market:
            self.market.ensure_open(asset, InstrumentType.DIGITAL)

        instrument = self.get_instrument(asset, direction, period=period, strike=strike)
        balance_id = self.accounts.user_balance_id
        order = self.orders.create(instrument=instrument, direction=direction,
                                   amount=amount, balance_id=balance_id)
        self.orders.validate(order, balance=self._balance())

        body = {
            "user_balance_id": balance_id,
            "instrument_id": instrument.instrument_id,
            "amount": str(float(amount)),
            "asset_id": instrument.asset_id,
            "direction": direction.value,
        }
        return self.orders.submit(order, MS_DIGITAL_PLACE, body, version="1.0", timeout=timeout)

    def call(self, asset: "str | int", amount: float, *, duration: int = 1,
             **kwargs: Any) -> Order:
        return self.buy(asset, amount, Direction.CALL, duration=duration, **kwargs)

    def put(self, asset: "str | int", amount: float, *, duration: int = 1,
            **kwargs: Any) -> Order:
        return self.buy(asset, amount, Direction.PUT, duration=duration, **kwargs)

    # ==================================================================
    # 8-9. Positions / settlement
    # ==================================================================
    def open_positions(self) -> List[Position]:
        return self.positions.open_positions(instrument_type=InstrumentType.DIGITAL)

    def close(self, position_id: int) -> bool:
        return self.positions.close(position_id)

    def check_result(self, order: "Order | int", *, timeout: float = 300.0) -> TradeResult:
        order_id = order.order_id if isinstance(order, Order) else int(order)
        if order_id is None:
            raise OrderError("order has no id")
        position = self.positions.by_order_id(order_id) or self.positions.get(order_id)
        return self.positions.wait_for_close(
            position.position_id if position else order_id, timeout=timeout)

    def buy_and_wait(self, asset: "str | int", amount: float,
                     direction: "Direction | str", *, duration: int = 1,
                     timeout: float = 300.0) -> TradeResult:
        order = self.buy(asset, amount, direction, duration=duration)
        return self.check_result(order, timeout=max(timeout, duration * 60 + 30))

    def history(self, limit: int = 50) -> List[Order]:
        return [o for o in self.orders.history(limit)
                if o.instrument_type is InstrumentType.DIGITAL]

    # ==================================================================
    @staticmethod
    def parse_symbol(symbol: str) -> Dict[str, Any]:
        """Decode a digital symbol such as ``doEURUSD202401151230PT1MCSPT``."""
        match = _SYMBOL_RE.match(symbol or "")
        if not match:
            return {"symbol": symbol}
        groups = match.groupdict()
        return {
            "symbol": symbol,
            "asset": groups["asset"],
            "expiry": groups["expiry"],
            "period": groups["period"],
            "direction": Direction.CALL if groups["dir"] == "C" else Direction.PUT,
            "strike": groups.get("strike"),
        }

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_ts(value: Any) -> Optional[float]:
        try:
            ts = float(value)
        except (TypeError, ValueError):
            return None
        while ts > 1e11:
            ts /= 1000.0
        return ts

    def _balance(self) -> Optional[float]:
        try:
            return self.accounts.balance(refresh=False)
        except Exception:
            return None
