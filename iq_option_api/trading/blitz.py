"""Blitz options - ultra short expiry options (5s - 60s).

Blitz is a separate product from binary and digital options: expiration is a
*duration* in seconds counted from the moment the order is accepted, not a
wall-clock expiry timestamp.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..account import AccountManager
from ..connection.protocol import (
    BLITZ_OPEN_VERSION,
    MS_BLITZ_OPEN,
    OPTION_TYPE_BLITZ,
)
from ..connection.websocket import WebSocketClient
from ..exceptions import AssetError, OrderError
from ..market import MarketManager
from ..market.instruments import BLITZ_DURATIONS
from ..models import (
    Asset,
    BlitzOption,
    Expiration,
    Direction,
    Instrument,
    InstrumentType,
    Order,
    Position,
    TradeResult,
)
from .option_events import option_matcher
from .orders import OrderManager
from .positions import PositionManager


class BlitzOptions:
    """Blitz option discovery, trading and result tracking."""

    DURATIONS = BLITZ_DURATIONS

    def __init__(self, client: WebSocketClient, market: MarketManager,
                 accounts: AccountManager, orders: OrderManager,
                 positions: PositionManager,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.market = market
        self.accounts = accounts
        self.orders = orders
        self.positions = positions
        self.log = logger or logging.getLogger("iq_option_api.blitz")

    # ==================================================================
    # Assets
    # ==================================================================
    def assets(self, *, only_open: bool = False, refresh: bool = False) -> List[Asset]:
        return self.market.list_assets(InstrumentType.BLITZ,
                                       only_open=only_open, refresh=refresh)

    def open_assets(self) -> List[Asset]:
        return self.assets(only_open=True, refresh=True)

    def get_asset(self, name: "str | int") -> Asset:
        return self.market.get_asset(name, InstrumentType.BLITZ)

    def is_open(self, asset: "str | int") -> bool:
        return self.market.is_open(asset, InstrumentType.BLITZ)

    def durations(self, asset: Optional["str | int"] = None) -> List[int]:
        """Available blitz durations (seconds)."""
        if asset is None:
            return list(self.DURATIONS)
        try:
            option = self.get_option(asset)
            return option.durations or list(self.DURATIONS)
        except Exception:
            return list(self.DURATIONS)

    def get_option(self, asset: "str | int", duration: int = 60) -> BlitzOption:
        asset_obj = self.get_asset(asset)
        raw = asset_obj.raw or {}
        # The initialization data nests the offered expirations under
        # ``option.expiration_times`` (a dict keyed by the duration in
        # seconds); older payloads put a plain list at the top level.
        durations = self._parse_durations(raw)
        return BlitzOption(
            asset_id=asset_obj.asset_id,
            name=asset_obj.name,
            duration=int(duration),
            profit_percent=self.payout(asset_obj),
            is_open=asset_obj.is_open,
            durations=durations or list(self.DURATIONS),
            raw=raw,
        )

    @staticmethod
    def _parse_durations(raw: Dict[str, Any]) -> List[int]:
        """Blitz expirations offered by the server, in seconds."""
        sources: List[Any] = []
        option = raw.get("option")
        if isinstance(option, dict):
            sources.append(option.get("expiration_times"))
            sources.append(option.get("expiration"))
        for key in ("expiration_times", "durations", "times"):
            sources.append(raw.get(key))

        for value in sources:
            if isinstance(value, dict):
                # {"5": {...}, "10": {...}} - the keys are the durations
                parsed = []
                for key in value:
                    try:
                        parsed.append(int(key))
                    except (TypeError, ValueError):
                        continue
                if parsed:
                    return sorted(set(parsed))
            elif isinstance(value, (list, tuple)):
                parsed = []
                for item in value:
                    if isinstance(item, dict):
                        item = item.get("time", item.get("value"))
                    try:
                        parsed.append(int(item))
                    except (TypeError, ValueError):
                        continue
                if parsed:
                    return sorted(set(parsed))
        return []

    def get_instrument(self, asset: "str | int", duration: int = 60) -> Instrument:
        option = self.get_option(asset, duration)
        asset_obj = self.get_asset(asset)
        expiration = Expiration(timestamp=self.market.server_time + duration,
                                period=int(duration))
        return Instrument(
            instrument_id=f"{option.asset_id}:blitz:{duration}",
            asset_id=option.asset_id,
            symbol=option.name,
            instrument_type=InstrumentType.BLITZ,
            expiration=expiration,
            payout=option.profit_percent,
            is_tradable=option.is_open,
            min_amount=asset_obj.minimal_amount,
            max_amount=asset_obj.maximal_amount,
            raw=option.raw,
        )

    # ==================================================================
    # Payout
    # ==================================================================
    def payout(self, asset: "str | int | Asset") -> Optional[float]:
        """Profit percentage of a blitz asset (e.g. 80.0 means +80%)."""
        asset_obj = asset if isinstance(asset, Asset) else self.get_asset(asset)
        if asset_obj.profit_percent is not None:
            return asset_obj.profit_percent
        try:
            data = self.market.assets.initialization_data()
        except Exception:
            return None
        section = data.get("blitz", {}) if isinstance(data, dict) else {}
        actives = section.get("actives", {}) if isinstance(section, dict) else {}
        item = actives.get(str(asset_obj.asset_id)) or actives.get(asset_obj.asset_id)
        if not isinstance(item, dict):
            return None
        option = item.get("option", {})
        commission = (option.get("profit", {}) or {}).get("commission")
        return None if commission is None else 100.0 - float(commission)

    def expected_profit(self, asset: "str | int", amount: float) -> Optional[float]:
        payout = self.payout(asset)
        return None if payout is None else float(amount) * payout / 100.0

    # ==================================================================
    # Position subscription (blitz positions arrive on the same stream)
    # ==================================================================
    def subscribe_positions(self, callback: Optional[Callable[[Position], None]] = None):
        return self.positions.subscribe(
            user_id=self.accounts.user_id,
            balance_id=self.accounts.user_balance_id,
            instrument_types=[InstrumentType.BLITZ],
            callback=callback,
        )

    # ==================================================================
    # Trading
    # ==================================================================
    def buy(self, asset: "str | int", amount: float, direction: "Direction | str",
            duration: int = 60, *, check_market: bool = True,
            timeout: Optional[float] = None) -> Order:
        direction = Direction.parse(direction)
        if direction not in (Direction.CALL, Direction.PUT):
            direction = Direction.CALL if direction.is_long else Direction.PUT

        duration = int(duration)
        available = self.durations(asset)
        if duration not in available:
            raise OrderError(
                f"invalid blitz duration {duration}s",
                details={"available": available})

        if check_market and not self.is_open(asset):
            raise AssetError(f"blitz market closed for {asset}")

        instrument = self.get_instrument(asset, duration)
        balance_id = self.accounts.user_balance_id

        order = self.orders.create(
            instrument=instrument, direction=direction, amount=amount,
            balance_id=balance_id,
        )
        self.orders.validate(order, balance=self._balance())

        # ``blitz-options.open-option`` does not exist on the gateway: the
        # frame is accepted and then silently dropped, which is the
        # "no response for request_id=N within 25.0s" failure.  Blitz is
        # placed on the *binary* channel (v2.0) with ``option_type_id=12``,
        # and unlike binary/turbo it needs BOTH:
        #   * ``expired``          - absolute server timestamp of the expiry
        #   * ``expiration_size``  - the duration in seconds
        # plus a non-zero ``value`` (the quote the order is priced against);
        # sending ``value: 0`` gets the order rejected.
        expired = int(self.market.server_time) + duration
        body: Dict[str, Any] = {
            "user_balance_id": int(balance_id),
            "active_id": int(instrument.asset_id),
            "option_type_id": OPTION_TYPE_BLITZ,
            "direction": direction.value.lower(),
            "expired": expired,
            "expiration_size": duration,
            "price": float(amount),
            "value": self._quote_value(instrument.asset_id),
            "refund_value": 0,
            "profit_percent": int(instrument.payout or 0),
        }
        # Blitz shares the binary options lifecycle events, so the same
        # broadcast fallback applies (see trading/option_events.py).
        matcher = option_matcher(active_id=body["active_id"],
                                 direction=body["direction"],
                                 balance_id=body["user_balance_id"])
        return self.orders.submit(order, MS_BLITZ_OPEN, body,
                                  version=BLITZ_OPEN_VERSION,
                                  timeout=timeout, matcher=matcher)

    def _quote_value(self, asset_id: int) -> int:
        """``value`` field of an open-option body.

        The platform sends the fractional part of the current quote as an
        integer (``1.153067`` -> ``153067``).  It is used to price the option
        at submission time; a hardcoded ``0`` is refused by the gateway.
        Falls back to ``0`` when no quote is reachable so a missing tick never
        blocks the order path outright.
        """
        try:
            price = self.market.current_price(int(asset_id), InstrumentType.BLITZ)
        except Exception as exc:
            self.log.debug("no quote for asset %s (%s), sending value=0", asset_id, exc)
            return 0
        try:
            fraction = str(float(price)).split(".")
            return int(fraction[1]) if len(fraction) > 1 else 0
        except (TypeError, ValueError, IndexError):
            return 0

    def call(self, asset: "str | int", amount: float, duration: int = 60, **kw: Any) -> Order:
        return self.buy(asset, amount, Direction.CALL, duration, **kw)

    def put(self, asset: "str | int", amount: float, duration: int = 60, **kw: Any) -> Order:
        return self.buy(asset, amount, Direction.PUT, duration, **kw)

    # ==================================================================
    # Results
    # ==================================================================
    def position_of(self, order: "Order | int") -> Optional[Position]:
        order_id = order.order_id if isinstance(order, Order) else int(order)
        return self.positions.by_order_id(order_id) if order_id else None

    def check_result(self, order: "Order | int", timeout: float = 120.0) -> TradeResult:
        order_id = order.order_id if isinstance(order, Order) else int(order)
        position = self.position_of(order_id)
        if position is None:
            # ``portfolio.get-positions`` v4.0 is scoped by balance: without
            # ``user_balance_id`` the gateway answers for the wrong account
            # (or not at all), so a blitz position placed on PRACTICE would
            # never be found.
            self.positions.refresh(instrument_types=[InstrumentType.BLITZ],
                                   user_balance_id=self.accounts.active_balance_id,
                                   limit=30)
            position = self.positions.by_order_id(order_id)
        if position is None:
            raise OrderError(f"no blitz position for order {order_id}")
        return self.positions.wait_for_close(position.position_id, timeout=timeout,
                                             poll_interval=2.0)

    def buy_and_wait(self, asset: "str | int", amount: float,
                     direction: "Direction | str", duration: int = 60,
                     **kwargs: Any) -> TradeResult:
        order = self.buy(asset, amount, direction, duration, **kwargs)
        return self.check_result(order, timeout=duration + 60.0)

    def open_positions(self) -> List[Position]:
        return self.positions.open_positions(instrument_type=InstrumentType.BLITZ)

    def history(self, limit: int = 50) -> List[Order]:
        return [o for o in self.orders.history(limit)
                if o.instrument_type is InstrumentType.BLITZ]

    # ==================================================================
    def _balance(self) -> Optional[float]:
        try:
            return self.accounts.balance(refresh=False)
        except Exception:
            return None
