"""Commodity trading (gold, silver, oil, ...) - CFD based."""

from __future__ import annotations

from typing import Any, Dict, List

from ..models import Asset, InstrumentType
from .marginal import MarginalTrading

METALS = ("XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD", "GOLD", "SILVER", "PLATINUM")
ENERGY = ("USCRUDE", "UKBRENT", "NGAS", "OIL", "BRENT", "WTI")
AGRICULTURAL = ("CORN", "WHEAT", "SOYBEAN", "SUGAR", "COFFEE", "COCOA", "COTTON")


class CommoditiesTrading(MarginalTrading):
    INSTRUMENT_TYPE = InstrumentType.COMMODITY
    ASSET_GROUPS = ("commodity", "commodities")

    # ------------------------------------------------------------------
    def discover(self, *, only_open: bool = False) -> List[Asset]:
        return self.assets(only_open=only_open, refresh=True)

    def commodity_symbols(self) -> List[str]:
        return [a.name for a in self.assets()]

    def _match(self, names: tuple) -> List[Asset]:
        wanted = {n.upper() for n in names}
        return [a for a in self.assets() if a.name.upper() in wanted]

    def metals(self) -> List[Asset]:
        return self._match(METALS)

    def energy(self) -> List[Asset]:
        return self._match(ENERGY)

    def agricultural(self) -> List[Asset]:
        return self._match(AGRICULTURAL)

    def gold(self) -> Asset:
        for name in ("XAUUSD", "GOLD"):
            try:
                return self.get_asset(name)
            except Exception:
                continue
        raise LookupError("gold asset not available")

    def silver(self) -> Asset:
        for name in ("XAGUSD", "SILVER"):
            try:
                return self.get_asset(name)
            except Exception:
                continue
        raise LookupError("silver asset not available")

    def oil(self) -> Asset:
        for name in ("USCRUDE", "UKBRENT", "OIL"):
            try:
                return self.get_asset(name)
            except Exception:
                continue
        raise LookupError("oil asset not available")

    def quote(self, symbol: "str | int") -> Dict[str, Any]:
        quote = self.bid_ask(symbol)
        return {
            "symbol": self.market.asset_name(symbol),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "price": quote.get("mid"),
            "spread": quote.get("spread"),
            "is_open": self.is_open(symbol),
        }
