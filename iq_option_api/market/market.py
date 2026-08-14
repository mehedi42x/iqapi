"""Market manager - the common layer above every market.

Combines the asset catalog, price stream, candles and instrument registry, and
answers the questions that all trading modules ask before placing an order:
*is this market open?*, *what is the asset id?*, *what is the current price?*
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..connection.protocol import (
    EVENT_TOP_ASSETS,
    EVENT_TRADERS_MOOD,
    MS_GET_INSTRUMENTS,
)
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
        # top-assets-updated is a push stream; keep the last frame per type
        self._top_assets_cache: Dict[str, Dict[str, Any]] = {}
        self._top_assets_subs: Dict[str, Any] = {}

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
    # Instruments / sentiment / top assets
    # ==================================================================
    #: ``get-instruments`` only knows about margin/CFD style markets.  Binary,
    #: turbo and blitz books live in the initialization data instead.
    _OPTION_TYPES = {"binary", "turbo", "blitz",
                     "binary-option", "turbo-option", "blitz-option"}

    @staticmethod
    def wire_instrument_type(instrument_type: "str | InstrumentType") -> str:
        """Normalise ``turbo`` / ``InstrumentType.TURBO`` to ``turbo-option``."""
        if isinstance(instrument_type, InstrumentType):
            return InstrumentRegistry.wire_type(instrument_type)
        name = str(instrument_type).strip().lower()
        if name in ("binary", "turbo", "blitz", "digital"):
            return f"{name}-option"
        return name

    def get_instruments(self, instrument_type: str = "binary",
                        *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Instrument book for an instrument type.

        ``get-instruments`` (v4.0) serves forex / CFD / crypto / digital only -
        asking it for ``binary`` or ``turbo`` returns an empty book.  Those are
        published through the initialization data, so we build an equivalent
        ``{"instruments": [...]}`` payload from there.
        """
        name = str(instrument_type).strip().lower()
        if name in self._OPTION_TYPES:
            return self._option_instruments(name)

        payload = self.ws.call(MS_GET_INSTRUMENTS, {"type": name},
                               version="4.0", timeout=timeout)
        return payload if isinstance(payload, dict) else {}

    def _option_instruments(self, name: str) -> Dict[str, Any]:
        """Binary / turbo / blitz book assembled from the initialization data."""
        kind = name.replace("-option", "")
        if kind == "blitz":
            assets = self.assets.blitz_assets()
        else:
            assets = self.assets.binary_assets(turbo=(kind == "turbo"))

        instruments = []
        for asset in assets:
            instruments.append({
                "id": asset.asset_id,
                "active_id": asset.asset_id,
                "name": asset.name,
                "type": f"{kind}-option",
                "is_open": asset.is_open,
                "minimal_amount": asset.minimal_amount,
                "maximal_amount": asset.maximal_amount,
                "profit_percent": asset.profit_percent,
            })
        return {"type": f"{kind}-option", "instruments": instruments}

    def top_assets(self, instrument_type: str = "binary", *,
                   timeout: Optional[float] = None) -> Dict[str, Any]:
        """Top assets for an instrument type.

        There is no ``get-top-assets-info`` request/response call - the
        platform only *pushes* this data on the ``top-assets-updated``
        subscription, which is why the old blocking call always came back
        empty.  We subscribe, wait for the first delivery, and cache it.
        """
        wire = self.wire_instrument_type(instrument_type)
        timeout = timeout if timeout is not None else 15.0

        cached = self._top_assets_cache.get(wire)
        if cached and (time.time() - cached["updated_at"]) < 60.0:
            return cached["data"]

        self.subscribe_top_assets(wire)
        deadline = time.time() + timeout
        while time.time() < deadline:
            cached = self._top_assets_cache.get(wire)
            if cached and cached["data"]:
                return cached["data"]
            try:
                payload = self.ws.wait_for(
                    EVENT_TOP_ASSETS, timeout=max(1.0, deadline - time.time()),
                    predicate=lambda p: self._top_assets_type(p) == wire)
            except Exception:
                break
            data = self._store_top_assets(payload)
            if data:
                return data

        cached = self._top_assets_cache.get(wire)
        return cached["data"] if cached else {}

    def subscribe_top_assets(self, instrument_type: "str | InstrumentType",
                             callback=None):
        """Subscribe to ``top-assets-updated`` for one instrument type."""
        wire = self.wire_instrument_type(instrument_type)
        existing = self._top_assets_subs.get(wire)
        if existing is not None and callback is None:
            return existing

        def _handler(payload: Any) -> None:
            data = self._store_top_assets(payload)
            if data and callback:
                callback(data)

        sub = self.ws.subscribe(EVENT_TOP_ASSETS,
                                params={"instrument_type": wire},
                                version="1.2", callback=_handler)
        self._top_assets_subs[wire] = sub
        return sub

    def unsubscribe_top_assets(self, instrument_type: "str | InstrumentType") -> bool:
        wire = self.wire_instrument_type(instrument_type)
        sub = self._top_assets_subs.pop(wire, None)
        return self.ws.unsubscribe(sub.subscription_id) if sub else False

    @staticmethod
    def _top_assets_type(payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        value = msg.get("instrument_type")
        return str(value) if value else None

    def _store_top_assets(self, payload: Any) -> Dict[str, Any]:
        """Cache one ``top-assets-updated`` frame keyed by instrument type."""
        wire = self._top_assets_type(payload)
        if not wire or not isinstance(payload, dict):
            return {}
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        data = msg.get("data")
        if isinstance(data, list):
            data = {str(entry.get("active_id")): entry
                    for entry in data if isinstance(entry, dict)}
        if not isinstance(data, dict) or not data:
            return {}
        self._top_assets_cache[wire] = {"data": data, "updated_at": time.time()}
        return data

    def subscribe_traders_mood(self, asset: "str | int",
                               instrument: str = "binary", callback=None):
        """Live ``traders-mood-changed`` sentiment updates."""
        asset_id = self.asset_id(asset)
        return self.ws.subscribe(
            EVENT_TRADERS_MOOD,
            params={"instrument": instrument, "asset_id": asset_id},
            callback=callback,
        )

    def unsubscribe_traders_mood(self, subscription) -> bool:
        return self.ws.unsubscribe(subscription.subscription_id)

    # ==================================================================
    def close(self) -> None:
        self.prices.unsubscribe_all()
        self.candles.unsubscribe_all()
