"""Digital options - completely separate from binary options.

Captured workflow this module implements::

    subscribe instrument-quotes-generated   (active + expiration_period + kind)
            v
    quote frame  ->  asset_id / expiration
            v
    strike
            v
    CALL / PUT symbol
            v
    instrument_id
            v
    trade

The old ``digital-option-client-price-generated`` event is never pushed unless
the client subscribes first, and the subscription is per (asset, expiry
period): asking for the book without it simply timed out.  Strike symbols look
like ``doEURUSD202401151230PT1MCSPT`` (at the money) or
``doUSDJPY-OTC201811111204PT1MC11350481`` (fixed strike, value scaled by 1e-6);
the ``instrument_id`` used to trade is the symbol itself, taken from the quote
stream and never guessed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ..account import AccountManager
from ..connection.protocol import (
    EVENT_DIGITAL_QUOTES,
    MS_DIGITAL_INSTRUMENTS,
    MS_DIGITAL_PLACE,
    MS_DIGITAL_STRIKE_LIST,
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

# Digital instrument ids come in two shapes:
#   ATM     doEURUSD202401151230PT1MCSPT          (trailing "SPT")
#   strike  doUSDJPY-OTC201811111204PT1MC11350481 (bare numeric strike)
# The asset may carry a suffix such as "-OTC", so it is matched non-greedily up
# to the 12-digit expiry stamp.
_SYMBOL_RE = re.compile(
    r"^do(?P<asset>[A-Z0-9-]+?)(?P<expiry>\d{12})"
    r"PT(?P<period>\d+[MH])(?P<dir>[CP])"
    r"(?:SPT|(?P<strike>\d+(?:\.\d+)?))$"
)


class DigitalOptions:
    """Strike-based options priced by the ``instrument-quotes-generated`` stream."""

    EVENT_PRICE = EVENT_DIGITAL_QUOTES

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
        """Subscribe to ``instrument-quotes-generated`` for (asset, period).

        The quote stream is filtered on ``active`` + ``expiration_period`` +
        ``kind``; those three routing filters are mandatory.  Nothing is
        delivered when they are wrong (this is what made ``price_book`` time
        out), and a subscription for the 60s book says nothing about the 300s
        book - hence one subscription per (asset, period) pair.
        """
        asset_id = self.market.asset_id(asset, InstrumentType.DIGITAL)
        period = int(period)
        key = (asset_id, period)
        with self._lock:
            if key in self._subs:
                return self._subs[key]

        def _handler(payload: Any) -> None:
            book = self._ingest_price_event(payload)
            if book and callback:
                callback(book)

        sub = self.ws.subscribe(self.EVENT_PRICE,
                                params={"active": asset_id,
                                        "expiration_period": period,
                                        "kind": "digital-option"},
                                version="1.0", callback=_handler)
        with self._lock:
            self._subs[key] = sub
        return sub

    def unsubscribe_prices(self, asset: "str | int", *, period: int = 60) -> bool:
        asset_id = self.market.asset_id(asset, InstrumentType.DIGITAL)
        with self._lock:
            sub = self._subs.pop((asset_id, int(period)), None)
        return self.ws.unsubscribe(sub.subscription_id) if sub else False

    def _ingest_price_event(self, payload: Any) -> Optional[Dict[str, Any]]:
        """Parse one ``instrument-quotes-generated`` frame into a strike book.

        Wire shape::

            {"active": 1, "expiration": {"period": 60, "timestamp": 1700000000},
             "quotes": [{"price": {"ask": 42.1, "bid": 40.0},
                         "symbols": ["doEURUSD202401151230PT1MCSPT", ...]}]}

        Each *symbol* is itself the tradable ``instrument_id`` - digital
        placement never invents one.  Profit follows the platform's own
        formula ``((100 - ask) * 100) / ask``.
        """
        if not isinstance(payload, dict):
            return None
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        asset_id = msg.get("active", msg.get("asset_id", msg.get("active_id")))
        if asset_id is None:
            return None

        expiration_obj = msg.get("expiration") if isinstance(msg.get("expiration"), dict) else {}
        period = int(expiration_obj.get("period")
                     or msg.get("period")
                     or msg.get("expiration_size")
                     or 60)
        expiration = expiration_obj.get("timestamp", msg.get("expiration_time"))
        instrument_index = msg.get("instrument_index", f"{int(asset_id)}:{period}")

        strikes: Dict[str, DigitalStrike] = {}
        for entry in (msg.get("quotes") or msg.get("prices") or []):
            if not isinstance(entry, dict):
                continue
            price = entry.get("price") if isinstance(entry.get("price"), dict) else {}
            ask = self._to_float(price.get("ask"))
            profit = ((100.0 - ask) * 100.0) / ask if ask else None

            symbols = entry.get("symbols")
            if not symbols:
                symbols = [s for s in (entry.get("symbol"),) if s]
            for symbol in symbols:
                symbol = str(symbol)
                info = self.parse_symbol(symbol)
                direction = info.get("direction")
                if direction is None:
                    continue
                key = info.get("strike") or "SPT"
                strike = strikes.get(key)
                if strike is None:
                    strike = DigitalStrike(value=self._strike_value(key) or 0.0)
                    strikes[key] = strike
                if direction is Direction.CALL:
                    strike.symbol_call = symbol
                    strike.instrument_id_call = symbol
                    strike.price_call = ask
                    strike.profit_call = profit
                else:
                    strike.symbol_put = symbol
                    strike.instrument_id_put = symbol
                    strike.price_put = ask
                    strike.profit_put = profit
                strike.raw = entry

        if not strikes:
            return None

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

        def _matches(p: Any) -> bool:
            if not isinstance(p, dict):
                return False
            msg = p.get("msg") if isinstance(p.get("msg"), dict) else p
            active = msg.get("active", msg.get("asset_id", msg.get("active_id")))
            try:
                return int(active or 0) == asset_id
            except (TypeError, ValueError):
                return False

        while time.time() < deadline:
            # The subscription callback fills ``_books`` on its own; re-check it
            # each pass so a frame delivered between waits is not missed.
            with self._lock:
                index = self._by_asset.get((asset_id, period))
                cached = self._books.get(index) if index else None
            if cached and (time.time() - cached["updated_at"]) < max_age:
                return cached
            payload = self.ws.wait_for(
                self.EVENT_PRICE, timeout=max(1.0, deadline - time.time()),
                predicate=_matches,
            )
            book = self._ingest_price_event(payload)
            if book and (period is None or book["period"] == period):
                return book
        raise IQTimeoutError(
            f"no digital price data for asset {asset_id} period {period}s within {timeout}s")

    def strike_list(self, asset: "str | int", *, duration: int = 1,
                    timeout: Optional[float] = None) -> Dict[str, Dict[str, str]]:
        """Strikes straight from ``get-strike-list`` (no stream required).

        Returns ``{"1.234560": {"call": <instrument_id>, "put": <instrument_id>}}``.
        Useful as a cross-check on the quote stream, and as a source of
        instrument ids for strikes that are not currently quoted.
        """
        name = self.market.assets.resolve_name(asset)
        duration = max(1, int(duration))
        payload = self.ws.call(
            MS_DIGITAL_STRIKE_LIST,
            {"type": "digital-option", "underlying": name,
             "expiration": int(self._strike_expiration(duration)) * 1000,
             "period": duration * 60},
            version="4.0", timeout=timeout)

        msg = payload.get("msg") if isinstance(payload, dict) and isinstance(
            payload.get("msg"), dict) else payload
        result: Dict[str, Dict[str, str]] = {}
        for entry in (msg or {}).get("strike", []) or []:
            try:
                key = "%.6f" % (float(entry["value"]) * 10e-7)
                result[key] = {"call": str(entry["call"]["id"]),
                               "put": str(entry["put"]["id"])}
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def _strike_expiration(self, duration: int) -> float:
        """Next expiry aligned to a ``duration``-minute boundary (server time)."""
        now = int(self.market.server_time)
        value = time.gmtime(now)
        aligned = now - now % 60
        aligned += (duration - value.tm_min % duration) * 60
        if now > aligned - 10:          # too close to be accepted
            aligned += duration * 60
        return aligned

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

        # ``place-digital-option`` takes exactly these three fields; the
        # direction and asset are already encoded in ``instrument_id``, and
        # sending them as extras gets the frame rejected.
        body = {
            "user_balance_id": int(balance_id),
            "instrument_id": str(instrument.instrument_id),
            "amount": str(float(amount)),
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
    def _strike_value(key: str) -> Optional[float]:
        """Decode the strike embedded in an instrument id.

        Strikes ride in the symbol as fixed-point integers scaled by 1e-6
        (``11350481`` -> ``113.50481``); ``SPT`` marks the at-the-money entry,
        which has no strike of its own.
        """
        if not key or key == "SPT":
            return None
        try:
            return float(key) * 10e-7
        except (TypeError, ValueError):
            return None

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
