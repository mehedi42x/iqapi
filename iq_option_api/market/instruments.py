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
from typing import Any, Dict, List, Optional, Tuple

from ..connection.protocol import MS_MARGINAL_INSTRUMENTS
from ..connection.websocket import WebSocketClient
from ..exceptions import InstrumentError
from ..models import Expiration, Instrument, InstrumentType

# expiration helpers ---------------------------------------------------------
BINARY_PERIODS = (60, 120, 180, 300, 600, 900, 1800, 3600, 14400, 86400)
BLITZ_DURATIONS = (5, 10, 15, 30, 60)


def expiration_candidates(now: Optional[float] = None, *,
                          quarters: int = 50) -> List[float]:
    """The expiry timestamps the platform actually offers, in platform order.

    IQ Option does not accept an arbitrary ``now + duration`` timestamp: an
    option expires either on one of the next **five whole minutes** (turbo) or
    on a **quarter-hour mark at least five minutes away** (binary).  Sending
    anything else is silently dropped by ``binary-options.open-option``, which
    looks like a lost reply on our side.

    The first five entries are therefore the turbo ladder and the rest are the
    binary ladder — the index of the chosen entry is what tells the two apart.
    """
    now = float(now or time.time())
    # Nearest whole minute.  Inside the last 30 seconds of a minute the next
    # minute is too close to be accepted, so the ladder starts one later.
    minute = now - (now % 60)
    start = minute + 60 if (minute + 60 - now) > 30 else minute + 120

    candidates = [start + index * 60 for index in range(5)]

    # Quarter-hour marks, at least five minutes out.
    mark = minute
    found = 0
    while found < quarters:
        mark += 60
        if int(mark) % 900 == 0 and (mark - now) > 300:
            candidates.append(mark)
            found += 1
    return [float(int(ts)) for ts in candidates]


TURBO_LADDER = 5   # candidates [0:5] are the 1-5 minute turbo expiries


def expiration_for(duration: int, *, now: Optional[float] = None,
                   ladder: Optional[str] = None) -> Tuple[float, int]:
    """Pick the offered expiry closest to ``duration`` **minutes**.

    Returns ``(timestamp, index)``.  ``index < 5`` means the expiry sits on the
    turbo ladder, anything above is a binary (quarter-hour) expiry.  Pass
    ``ladder="turbo"`` or ``ladder="binary"`` to constrain the search — a turbo
    order priced against a quarter-hour expiry (or the reverse) is rejected.
    """
    now = float(now or time.time())
    candidates = expiration_candidates(now)
    if ladder == "turbo":
        choices = range(0, TURBO_LADDER)
    elif ladder == "binary":
        choices = range(TURBO_LADDER, len(candidates))
    else:
        choices = range(len(candidates))
    target = float(duration) * 60.0
    index = min(choices, key=lambda i: abs((candidates[i] - now) - target))
    return candidates[index], index


def next_expiration(period: int, *, now: Optional[float] = None,
                    min_lead: float = 5.0) -> float:
    """Next aligned expiration timestamp for an option of ``period`` seconds."""
    now = now or time.time()
    if period >= 60:
        # Snap onto the ladder the platform publishes rather than guessing.
        timestamp, _ = expiration_for(max(1, int(round(period / 60.0))), now=now)
        if timestamp - now >= min_lead:
            return timestamp
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

    def next_expiration(self, period: int, *, min_lead: float = 5.0,
                        now: Optional[float] = None) -> Expiration:
        now = now if now is not None else self._now()
        ts = next_expiration(period, now=now, min_lead=min_lead)
        return Expiration(timestamp=ts, period=period, index=0)

    def expiration_for(self, duration_minutes: int, *,
                       now: Optional[float] = None,
                       ladder: Optional[str] = None) -> Expiration:
        """Platform-aligned expiry for ``duration_minutes``, with its ladder index.

        ``Expiration.index < 5`` marks a turbo-ladder expiry.  Always computed
        against **server** time — a client clock that drifts by a few seconds
        is enough to pick an expiry the gateway refuses.
        """
        now = now if now is not None else self._now()
        timestamp, index = expiration_for(duration_minutes, now=now, ladder=ladder)
        return Expiration(timestamp=timestamp,
                          period=int(duration_minutes) * 60, index=index)

    def _now(self) -> float:
        """Server time when we have it, wall clock otherwise."""
        try:
            server = float(getattr(self.ws, "server_time", 0.0) or 0.0)
        except (TypeError, ValueError):
            server = 0.0
        return server if server > 0 else time.time()

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
