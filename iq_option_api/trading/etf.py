"""ETF trading - CFD based."""

from __future__ import annotations

from typing import Any, Dict, List

from ..models import Asset, InstrumentType
from .marginal import MarginalTrading

POPULAR_ETFS = ("SPY", "QQQ", "IWM", "EEM", "GLD", "SLV", "XLF", "XLE", "ARKK", "VXX")


class ETFTrading(MarginalTrading):
    INSTRUMENT_TYPE = InstrumentType.ETF
    ASSET_GROUPS = ("etf", "etfs")

    # ------------------------------------------------------------------
    def discover(self, *, only_open: bool = False) -> List[Asset]:
        return self.assets(only_open=only_open, refresh=True)

    def etf_symbols(self) -> List[str]:
        return [a.name for a in self.assets()]

    def popular(self) -> List[Asset]:
        wanted = set(POPULAR_ETFS)
        return [a for a in self.assets() if a.name.upper() in wanted]

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

    def etf_price(self, symbol: "str | int") -> float:
        return self.market.current_price(symbol, self.INSTRUMENT_TYPE)
