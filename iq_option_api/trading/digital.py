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
    DIGITAL_PLACE_VERSION,
    DIGITAL_PRICE_EVENTS,
    EVENT_DIGITAL_CLIENT_PRICE,
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
from .option_events import digital_matcher
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

# The current platform emits a second, denser id format keyed by the numeric
# asset id rather than the ticker, e.g.
#   do1861A20260812D052000T1MCSPT          (ATM)
#   do1861A20260812D052000T1MC1F153067     (strike 1.153067)
# Layout: do<asset_id>A<YYYYMMDD>D<HHMMSS>T<period><C|P><SPT | strike>
# where the strike encodes the decimal point as "F" (1F153067 -> 1.153067).
_SYMBOL_RE_V2 = re.compile(
    r"^do(?P<asset_id>\d+)A(?P<date>\d{8})D(?P<time>\d{6})"
    r"T(?P<period>\d+[MH])(?P<dir>[CP])"
    r"(?:SPT|(?P<strike>[0-9]+F[0-9]+|[0-9]+))$"
)


class DigitalOptions:
    """Strike-based options priced by the ``instrument-quotes-generated`` stream."""

    #: Kept for backwards compatibility; the client listens to *both* names in
    #: :data:`~iq_option_api.connection.protocol.DIGITAL_PRICE_EVENTS`.
    EVENT_PRICE = EVENT_DIGITAL_QUOTES
    EVENT_PRICES = DIGITAL_PRICE_EVENTS

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
        self._subs: Dict[Tuple[int, int], Any] = {}   # (asset_id, period) -> subs
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
        """Subscribe to the digital strike book for (asset, period).

        Two different streams carry the book depending on the gateway:

        * ``instrument-quotes-generated``            filtered on
          ``active`` + ``expiration_period`` + ``kind``
        * ``digital-option-client-price-generated``  filtered on ``asset_id``
          (and ``instrument_type``)

        Only listening to the first one is what produced
        ``TimeoutError: event 'instrument-quotes-generated' not received``:
        the account was being served the *second* stream, so no frame ever
        matched.  We now subscribe to both and merge whichever answers.
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

        subs = [
            self.ws.subscribe(EVENT_DIGITAL_QUOTES,
                              params={"active": asset_id,
                                      "expiration_period": period,
                                      "kind": "digital-option"},
                              version="1.0", callback=_handler),
            self.ws.subscribe(EVENT_DIGITAL_CLIENT_PRICE,
                              params={"asset_id": asset_id,
                                      "instrument_type": "digital-option"},
                              version="1.0", callback=_handler),
        ]
        with self._lock:
            self._subs[key] = subs
        return subs

    def unsubscribe_prices(self, asset: "str | int", *, period: int = 60) -> bool:
        asset_id = self.market.asset_id(asset, InstrumentType.DIGITAL)
        with self._lock:
            subs = self._subs.pop((asset_id, int(period)), None)
        if not subs:
            return False
        if not isinstance(subs, list):
            subs = [subs]
        return all(self.ws.unsubscribe(s.subscription_id) for s in subs)

    def _ingest_price_event(self, payload: Any) -> Optional[Dict[str, Any]]:
        """Parse one digital price frame into a strike book.

        Two wire shapes are accepted.

        ``instrument-quotes-generated``::

            {"active": 1, "expiration": {"period": 60, "timestamp": 1700000000},
             "quotes": [{"price": {"ask": 42.1, "bid": 40.0},
                         "symbols": ["doEURUSD202401151230PT1MCSPT", ...]}]}

        ``digital-option-client-price-generated``::

            {"asset_id": 1861, "instrument_index": 835766,
             "digital_option_trading_group_id": "191_0",
             "prices": [{"strike": "1.153067",
                         "call": {"symbol": "do1861A...C1F153067", "bid": 65},
                         "put":  {"symbol": "do1861A...P1F153067", "bid": 33}}]}

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
                     or 0)
        expiration = expiration_obj.get("timestamp", msg.get("expiration_time"))
        strikes: Dict[str, DigitalStrike] = {}

        for entry in (msg.get("quotes") or msg.get("prices") or []):
            if not isinstance(entry, dict):
                continue

            # -- shape B: {"strike": "...", "call": {...}, "put": {...}} -----
            if isinstance(entry.get("call"), dict) or isinstance(entry.get("put"), dict):
                key = self._strike_key(entry.get("strike"))
                strike = strikes.get(key)
                if strike is None:
                    strike = DigitalStrike(
                        value=self._to_float(entry.get("strike"))
                        or self._strike_value(key) or 0.0)
                    strikes[key] = strike
                for name, direction in (("call", Direction.CALL), ("put", Direction.PUT)):
                    side = entry.get(name)
                    if not isinstance(side, dict):
                        continue
                    symbol = side.get("symbol") or side.get("id")
                    if not symbol:
                        continue
                    # ``bid`` here is the option price in the same 0-100 scale
                    # the other stream calls ``ask``.
                    price = self._to_float(side.get("ask", side.get("bid")))
                    profit = ((100.0 - price) * 100.0) / price if price else None
                    self._apply_side(strike, direction, str(symbol), price, profit)
                strike.raw = entry
                if period == 0:
                    period = self._period_of(strike.symbol_call or strike.symbol_put) or period
                continue

            # -- shape A: {"price": {...}, "symbols": [...]} ------------------
            price_obj = entry.get("price") if isinstance(entry.get("price"), dict) else {}
            ask = self._to_float(price_obj.get("ask"))
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
                self._apply_side(strike, direction, symbol, ask, profit)
                strike.raw = entry
                if period == 0:
                    period = self._period_of(symbol) or period

        if not strikes:
            return None

        period = period or 60
        instrument_index = msg.get("instrument_index", f"{int(asset_id)}:{period}")

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
        """Latest strike book for (asset, period), subscribing if necessary.

        The subscriptions opened by :meth:`subscribe_prices` fill ``_books``
        from the reader thread; this method just waits for one of them to land.
        Both digital price streams are polled, so an account served only
        ``digital-option-client-price-generated`` no longer times out waiting
        for ``instrument-quotes-generated``.
        """
        asset_id = self.market.asset_id(asset, InstrumentType.DIGITAL)
        period = int(period)
        with self._lock:
            index = self._by_asset.get((asset_id, period))
            book = self._books.get(index) if index else None
        if book and (time.time() - book["updated_at"]) < max_age:
            return book

        self.subscribe_prices(asset_id, period=period)
        deadline = time.time() + timeout

        while time.time() < deadline:
            # The subscription callback fills ``_books`` on its own; re-check it
            # each pass so a frame delivered between waits is not missed.
            with self._lock:
                index = self._by_asset.get((asset_id, period))
                cached = self._books.get(index) if index else None
            if cached and (time.time() - cached["updated_at"]) < max_age:
                return cached
            time.sleep(0.2)

        # Nothing on the streams - fall back to the request/response strike
        # list, which needs no subscription at all.
        book = self._book_from_strike_list(asset_id, period)
        if book:
            return book

        raise IQTimeoutError(
            f"no digital price data for asset {asset_id} period {period}s within "
            f"{timeout}s (tried {', '.join(DIGITAL_PRICE_EVENTS)} and get-strike-list)")

    def _book_from_strike_list(self, asset_id: int,
                               period: int) -> Optional[Dict[str, Any]]:
        """Build a strike book from ``get-strike-list`` (no stream needed)."""
        duration = max(1, int(round(period / 60.0)))
        try:
            strike_map = self.strike_list(asset_id, duration=duration)
        except Exception as exc:
            self.log.debug("get-strike-list fallback failed: %s", exc)
            return None
        if not strike_map:
            return None

        strikes: Dict[str, DigitalStrike] = {}
        for value, ids in strike_map.items():
            key = self._strike_key(value)
            strike = DigitalStrike(value=self._to_float(value) or 0.0)
            if ids.get("call"):
                self._apply_side(strike, Direction.CALL, str(ids["call"]), None, None)
            if ids.get("put"):
                self._apply_side(strike, Direction.PUT, str(ids["put"]), None, None)
            strikes[key] = strike

        index = f"{int(asset_id)}:{period}"
        book = {
            "instrument_index": index,
            "asset_id": int(asset_id),
            "period": period,
            "expiration": float(self._strike_expiration(duration)),
            "strikes": strikes,
            "updated_at": time.time(),
            "raw": {"source": "get-strike-list"},
        }
        with self._lock:
            self._books[index] = book
            self._by_asset[(int(asset_id), period)] = index
        self.log.info("digital book for asset %s built from get-strike-list", asset_id)
        return book

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

        # ``digital-options.place-digital-option`` is a **v3.0** microservice.
        # The direction, asset and expiry are all encoded in ``instrument_id``
        # (taken from the price stream, never invented); ``instrument_index``
        # and ``asset_id`` are sent when the book gave them to us, which is
        # what the current platform expects.
        body: Dict[str, Any] = {
            "user_balance_id": int(balance_id),
            "instrument_id": str(instrument.instrument_id),
            "amount": self._format_amount(amount),
        }
        if instrument.index is not None:
            body["instrument_index"] = int(instrument.index)
        if instrument.asset_id:
            body["asset_id"] = int(instrument.asset_id)

        # Digital placement answers with a ``digital-option-placed`` broadcast
        # that often omits the envelope request_id - correlate on the symbol.
        matcher = digital_matcher(instrument_id=body["instrument_id"],
                                  balance_id=body["user_balance_id"])
        return self.orders.submit(order, MS_DIGITAL_PLACE, body,
                                  version=DIGITAL_PLACE_VERSION,
                                  timeout=timeout, matcher=matcher)

    @staticmethod
    def _format_amount(amount: float) -> str:
        """Amount as the string the gateway expects (``"1000"``, ``"1.5"``)."""
        value = float(amount)
        return str(int(value)) if value.is_integer() else repr(value)

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
        """Decode a digital symbol.

        Handles both id formats the platform emits:

        * ``doEURUSD202401151230PT1MCSPT``      (ticker + 12-digit stamp)
        * ``do1861A20260812D052000T1MCSPT``     (asset id + A<date>D<time>)
        """
        symbol = symbol or ""
        match = _SYMBOL_RE.match(symbol)
        if match:
            groups = match.groupdict()
            return {
                "symbol": symbol,
                "asset": groups["asset"],
                "expiry": groups["expiry"],
                "period": groups["period"],
                "direction": Direction.CALL if groups["dir"] == "C" else Direction.PUT,
                "strike": groups.get("strike"),
            }

        match = _SYMBOL_RE_V2.match(symbol)
        if match:
            groups = match.groupdict()
            return {
                "symbol": symbol,
                "asset": groups["asset_id"],
                "asset_id": int(groups["asset_id"]),
                "expiry": f"{groups['date']}{groups['time'][:4]}",
                "period": groups["period"],
                "direction": Direction.CALL if groups["dir"] == "C" else Direction.PUT,
                "strike": groups.get("strike"),
            }

        return {"symbol": symbol}

    @staticmethod
    def _strike_value(key: str) -> Optional[float]:
        """Decode the strike embedded in an instrument id.

        Two encodings are in use:

        * ``11350481``  - fixed-point integer scaled by 1e-6 -> ``113.50481``
        * ``1F153067``  - ``F`` stands in for the decimal point -> ``1.153067``

        ``SPT`` marks the at-the-money entry, which has no strike of its own.
        """
        if not key or key == "SPT":
            return None
        text = str(key)
        if "F" in text:
            whole, _, fraction = text.partition("F")
            try:
                return float(f"{whole}.{fraction}")
            except (TypeError, ValueError):
                return None
        try:
            return float(text) * 10e-7
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _apply_side(strike: DigitalStrike, direction: Direction, symbol: str,
                    price: Optional[float], profit: Optional[float]) -> None:
        """Store one CALL/PUT leg of a strike."""
        if direction is Direction.CALL:
            strike.symbol_call = symbol
            strike.instrument_id_call = symbol
            strike.price_call = price
            strike.profit_call = profit
        else:
            strike.symbol_put = symbol
            strike.instrument_id_put = symbol
            strike.price_put = price
            strike.profit_put = profit

    @staticmethod
    def _strike_key(value: Any) -> str:
        """Normalise the book key of a strike (``"SPT"`` for at-the-money)."""
        if value in (None, "", "SPT", "spt"):
            return "SPT"
        return str(value)

    @classmethod
    def _period_of(cls, symbol: Optional[str]) -> Optional[int]:
        """Expiry period in seconds decoded from a symbol (``PT1M`` / ``T1M``)."""
        if not symbol:
            return None
        info = cls.parse_symbol(str(symbol))
        raw = info.get("period")
        if not raw:
            return None
        try:
            value, unit = int(str(raw)[:-1]), str(raw)[-1].upper()
        except (TypeError, ValueError):
            return None
        return value * (3600 if unit == "H" else 60)

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
