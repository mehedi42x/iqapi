"""Stock trading.

Stocks are offered as CFDs on IQ Option, so the trading logic is inherited
from :class:`~iq_option_api.trading.marginal.MarginalTrading` (the CFD engine)
instead of being duplicated - only the discovery layer is stock specific.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..models import Asset, InstrumentType
from .marginal import MarginalTrading

POPULAR_STOCKS = ("AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "NFLX")


class StocksTrading(MarginalTrading):
    INSTRUMENT_TYPE = InstrumentType.STOCK
    ASSET_GROUPS = ("stock", "stocks")

    # ------------------------------------------------------------------
    def discover(self, *, only_open: bool = False) -> List[Asset]:
        return self.assets(only_open=only_open, refresh=True)

    def stock_symbols(self) -> List[str]:
        return [a.name for a in self.assets()]

    def popular(self) -> List[Asset]:
        names = set(POPULAR_STOCKS)
        return [a for a in self.assets() if a.name.upper() in names]

    def quote(self, symbol: str) -> Dict[str, Any]:
        quote = self.bid_ask(symbol)
        return {
            "symbol": self.market.asset_name(symbol),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "price": quote.get("mid"),
            "spread": quote.get("spread"),
            "is_open": self.is_open(symbol),
        }

    def stock_price(self, symbol: str) -> float:
        return self.market.current_price(symbol, self.INSTRUMENT_TYPE)
