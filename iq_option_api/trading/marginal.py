"""Shared engine for every *marginal* (leveraged) instrument.

Forex, CFD, Stocks, Crypto, Commodities, ETFs and Indices all trade through
the same ``marginal-instruments.place-order`` microservice - only the
``instrument_type`` and the asset group differ.  That logic lives here once;
the per-asset-class modules are thin, well typed facades over it.

Workflow::

    Market -> Asset -> Instrument -> Leverage/Margin -> Order Validation
        -> BUY/SELL -> Position -> SL/TP -> Monitor -> Modify/Close -> P/L
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..account import AccountManager
from ..connection.protocol import MS_MARGINAL_PLACE
from ..connection.websocket import WebSocketClient
from ..exceptions import InstrumentError, MarketError, OrderError, PositionError
from ..market import MarketManager
from ..models import (
    Asset,
    Direction,
    Instrument,
    InstrumentType,
    Order,
    OrderType,
    Position,
    Price,
)
from .orders import OrderManager
from .positions import PositionManager


class MarginalTrading:
    """Generic leveraged trading for one :class:`InstrumentType`."""

    #: overridden by subclasses
    INSTRUMENT_TYPE: InstrumentType = InstrumentType.CFD
    #: optional asset-group filter applied to the underlying list
    ASSET_GROUPS: tuple = ()

    def __init__(self, client: WebSocketClient, market: MarketManager,
                 accounts: AccountManager, orders: OrderManager,
                 positions: PositionManager,
                 instrument_type: Optional[InstrumentType] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.market = market
        self.accounts = accounts
        self.orders = orders
        self.positions = positions
        if instrument_type is not None:
            self.INSTRUMENT_TYPE = instrument_type
        self.log = logger or logging.getLogger(
            f"iq_option_api.{self.INSTRUMENT_TYPE.value}")

    # ==================================================================
    # Assets
    # ==================================================================
    def assets(self, *, only_open: bool = False, refresh: bool = False) -> List[Asset]:
        assets = self.market.list_assets(self.INSTRUMENT_TYPE,
                                         only_open=only_open, refresh=refresh)
        if self.ASSET_GROUPS:
            wanted = {g.lower() for g in self.ASSET_GROUPS}
            filtered = [a for a in assets if a.group.lower() in wanted]
            if filtered:
                return filtered
        return assets

    def open_assets(self) -> List[Asset]:
        return self.assets(only_open=True, refresh=True)

    def get_asset(self, name: "str | int") -> Asset:
        return self.market.get_asset(name, self.INSTRUMENT_TYPE)

    def is_open(self, asset: "str | int") -> bool:
        return self.market.is_open(asset, self.INSTRUMENT_TYPE)

    def symbols(self) -> List[str]:
        return [a.name for a in self.assets()]

    # ==================================================================
    # Prices
    # ==================================================================
    def price(self, asset: "str | int", *, timeout: float = 20.0) -> Price:
        return self.market.price(asset, self.INSTRUMENT_TYPE, timeout=timeout)

    def bid_ask(self, asset: "str | int", *, timeout: float = 20.0) -> Dict[str, Optional[float]]:
        return self.market.bid_ask(asset, self.INSTRUMENT_TYPE, timeout=timeout)

    def spread(self, asset: "str | int", *, timeout: float = 20.0) -> Optional[float]:
        return self.bid_ask(asset, timeout=timeout).get("spread")

    def candles(self, asset: "str | int", size: int = 60, count: int = 100) -> List[Any]:
        return self.market.get_candles(asset, size, count, instrument_type=self.INSTRUMENT_TYPE)

    def historical_data(self, asset: "str | int", size: int, count: int) -> List[Any]:
        return self.market.historical_data(asset, size, count,
                                           instrument_type=self.INSTRUMENT_TYPE)

    def subscribe_ticks(self, asset: "str | int", callback=None):
        return self.market.subscribe_ticks(asset, self.INSTRUMENT_TYPE, callback)

    def subscribe_candles(self, asset: "str | int", size: int = 60, callback=None):
        return self.market.subscribe_candles(asset, size, callback, self.INSTRUMENT_TYPE)

    # ==================================================================
    # Instrument / leverage / margin
    # ==================================================================
    def get_instrument(self, asset: "str | int", *, refresh: bool = False) -> Instrument:
        asset_id = self.market.asset_id(asset, self.INSTRUMENT_TYPE)
        try:
            return self.market.instruments.find_marginal(
                self.INSTRUMENT_TYPE, asset_id, refresh=refresh)
        except InstrumentError:
            # fall back to a synthetic instrument so the order can still carry
            # the asset id (some deployments accept active_id only)
            asset_obj = self.get_asset(asset)
            return Instrument(instrument_id="", asset_id=asset_id,
                              symbol=asset_obj.name,
                              instrument_type=self.INSTRUMENT_TYPE)

    def leverages(self, asset: "str | int") -> List[int]:
        asset_id = self.market.asset_id(asset, self.INSTRUMENT_TYPE)
        return self.market.instruments.leverages(self.INSTRUMENT_TYPE, asset_id)

    def default_leverage(self, asset: "str | int") -> Optional[int]:
        instrument = self.get_instrument(asset)
        if instrument.leverage:
            return instrument.leverage
        available = self.leverages(asset)
        return available[0] if available else None

    def margin_required(self, asset: "str | int", amount: float,
                        leverage: Optional[int] = None) -> float:
        leverage = leverage or self.default_leverage(asset) or 1
        return float(amount) / float(leverage)

    def position_size(self, asset: "str | int", amount: float,
                      leverage: Optional[int] = None,
                      price: Optional[float] = None) -> float:
        leverage = leverage or self.default_leverage(asset) or 1
        price = price or self.market.current_price(asset, self.INSTRUMENT_TYPE)
        if not price:
            raise MarketError(f"cannot determine price of {asset}")
        return (float(amount) * float(leverage)) / float(price)

    # ==================================================================
    # Orders
    # ==================================================================
    def open_position(self, asset: "str | int", amount: float,
                      direction: "Direction | str", *,
                      leverage: Optional[int] = None,
                      stop_loss: Optional[float] = None,
                      take_profit: Optional[float] = None,
                      stop_loss_kind: str = "price",
                      take_profit_kind: str = "price",
                      order_type: "OrderType | str" = OrderType.MARKET,
                      limit_price: Optional[float] = None,
                      stop_price: Optional[float] = None,
                      check_market: bool = True,
                      timeout: Optional[float] = None) -> Order:
        """BUY/SELL a leveraged instrument."""
        direction = Direction.parse(direction)
        if direction in (Direction.CALL, Direction.PUT):
            direction = Direction.BUY if direction is Direction.CALL else Direction.SELL
        if isinstance(order_type, str):
            order_type = OrderType(order_type.lower())

        if check_market:
            self.market.ensure_open(asset, self.INSTRUMENT_TYPE)

        instrument = self.get_instrument(asset)
        leverage = leverage or instrument.leverage or self.default_leverage(asset)
        balance_id = self.accounts.user_balance_id

        order = self.orders.create(
            instrument=instrument, direction=direction, amount=amount,
            balance_id=balance_id, order_type=order_type,
            stop_loss=stop_loss, take_profit=take_profit, leverage=leverage,
            limit_price=limit_price, stop_price=stop_price,
        )
        self.orders.validate(order, balance=self._balance())

        body: Dict[str, Any] = {
            "user_balance_id": balance_id,
            "instrument_type": self.market.instruments.wire_type(self.INSTRUMENT_TYPE),
            "instrument_id": instrument.instrument_id or str(instrument.asset_id),
            "instrument_active_id": instrument.asset_id,
            "side": "buy" if direction.is_long else "sell",
            "type": order_type.value,
            "amount": str(float(amount)),
            "leverage": int(leverage) if leverage else 1,
        }
        if order_type is OrderType.LIMIT and limit_price is not None:
            body["limit_price"] = float(limit_price)
        if order_type is OrderType.STOP and stop_price is not None:
            body["stop_price"] = float(stop_price)
        if stop_loss is not None:
            body["stop_lose_kind"] = stop_loss_kind
            body["stop_lose_value"] = float(stop_loss)
        if take_profit is not None:
            body["take_profit_kind"] = take_profit_kind
            body["take_profit_value"] = float(take_profit)

        return self.orders.submit(order, MS_MARGINAL_PLACE, body,
                                  version="1.0", timeout=timeout)

    def buy(self, asset: "str | int", amount: float, **kwargs: Any) -> Order:
        return self.open_position(asset, amount, Direction.BUY, **kwargs)

    def sell(self, asset: "str | int", amount: float, **kwargs: Any) -> Order:
        return self.open_position(asset, amount, Direction.SELL, **kwargs)

    def market_order(self, asset: "str | int", amount: float,
                     direction: "Direction | str", **kwargs: Any) -> Order:
        return self.open_position(asset, amount, direction,
                                  order_type=OrderType.MARKET, **kwargs)

    def pending_order(self, asset: "str | int", amount: float,
                      direction: "Direction | str", *, limit_price: float,
                      **kwargs: Any) -> Order:
        return self.open_position(asset, amount, direction,
                                  order_type=OrderType.LIMIT,
                                  limit_price=limit_price, **kwargs)

    def cancel_order(self, order_id: int) -> bool:
        return self.orders.cancel(order_id, instrument_type=self.INSTRUMENT_TYPE)

    def modify_order(self, order_id: int, **kwargs: Any) -> Order:
        return self.orders.modify(order_id, **kwargs)

    # ==================================================================
    # Positions
    # ==================================================================
    def open_positions(self) -> List[Position]:
        return self.positions.open_positions(instrument_type=self.INSTRUMENT_TYPE)

    def get_position(self, position_id: int) -> Position:
        position = self.positions.get(position_id)
        if position is None:
            self.positions.refresh(instrument_types=[self.INSTRUMENT_TYPE])
            position = self.positions.get(position_id)
        if position is None:
            raise PositionError(f"position {position_id} not found")
        return position

    def position_of_order(self, order: "Order | int") -> Optional[Position]:
        order_id = order.order_id if isinstance(order, Order) else int(order)
        return self.positions.by_order_id(order_id) if order_id else None

    def set_sl_tp(self, position_id: int, *, stop_loss: Optional[float] = None,
                  take_profit: Optional[float] = None, use_pnl: bool = False) -> Position:
        return self.positions.set_stop_loss_take_profit(
            position_id, stop_loss=stop_loss, take_profit=take_profit, use_pnl=use_pnl)

    def floating_pnl(self, position_id: int) -> Optional[float]:
        return self.get_position(position_id).floating_pnl

    def close_position(self, position_id: int) -> bool:
        return self.positions.close(position_id)

    def close_all(self) -> int:
        return self.positions.close_all(instrument_type=self.INSTRUMENT_TYPE)

    def history(self, limit: int = 50) -> List[Order]:
        return [o for o in self.orders.history(limit)
                if o.instrument_type is self.INSTRUMENT_TYPE]

    # ==================================================================
    def _balance(self) -> Optional[float]:
        try:
            return self.accounts.balance(refresh=False)
        except Exception:
            return None
