"""Market manager - the common layer above every market.

Combines the asset catalog, price stream, candles and instrument registry, and
answers the questions that all trading modules ask before placing an order:
*is this market open?*, *what is the asset id?*, *what is the current price?*
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..connection.websocket import WebSocketClient
from ..exceptions import MarketError
from ..models import Asset, Candle, InstrumentType, MarketStatus, Price, Tick
from .assets import AssetCatalog
from .candles import CandleManager
from .instruments import InstrumentRegistry
from .prices import PriceStream


class MarketManager:
    def __init__(self, client: WebSocketClient, logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.log = logger or logging.getLogger("iq_option_api.market")
        self.assets = AssetCatalog(client, logger=self.log)
        self.prices = PriceStream(client, logger=self.log)
        self.candles = CandleManager(client, logger=self.log)
        self.instruments = InstrumentRegistry(client, logger=self.log)

    # ==================================================================
    # Time
    # ==================================================================
    @property
    def server_time(self) -> float:
        return self.ws.server_time

    def sync_time(self, timeout: float = 10.0) -> float:
        return self.ws.sync_time(timeout=timeout)

    # ==================================================================
    # Assets
    # ==================================================================
    def list_assets(self, instrument_type: InstrumentType, *,
                    only_open: bool = False, refresh: bool = False) -> List[Asset]:
        assets = self.assets.all(instrument_type, refresh=refresh)
        return [a for a in assets if a.is_open] if only_open else assets

    def get_asset(self, name: "str | int",
                  instrument_type: Optional[InstrumentType] = None) -> Asset:
        if isinstance(name, int) or str(name).isdigit():
            asset_id = int(name)
            if instrument_type:
                for asset in self.assets.all(instrument_type):
                    if asset.asset_id == asset_id:
                        return asset
            return Asset(asset_id=asset_id,
                         name=self.assets.resolve_name(asset_id),
                         instrument_type=instrument_type or InstrumentType.UNKNOWN)
        return self.assets.find(str(name), instrument_type)

    def asset_id(self, asset: "str | int",
                 instrument_type: Optional[InstrumentType] = None) -> int:
        return self.assets.resolve_id(asset, instrument_type)

    def asset_name(self, asset: "str | int") -> str:
        return self.assets.resolve_name(asset)

    # ==================================================================
    # Market status
    # ==================================================================
    def market_status(self, asset: "str | int",
                      instrument_type: InstrumentType) -> MarketStatus:
        resolved = self.get_asset(asset, instrument_type)
        if resolved.market_status is not None:
            return resolved.market_status
        status = MarketStatus(
            asset_id=resolved.asset_id,
            name=resolved.name,
            is_open=resolved.is_enabled and not resolved.is_suspended,
            instrument_type=instrument_type,
            schedule=resolved.schedule,
        )
        return status

    def is_open(self, asset: "str | int", instrument_type: InstrumentType) -> bool:
        try:
            return self.market_status(asset, instrument_type).is_open
        except Exception as exc:
            self.log.debug("market status check failed: %s", exc)
            return False

    def ensure_open(self, asset: "str | int", instrument_type: InstrumentType) -> MarketStatus:
        status = self.market_status(asset, instrument_type)
        if not status.is_open:
            raise MarketError(
                f"market closed for {status.name or asset} ({instrument_type.value})",
                details={"asset_id": status.asset_id, "opens_at": status.open_time})
        return status

    def schedule(self, asset: "str | int", instrument_type: InstrumentType) -> List[Dict[str, Any]]:
        return self.market_status(asset, instrument_type).schedule

    def open_assets(self, instrument_type: InstrumentType) -> List[Asset]:
        return self.assets.open_assets(instrument_type)

    # ==================================================================
    # Prices
    # ==================================================================
    def price(self, asset: "str | int",
              instrument_type: Optional[InstrumentType] = None,
              *, timeout: float = 20.0) -> Price:
        return self.prices.wait_for_price(self.asset_id(asset, instrument_type), timeout=timeout)

    def bid_ask(self, asset: "str | int",
                instrument_type: Optional[InstrumentType] = None,
                *, timeout: float = 20.0) -> Dict[str, Optional[float]]:
        return self.prices.bid_ask(self.asset_id(asset, instrument_type), timeout=timeout)

    def subscribe_ticks(self, asset: "str | int",
                        instrument_type: Optional[InstrumentType] = None,
                        callback=None):
        return self.prices.subscribe(self.asset_id(asset, instrument_type), callback=callback)

    def unsubscribe_ticks(self, asset: "str | int",
                          instrument_type: Optional[InstrumentType] = None) -> bool:
        return self.prices.unsubscribe(self.asset_id(asset, instrument_type))

    def ticks(self, asset: "str | int", count: int = 50,
              instrument_type: Optional[InstrumentType] = None) -> List[Tick]:
        return self.prices.ticks(self.asset_id(asset, instrument_type), count)

    # ==================================================================
    # Candles
    # ==================================================================
    def get_candles(self, asset: "str | int", size: int = 60, count: int = 100,
                    *, end_time: Optional[float] = None,
                    instrument_type: Optional[InstrumentType] = None) -> List[Candle]:
        return self.candles.get_candles(self.asset_id(asset, instrument_type),
                                        size, count, end_time=end_time)

    def historical_data(self, asset: "str | int", size: int, count: int,
                        *, end_time: Optional[float] = None,
                        instrument_type: Optional[InstrumentType] = None) -> List[Candle]:
        return self.candles.history(self.asset_id(asset, instrument_type),
                                    size, count, end_time=end_time)

    def subscribe_candles(self, asset: "str | int", size: int = 60, callback=None,
                          instrument_type: Optional[InstrumentType] = None):
        return self.candles.subscribe(self.asset_id(asset, instrument_type), size, callback)

    def unsubscribe_candles(self, asset: "str | int", size: int = 60,
                            instrument_type: Optional[InstrumentType] = None) -> bool:
        return self.candles.unsubscribe(self.asset_id(asset, instrument_type), size)

    def current_price(self, asset: "str | int",
                      instrument_type: Optional[InstrumentType] = None) -> float:
        asset_id = self.asset_id(asset, instrument_type)
        cached = self.prices.latest(asset_id)
        if cached is not None and cached.value is not None:
            if time.time() - cached.timestamp < 30:
                return cached.value
        return self.candles.current_price(asset_id)

    # ==================================================================
    def close(self) -> None:
        self.prices.unsubscribe_all()
        self.candles.unsubscribe_all()
