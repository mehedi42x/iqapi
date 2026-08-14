"""Asset discovery and the asset-id catalog.

``ACTIVE_IDS`` is only a *bootstrap* mapping used when the server list has not
been fetched yet (or for offline id/name translation).  Anything that matters
for trading is refreshed from ``get-initialization-data`` /
``get-underlying-list`` so the live server always wins.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from ..connection.protocol import (
    MS_DIGITAL_UNDERLYING,
    MS_INITIALIZATION_DATA,
    MS_MARGINAL_UNDERLYING,
)
from ..connection.websocket import WebSocketClient
from ..exceptions import AssetError
from ..models import Asset, InstrumentType, MarketStatus

# Bootstrap id table (subset of the well known IQ Option active ids).
ACTIVE_IDS: Dict[str, int] = {
    "EURUSD": 1, "EURGBP": 2, "USDCHF": 3, "GBPJPY": 4, "AUDCAD": 5, "NZDUSD": 6,
    "USDRUB": 7, "GBPUSD": 8, "EURJPY": 9, "EURRUB": 10, "USDJPY": 11, "AUDUSD": 12,
    "GBPAUD": 13, "USDCAD": 14, "EURAUD": 15, "GBPCHF": 16, "EURCAD": 17, "AUDJPY": 18,
    "CHFJPY": 19, "GBPCAD": 20, "AUDCHF": 21, "CADCHF": 22, "NZDJPY": 23, "NZDCAD": 24,
    "AUDNZD": 25, "CADJPY": 26, "EURNZD": 27, "GBPNZD": 28, "NZDCHF": 29,
    "BTCUSD": 816, "ETHUSD": 848, "XRPUSD": 849, "LTCUSD": 850, "BCHUSD": 851,
    "EOSUSD": 852, "TRXUSD": 855, "DSHUSD": 858, "OMGUSD": 859, "ZECUSD": 860,
    "XLMUSD": 861, "ADAUSD": 862, "DOTUSD": 1132, "LINKUSD": 1133,
    "XAUUSD": 1004, "XAGUSD": 1005, "USOUSD": 1000, "UKOUSD": 1001, "NGCUSD": 1002,
    "AAPL": 100, "MSFT": 101, "GOOGL": 102, "AMZN": 103, "FB": 104, "TSLA": 105,
    "NFLX": 106, "NVDA": 107, "INTC": 108, "BABA": 109, "TWTR": 110, "V": 111,
    "SPX": 200, "NDX": 201, "DJI": 202, "DAX": 203, "FTSE": 204, "N225": 205,
    "SPY": 300, "QQQ": 301, "IWM": 302, "EEM": 303, "GLD": 304,
    "EURUSD-OTC": 76, "GBPUSD-OTC": 78, "USDJPY-OTC": 79, "AUDCAD-OTC": 86,
    "EURJPY-OTC": 77, "USDCHF-OTC": 80, "NZDUSD-OTC": 81, "EURGBP-OTC": 82,
}

_ID_TO_NAME: Dict[int, str] = {v: k for k, v in ACTIVE_IDS.items()}

# Grouping heuristics used when the server does not give an explicit group.
_GROUP_TO_TYPE = {
    "forex": InstrumentType.FOREX,
    "crypto": InstrumentType.CRYPTO,
    "cryptocurrency": InstrumentType.CRYPTO,
    "stocks": InstrumentType.STOCK,
    "stock": InstrumentType.STOCK,
    "commodities": InstrumentType.COMMODITY,
    "commodity": InstrumentType.COMMODITY,
    "indices": InstrumentType.INDEX,
    "index": InstrumentType.INDEX,
    "etf": InstrumentType.ETF,
    "etfs": InstrumentType.ETF,
}


def asset_id_of(name: str) -> Optional[int]:
    return ACTIVE_IDS.get(name.upper())


def asset_name_of(asset_id: int) -> Optional[str]:
    return _ID_TO_NAME.get(int(asset_id))


class AssetCatalog:
    """Discovers assets per instrument type and answers id/name questions."""

    #: Market schedules and payouts move during the day, so the catalog is
    #: only trusted for this long.  An indefinitely cached "market open" flag
    #: is what let orders be sent into markets that had since closed.
    CACHE_TTL = 60.0

    def __init__(self, client: WebSocketClient, logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.log = logger or logging.getLogger("iq_option_api.assets")
        self._by_type: Dict[InstrumentType, Dict[str, Asset]] = {}
        self._loaded_at: Dict[InstrumentType, float] = {}
        self._init_data: Dict[str, Any] = {}
        self._init_data_at: float = 0.0
        self._lock = threading.RLock()

    # ==================================================================
    # Raw server data
    # ==================================================================
    def initialization_data(self, *, refresh: bool = False,
                            timeout: Optional[float] = None) -> Dict[str, Any]:
        """``get-initialization-data`` - binary/turbo/blitz actives + payouts."""
        import time as _time
        with self._lock:
            fresh = (_time.time() - self._init_data_at) < self.CACHE_TTL
            if self._init_data and not refresh and fresh:
                return self._init_data
        payload = self.ws.call(MS_INITIALIZATION_DATA, {}, version="3.0", timeout=timeout)
        if not isinstance(payload, dict):
            raise AssetError("unexpected initialization-data payload", details=payload)
        with self._lock:
            self._init_data = payload
            self._init_data_at = _time.time()
        return payload

    # ==================================================================
    # Per instrument type discovery
    # ==================================================================
    def binary_assets(self, *, refresh: bool = False, turbo: bool = False) -> List[Asset]:
        data = self.initialization_data(refresh=refresh)
        section = data.get("turbo" if turbo else "binary", {})
        actives = section.get("actives", {}) if isinstance(section, dict) else {}
        itype = InstrumentType.TURBO if turbo else InstrumentType.BINARY
        assets: List[Asset] = []
        for active_id, item in (actives or {}).items():
            if not isinstance(item, dict):
                continue
            asset = Asset.from_payload({**item, "id": active_id}, instrument_type=itype)
            asset.name = asset.name or (asset_name_of(int(active_id)) or str(active_id))
            option = item.get("option", {}) if isinstance(item.get("option"), dict) else {}
            commission = option.get("profit", {}).get("commission") if isinstance(option.get("profit"), dict) else None
            if commission is not None:
                asset.profit_percent = 100.0 - float(commission)
            asset.is_suspended = bool(item.get("is_suspended", False))
            # The trading ``schedule`` is authoritative.  Ignoring it is what
            # produced "Cannot purchase an option (the asset is not available
            # at the moment)": ``enabled`` stays true for a closed FX pair on
            # the weekend, so the client happily sent an order the gateway
            # then refused.
            asset.market_status = MarketStatus(
                asset_id=asset.asset_id, name=asset.name,
                is_open=(not asset.is_suspended
                         and bool(item.get("enabled", True))
                         and self._schedule_is_open(asset.schedule)),
                instrument_type=itype, schedule=asset.schedule,
            )
            assets.append(asset)
        self._store(itype, assets)
        return assets

    def turbo_assets(self, *, refresh: bool = False) -> List[Asset]:
        return self.binary_assets(refresh=refresh, turbo=True)

    def blitz_assets(self, *, refresh: bool = False) -> List[Asset]:
        data = self.initialization_data(refresh=refresh)
        section = data.get("blitz", data.get("blitz-option", {}))
        actives = section.get("actives", {}) if isinstance(section, dict) else {}
        assets = []
        for active_id, item in (actives or {}).items():
            if not isinstance(item, dict):
                continue
            asset = Asset.from_payload({**item, "id": active_id}, instrument_type=InstrumentType.BLITZ)
            asset.name = asset.name or (asset_name_of(int(active_id)) or str(active_id))
            option = item.get("option", {}) if isinstance(item.get("option"), dict) else {}
            profit = option.get("profit", {}) if isinstance(option.get("profit"), dict) else {}
            commission = profit.get("commission")
            if commission is not None:
                asset.profit_percent = 100.0 - float(commission)
            asset.is_suspended = bool(item.get("is_suspended", False))
            # Blitz used to carry no market status at all, so ``is_open`` fell
            # back to ``is_enabled`` and was always True - orders were sent
            # into closed markets and rejected.
            asset.market_status = MarketStatus(
                asset_id=asset.asset_id, name=asset.name,
                is_open=(not asset.is_suspended
                         and bool(item.get("enabled", True))
                         and self._schedule_is_open(asset.schedule)),
                instrument_type=InstrumentType.BLITZ, schedule=asset.schedule,
            )
            assets.append(asset)
        self._store(InstrumentType.BLITZ, assets)
        return assets

    def digital_assets(self, *, refresh: bool = False,
                       timeout: Optional[float] = None) -> List[Asset]:
        payload = self.ws.call(MS_DIGITAL_UNDERLYING, {"filterSuspended": True},
                               version="3.0", timeout=timeout)
        items = payload.get("underlying", []) if isinstance(payload, dict) else []
        assets: List[Asset] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            asset = Asset.from_payload(item, instrument_type=InstrumentType.DIGITAL)
            asset.asset_id = int(item.get("active_id", asset.asset_id) or 0)
            asset.name = item.get("name", asset.name)
            asset.is_suspended = bool(item.get("is_suspended", False))
            asset.market_status = MarketStatus(
                asset_id=asset.asset_id, name=asset.name,
                is_open=self._schedule_is_open(item.get("schedule")) and not asset.is_suspended,
                instrument_type=InstrumentType.DIGITAL,
                schedule=list(item.get("schedule", []) or []),
            )
            assets.append(asset)
        self._store(InstrumentType.DIGITAL, assets)
        return assets

    def marginal_assets(self, instrument_type: InstrumentType, *,
                        timeout: Optional[float] = None) -> List[Asset]:
        """CFD / forex / crypto underlyings (``marginal-*`` microservices)."""
        wire_type = {
            InstrumentType.FOREX: "marginal-forex",
            InstrumentType.CFD: "marginal-cfd",
            InstrumentType.CRYPTO: "marginal-crypto",
        }.get(instrument_type, "marginal-cfd")

        payload = self.ws.call(MS_MARGINAL_UNDERLYING, {"type": wire_type},
                               version="1.0", timeout=timeout)
        items = payload.get("items", payload.get("underlying", [])) if isinstance(payload, dict) else []
        assets: List[Asset] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            asset = Asset.from_payload(item, instrument_type=instrument_type)
            asset.asset_id = int(item.get("active_id", asset.asset_id) or 0)
            asset.market_status = MarketStatus(
                asset_id=asset.asset_id, name=asset.name,
                is_open=self._schedule_is_open(item.get("schedule")) and not asset.is_suspended,
                instrument_type=instrument_type,
                schedule=list(item.get("schedule", []) or []),
            )
            assets.append(asset)
        self._store(instrument_type, assets)
        return assets

    # ==================================================================
    # Lookup
    # ==================================================================
    def all(self, instrument_type: InstrumentType, *, refresh: bool = False) -> List[Asset]:
        import time as _time
        with self._lock:
            cached = list(self._by_type.get(instrument_type, {}).values())
            age = _time.time() - self._loaded_at.get(instrument_type, 0.0)
        if cached and not refresh and age < self.CACHE_TTL:
            return cached
        loader = {
            InstrumentType.BINARY: self.binary_assets,
            InstrumentType.TURBO: self.turbo_assets,
            InstrumentType.BLITZ: self.blitz_assets,
            InstrumentType.DIGITAL: self.digital_assets,
        }.get(instrument_type)
        if loader is not None:
            return loader(refresh=True)
        return self.marginal_assets(instrument_type)

    def find(self, name: str, instrument_type: Optional[InstrumentType] = None) -> Asset:
        import time as _time
        key = name.upper()
        types = [instrument_type] if instrument_type else list(self._by_type.keys())
        for itype in types:
            with self._lock:
                bucket = self._by_type.get(itype, {})
                age = _time.time() - self._loaded_at.get(itype, 0.0)
            # An expired entry must not be returned: its ``market_status`` is
            # what the trade path checks before sending the order.
            if key in bucket and age < self.CACHE_TTL:
                return bucket[key]
        if instrument_type is not None:
            for asset in self.all(instrument_type, refresh=True):
                if asset.name.upper() == key:
                    return asset
        fallback_id = asset_id_of(key)
        if fallback_id is not None:
            return Asset(asset_id=fallback_id, name=key,
                         instrument_type=instrument_type or InstrumentType.UNKNOWN)
        raise AssetError(f"asset {name!r} not found"
                         + (f" for {instrument_type.value}" if instrument_type else ""))

    def resolve_id(self, asset: "str | int",
                   instrument_type: Optional[InstrumentType] = None) -> int:
        if isinstance(asset, int):
            return asset
        if str(asset).isdigit():
            return int(asset)
        return self.find(str(asset), instrument_type).asset_id

    def resolve_name(self, asset: "str | int") -> str:
        if isinstance(asset, str) and not asset.isdigit():
            return asset.upper()
        name = asset_name_of(int(asset))
        if name:
            return name
        with self._lock:
            for bucket in self._by_type.values():
                for candidate in bucket.values():
                    if candidate.asset_id == int(asset):
                        return candidate.name
        raise AssetError(f"cannot resolve name of asset id {asset}")

    def open_assets(self, instrument_type: InstrumentType, *, refresh: bool = True) -> List[Asset]:
        return [a for a in self.all(instrument_type, refresh=refresh) if a.is_open]

    # ==================================================================
    @staticmethod
    def _schedule_is_open(schedule: Any, now: Optional[float] = None) -> bool:
        import time as _time
        if not schedule:
            return True
        now = now or _time.time()
        for window in schedule:
            if isinstance(window, dict):
                open_at, close_at = window.get("open"), window.get("close")
            elif isinstance(window, (list, tuple)) and len(window) >= 2:
                open_at, close_at = window[0], window[1]
            else:
                continue
            try:
                if float(open_at) <= now <= float(close_at):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _store(self, instrument_type: InstrumentType, assets: List[Asset]) -> None:
        import time as _time
        with self._lock:
            self._by_type[instrument_type] = {a.name.upper(): a for a in assets if a.name}
            self._loaded_at[instrument_type] = _time.time()
