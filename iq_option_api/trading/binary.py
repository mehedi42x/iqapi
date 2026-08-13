"""Binary (and turbo) options.

Workflow implemented here::

    Binary Asset -> Check Market -> Get Instrument -> Get Expiration
        -> Get Payout -> Validate Amount -> Place CALL/PUT -> Order ID
        -> Track Position -> Expiration -> Result
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..account import AccountManager
from ..connection.protocol import MS_BINARY_OPEN
from ..connection.websocket import WebSocketClient
from ..exceptions import AssetError, InstrumentError, MarketError, OrderError
from ..market import MarketManager
from ..market.instruments import BINARY_PERIODS, next_expiration
from ..models import (
    Asset,
    Direction,
    Expiration,
    Instrument,
    InstrumentType,
    Order,
    Position,
    TradeResult,
)
from .orders import OrderManager
from .positions import PositionManager


class BinaryOptions:
    """CALL/PUT options with a fixed expiration."""

    def __init__(self, client: WebSocketClient, market: MarketManager,
                 accounts: AccountManager, orders: OrderManager,
                 positions: PositionManager,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.market = market
        self.accounts = accounts
        self.orders = orders
        self.positions = positions
        self.log = logger or logging.getLogger("iq_option_api.binary")

    # ==================================================================
    # 1. Assets
    # ==================================================================
    def assets(self, *, turbo: bool = False, only_open: bool = False,
               refresh: bool = False) -> List[Asset]:
        itype = InstrumentType.TURBO if turbo else InstrumentType.BINARY
        return self.market.list_assets(itype, only_open=only_open, refresh=refresh)

    def open_assets(self, *, turbo: bool = False) -> List[Asset]:
        return self.assets(turbo=turbo, only_open=True, refresh=True)

    def get_asset(self, name: "str | int", *, turbo: bool = False) -> Asset:
        itype = InstrumentType.TURBO if turbo else InstrumentType.BINARY
        return self.market.get_asset(name, itype)

    # ==================================================================
    # 2. Market check
    # ==================================================================
    def is_open(self, asset: "str | int", *, turbo: bool = False) -> bool:
        itype = InstrumentType.TURBO if turbo else InstrumentType.BINARY
        return self.market.is_open(asset, itype)

    # ==================================================================
    # 3-4. Instrument + expiration
    # ==================================================================
    def expirations(self, asset: "str | int", *, period: int = 60,
                    count: int = 5) -> List[Expiration]:
        return self.market.instruments.option_expirations(period, count)

    def next_expiration(self, period: int = 60) -> Expiration:
        return self.market.instruments.next_expiration(period)

    def get_instrument(self, asset: "str | int", direction: "Direction | str",
                       *, expiration_period: int = 60, turbo: bool = False,
                       expiration_timestamp: Optional[float] = None) -> Instrument:
        direction = Direction.parse(direction)
        if direction not in (Direction.CALL, Direction.PUT):
            raise OrderError("binary options accept only CALL or PUT")
        itype = InstrumentType.TURBO if turbo else InstrumentType.BINARY
        asset_obj = self.get_asset(asset, turbo=turbo)

        if expiration_timestamp is None:
            expiration_timestamp = next_expiration(expiration_period)
        expiration = Expiration(timestamp=float(expiration_timestamp),
                                period=expiration_period)

        return Instrument(
            instrument_id=f"{asset_obj.asset_id}:{int(expiration.timestamp)}:{direction.value}",
            asset_id=asset_obj.asset_id,
            symbol=asset_obj.name,
            instrument_type=itype,
            expiration=expiration,
            direction=direction,
            payout=self.payout(asset_obj, turbo=turbo),
            min_amount=asset_obj.minimal_amount,
            max_amount=asset_obj.maximal_amount,
        )

    # ==================================================================
    # 5. Payout
    # ==================================================================
    def payout(self, asset: "str | int | Asset", *, turbo: bool = False) -> Optional[float]:
        """Profit percentage (e.g. 87.0 means +87%)."""
        asset_obj = asset if isinstance(asset, Asset) else self.get_asset(asset, turbo=turbo)
        if asset_obj.profit_percent is not None:
            return asset_obj.profit_percent
        try:
            data = self.market.assets.initialization_data()
        except Exception:
            return None
        section = data.get("turbo" if turbo else "binary", {})
        actives = section.get("actives", {}) if isinstance(section, dict) else {}
        item = actives.get(str(asset_obj.asset_id)) or actives.get(asset_obj.asset_id)
        if not isinstance(item, dict):
            return None
        option = item.get("option", {})
        profit = option.get("profit", {}) if isinstance(option, dict) else {}
        commission = profit.get("commission")
        return (100.0 - float(commission)) if commission is not None else None

    def expected_profit(self, asset: "str | int", amount: float,
                        *, turbo: bool = False) -> Optional[float]:
        payout = self.payout(asset, turbo=turbo)
        return None if payout is None else amount * payout / 100.0

    # ==================================================================
    # 6-8. Place order
    # ==================================================================
    def buy(self, asset: "str | int", amount: float, direction: "Direction | str",
            *, duration: int = 1, turbo: bool = False,
            expiration_timestamp: Optional[float] = None,
            check_market: bool = True,
            timeout: Optional[float] = None) -> Order:
        """Place a CALL/PUT.  ``duration`` is in **minutes**."""
        direction = Direction.parse(direction)
        period = int(duration) * 60
        if turbo and period > 300:
            self.log.debug("turbo options are short term, %ss may be rejected", period)

        itype = InstrumentType.TURBO if turbo else InstrumentType.BINARY
        if check_market:
            self.market.ensure_open(asset, itype)

        instrument = self.get_instrument(asset, direction, expiration_period=period,
                                         turbo=turbo,
                                         expiration_timestamp=expiration_timestamp)
        balance_id = self.accounts.user_balance_id
        order = self.orders.create(instrument=instrument, direction=direction,
                                   amount=amount, balance_id=balance_id)
        self.orders.validate(order, balance=self._balance())

        body = {
            "user_balance_id": balance_id,
            "active_id": instrument.asset_id,
            "option_type_id": 3 if turbo else 1,   # 1 binary, 3 turbo
            "direction": direction.value,
            "expired": int(instrument.expiration.timestamp),
            "refund_value": 0,
            "price": float(amount),
            "value": 0,
            "profit_percent": int(instrument.payout or 0),
        }
        return self.orders.submit(order, MS_BINARY_OPEN, body, version="2.0", timeout=timeout)

    def call(self, asset: "str | int", amount: float, *, duration: int = 1,
             **kwargs: Any) -> Order:
        return self.buy(asset, amount, Direction.CALL, duration=duration, **kwargs)

    def put(self, asset: "str | int", amount: float, *, duration: int = 1,
            **kwargs: Any) -> Order:
        return self.buy(asset, amount, Direction.PUT, duration=duration, **kwargs)

    # ==================================================================
    # 9-11. Track / result
    # ==================================================================
    def position_of(self, order: "Order | int") -> Optional[Position]:
        order_id = order.order_id if isinstance(order, Order) else int(order)
        if order_id is None:
            return None
        return self.positions.by_order_id(order_id) or self.positions.get(order_id)

    def check_result(self, order: "Order | int", *, timeout: float = 300.0) -> TradeResult:
        """Wait for the option to expire and return win/loss/equal."""
        order_id = order.order_id if isinstance(order, Order) else int(order)
        if order_id is None:
            raise OrderError("order has no id, cannot track it")
        position = self.position_of(order_id)
        position_id = position.position_id if position else order_id
        return self.positions.wait_for_close(position_id, timeout=timeout)

    def buy_and_wait(self, asset: "str | int", amount: float,
                     direction: "Direction | str", *, duration: int = 1,
                     turbo: bool = False, timeout: float = 300.0) -> TradeResult:
        order = self.buy(asset, amount, direction, duration=duration, turbo=turbo)
        extra = duration * 60 + 30
        return self.check_result(order, timeout=max(timeout, extra))

    def open_positions(self) -> List[Position]:
        binary = self.positions.open_positions(instrument_type=InstrumentType.BINARY)
        turbo = self.positions.open_positions(instrument_type=InstrumentType.TURBO)
        return binary + turbo

    def history(self, limit: int = 50) -> List[Any]:
        return [o for o in self.orders.history(limit)
                if o.instrument_type in (InstrumentType.BINARY, InstrumentType.TURBO)]

    # ==================================================================
    def _balance(self) -> Optional[float]:
        try:
            return self.accounts.balance(refresh=False)
        except Exception:
            return None
