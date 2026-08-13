"""Stock index trading (S&P 500, NASDAQ, DAX ...) - CFD based."""

from __future__ import annotations

from typing import Any, Dict, List

from ..models import Asset, InstrumentType
from .marginal import MarginalTrading

MAJOR_INDICES = ("SP500", "SPX500", "US500", "NASDAQ", "NAS100", "US100",
                 "DOW", "US30", "DAX", "GER30", "FTSE100", "UK100",
                 "NIKKEI", "JPN225", "CAC40", "FRA40", "ASX200", "HK50")


class IndicesTrading(MarginalTrading):
    INSTRUMENT_TYPE = InstrumentType.INDEX
    ASSET_GROUPS = ("index", "indices", "indice")

    # ------------------------------------------------------------------
    def discover(self, *, only_open: bool = False) -> List[Asset]:
        return self.assets(only_open=only_open, refresh=True)

    def index_symbols(self) -> List[str]:
        return [a.name for a in self.assets()]

    def major(self) -> List[Asset]:
        wanted = set(MAJOR_INDICES)
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

    def index_price(self, symbol: "str | int") -> float:
        return self.market.current_price(symbol, self.INSTRUMENT_TYPE)
