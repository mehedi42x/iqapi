"""Crypto trading.

Crypto on IQ Option comes either as ``marginal-crypto`` or as a crypto CFD.
The instrument type is detected from the server data and the order then flows
through the shared marginal/CFD engine - no duplicated trading logic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..exceptions import AssetError
from ..models import Asset, InstrumentType, Order
from .marginal import MarginalTrading

MAJOR_CRYPTO = ("BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD", "BCHUSD",
                "ADAUSD", "DOTUSD", "LINKUSD", "EOSUSD", "TRXUSD")


class CryptoTrading(MarginalTrading):
    INSTRUMENT_TYPE = InstrumentType.CRYPTO

    def __init__(self, *args: Any, cfd: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cfd = cfd          # CFDTrading, used when an asset is a crypto CFD

    # ------------------------------------------------------------------
    def discover(self, *, only_open: bool = False) -> List[Asset]:
        try:
            return self.assets(only_open=only_open, refresh=True)
        except Exception as exc:
            self.log.debug("marginal-crypto discovery failed (%s), trying CFD", exc)
            if self._cfd is not None:
                return self._cfd.crypto_cfds()
            raise

    def crypto_symbols(self) -> List[str]:
        return [a.name for a in self.discover()]

    def major(self) -> List[Asset]:
        names = set(MAJOR_CRYPTO)
        return [a for a in self.discover() if a.name.upper() in names]

    def btc(self) -> Asset:
        return self.get_asset("BTCUSD")

    def eth(self) -> Asset:
        return self.get_asset("ETHUSD")

    # ------------------------------------------------------------------
    def detect_instrument_type(self, asset: "str | int") -> InstrumentType:
        """Is this crypto tradable as marginal-crypto, or only as a CFD?"""
        asset_id = self.market.asset_id(asset, InstrumentType.CRYPTO)
        try:
            self.market.instruments.find_marginal(InstrumentType.CRYPTO, asset_id)
            return InstrumentType.CRYPTO
        except Exception:
            pass
        if self._cfd is not None:
            try:
                self.market.instruments.find_marginal(InstrumentType.CFD, asset_id)
                return InstrumentType.CFD
            except Exception:
                pass
        return InstrumentType.CRYPTO

    def _engine(self, asset: "str | int") -> MarginalTrading:
        """Route CFD-only crypto through the CFD layer."""
        if self.detect_instrument_type(asset) is InstrumentType.CFD and self._cfd is not None:
            return self._cfd
        return self

    def open_position(self, asset: "str | int", amount: float,
                      direction: Any, **kwargs: Any) -> Order:
        engine = self._engine(asset)
        if engine is self:
            return super().open_position(asset, amount, direction, **kwargs)
        self.log.info("routing %s through the CFD layer", asset)
        return engine.open_position(asset, amount, direction, **kwargs)

    def quote(self, symbol: "str | int") -> Dict[str, Any]:
        quote = self.bid_ask(symbol)
        return {
            "symbol": self.market.asset_name(symbol),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "price": quote.get("mid"),
            "spread": quote.get("spread"),
            "instrument_type": self.detect_instrument_type(symbol).value,
            "is_open": self.is_open(symbol),
        }
