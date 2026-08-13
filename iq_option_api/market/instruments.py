"""Instrument registry - the common abstraction over every tradable thing.

Binary / Digital / Blitz build their instruments from option metadata,
Forex / CFD / Stock / Crypto / Commodity / ETF / Index from the
``marginal-instruments`` microservice.  Everything is normalised into
:class:`~iq_option_api.models.Instrument`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from ..connection.protocol import MS_MARGINAL_INSTRUMENTS
from ..connection.websocket import WebSocketClient
from ..exceptions import InstrumentError
from ..models import Expiration, Instrument, InstrumentType

# expiration helpers ---------------------------------------------------------
BINARY_PERIODS = (60, 120, 180, 300, 600, 900, 1800, 3600, 14400, 86400)
BLITZ_DURATIONS = (5, 10, 15, 30, 60)


def next_expiration(period: int, *, now: Optional[float] = None,
                    min_lead: float = 5.0) -> float:
    """Next aligned expiration timestamp for an option of ``period`` seconds."""
    now = now or time.time()
    if period >= 3600:
        base = now - (now % period)
        expiry = base + period
    else:
        base = now - (now % 60)
        expiry = base + max(60, period)
    while expiry - now < min_lead:
        expiry += period if period >= 60 else 60
    return float(int(expiry))


def expiration_list(period: int, count: int = 5, *, now: Optional[float] = None) -> List[Expiration]:
    now = now or time.time()
    result: List[Expiration] = []
    ts = next_expiration(period, now=now)
    for index in range(count):
        result.append(Expiration(timestamp=ts + index * period, period=period, index=index))
    return result


class InstrumentRegistry:
    """Resolves and caches instruments for every instrument type."""

    def __init__(self, client: WebSocketClient, logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.log = logger or logging.getLogger("iq_option_api.instruments")
        self._cache: Dict[str, List[Instrument]] = {}
        self._cache_at: Dict[str, float] = {}
        self._lock = threading.RLock()
        self.cache_ttl = 300.0

    # ==================================================================
    # Marginal instruments (forex / cfd / crypto / stock / etc.)
    # ==================================================================
    def marginal_instruments(self, instrument_type: InstrumentType, *,
                             refresh: bool = False,
                             timeout: Optional[float] = None) -> List[Instrument]:
        wire_type = self.wire_type(instrument_type)
        key = f"marginal:{wire_type}"
        cached = self._get_cached(key, refresh)
        if cached is not None:
            return cached

        payload = self.ws.call(MS_MARGINAL_INSTRUMENTS, {"type": wire_type},
                               version="1.0", timeout=timeout)
        items = payload.get("instruments", []) if isinstance(payload, dict) else []
        instruments: List[Instrument] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            instruments.append(Instrument(
                instrument_id=str(item.get("id", item.get("instrument_id", ""))),
                asset_id=int(item.get("active_id", 0) or 0),
                symbol=str(item.get("symbol", item.get("name", ""))),
                instrument_type=instrument_type,
                leverage=self._default_leverage(item),
                is_tradable=not bool(item.get("is_suspended", False)),
                min_amount=self._to_float(item.get("minimal_amount")),
                max_amount=self._to_float(item.get("maximal_amount")),
                raw=item,
            ))
        self._put_cached(key, instruments)
        return instruments

    def find_marginal(self, instrument_type: InstrumentType, asset_id: int,
                      *, refresh: bool = False) -> Instrument:
        for instrument in self.marginal_instruments(instrument_type, refresh=refresh):
            if instrument.asset_id == int(asset_id):
                return instrument
        if not refresh:
            return self.find_marginal(instrument_type, asset_id, refresh=True)
        raise InstrumentError(
            f"no {instrument_type.value} instrument for asset_id={asset_id}")

    def leverages(self, instrument_type: InstrumentType, asset_id: int,
                  *, timeout: Optional[float] = None) -> List[int]:
        """Available leverage values for a marginal instrument."""
        try:
            payload = self.ws.call(
                "get-leverages",
                {"instrument_type": self.wire_type(instrument_type), "active_id": int(asset_id)},
                version="1.0", timeout=timeout)
        except Exception as exc:
            self.log.debug("get-leverages failed: %s", exc)
            payload = None

        values: List[int] = []
        if isinstance(payload, dict):
            raw = payload.get("leverages", payload.get("items", []))
            for entry in raw or []:
                if isinstance(entry, dict):
                    for key in ("leverage", "value", "default"):
                        if entry.get(key):
                            values.append(int(entry[key]))
                            break
                else:
                    try:
                        values.append(int(entry))
                    except (TypeError, ValueError):
                        continue
        if not values:
            try:
                instrument = self.find_marginal(instrument_type, asset_id)
                raw = instrument.raw.get("leverages") or instrument.raw.get("leverage_profile")
                if isinstance(raw, list):
                    for entry in raw:
                        if isinstance(entry, dict) and entry.get("leverage"):
                            values.append(int(entry["leverage"]))
                        elif isinstance(entry, (int, float, str)):
                            values.append(int(entry))
            except InstrumentError:
                pass
        return sorted(set(values))

    # ==================================================================
    # Option expirations
    # ==================================================================
    @staticmethod
    def option_expirations(period: int, count: int = 5) -> List[Expiration]:
        return expiration_list(period, count)

    @staticmethod
    def next_expiration(period: int, *, min_lead: float = 5.0) -> Expiration:
        ts = next_expiration(period, min_lead=min_lead)
        return Expiration(timestamp=ts, period=period, index=0)

    # ==================================================================
    # Generic
    # ==================================================================
    def build(self, *, instrument_type: InstrumentType, asset_id: int,
              symbol: str = "", instrument_id: str = "", **kwargs: Any) -> Instrument:
        return Instrument(instrument_id=instrument_id, asset_id=int(asset_id),
                          symbol=symbol, instrument_type=instrument_type, **kwargs)

    @staticmethod
    def wire_type(instrument_type: InstrumentType) -> str:
        """Map our enum to the string the server expects."""
        return {
            InstrumentType.FOREX: "marginal-forex",
            InstrumentType.CFD: "marginal-cfd",
            InstrumentType.CRYPTO: "marginal-crypto",
            InstrumentType.STOCK: "marginal-cfd",
            InstrumentType.COMMODITY: "marginal-cfd",
            InstrumentType.ETF: "marginal-cfd",
            InstrumentType.INDEX: "marginal-cfd",
            InstrumentType.BINARY: "binary-option",
            InstrumentType.TURBO: "turbo-option",
            InstrumentType.DIGITAL: "digital-option",
            InstrumentType.BLITZ: "blitz-option",
        }.get(instrument_type, "marginal-cfd")

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
            self._cache_at.clear()

    # ------------------------------------------------------------------
    def _get_cached(self, key: str, refresh: bool) -> Optional[List[Instrument]]:
        if refresh:
            return None
        with self._lock:
            if key in self._cache and (time.time() - self._cache_at.get(key, 0)) < self.cache_ttl:
                return list(self._cache[key])
        return None

    def _put_cached(self, key: str, instruments: List[Instrument]) -> None:
        with self._lock:
            self._cache[key] = list(instruments)
            self._cache_at[key] = time.time()

    @staticmethod
    def _default_leverage(item: Dict[str, Any]) -> Optional[int]:
        for key in ("default_leverage", "leverage"):
            if item.get(key):
                try:
                    return int(item[key])
                except (TypeError, ValueError):
                    continue
        profile = item.get("leverages") or item.get("leverage_profile")
        if isinstance(profile, list) and profile:
            entry = profile[0]
            if isinstance(entry, dict) and entry.get("leverage"):
                try:
                    return int(entry["leverage"])
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
