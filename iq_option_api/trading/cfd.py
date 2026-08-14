"""CFD trading - the full leveraged module.

Every CFD sub-family (forex CFD, stock CFD, commodity CFD, ETF CFD, index CFD)
is reachable from here.  The asset-class modules (stocks, commodities, ...)
delegate to this same engine instead of duplicating the trading logic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import Asset, InstrumentType, Order, Position
from .marginal import MarginalTrading

CFD_GROUPS = ("forex", "stock", "stocks", "commodity", "commodities",
              "etf", "etfs", "index", "indices", "crypto")


class CFDTrading(MarginalTrading):
    INSTRUMENT_TYPE = InstrumentType.CFD

    # ==================================================================
    # Asset families
    # ==================================================================
    def assets_by_group(self, group: str, *, refresh: bool = False) -> List[Asset]:
        wanted = group.lower()
        return [a for a in self.assets(refresh=refresh) if a.group.lower().startswith(wanted)]

    def forex_cfds(self) -> List[Asset]:
        return self.assets_by_group("forex")

    def stock_cfds(self) -> List[Asset]:
        return self.assets_by_group("stock")

    def commodity_cfds(self) -> List[Asset]:
        return self.assets_by_group("commodit")

    def etf_cfds(self) -> List[Asset]:
        return self.assets_by_group("etf")

    def index_cfds(self) -> List[Asset]:
        return self.assets_by_group("ind")

    def crypto_cfds(self) -> List[Asset]:
        return self.assets_by_group("crypto")

    def groups(self) -> List[str]:
        return sorted({a.group for a in self.assets() if a.group})

    # ==================================================================
    # Info
    # ==================================================================
    def instrument_info(self, asset: "str | int") -> Dict[str, Any]:
        instrument = self.get_instrument(asset)
        quote = self.bid_ask(asset)
        leverages = self.leverages(asset)
        return {
            "asset": self.market.asset_name(asset),
            "asset_id": instrument.asset_id,
            "instrument_id": instrument.instrument_id,
            "instrument_type": instrument.instrument_type.value,
            "is_open": self.is_open(asset),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "spread": quote.get("spread"),
            "leverages": leverages,
            "default_leverage": instrument.leverage or (leverages[0] if leverages else None),
            "min_amount": instrument.min_amount,
            "max_amount": instrument.max_amount,
        }

    def validate_order(self, asset: "str | int", amount: float,
                       leverage: Optional[int] = None) -> Dict[str, Any]:
        """Pre-trade check: market, leverage, margin, balance."""
        info = self.instrument_info(asset)
        leverage = leverage or info["default_leverage"] or 1
        available = info["leverages"]
        if available and int(leverage) not in available:
            raise ValueError(f"leverage {leverage} not available, options: {available}")
        margin = self.margin_required(asset, amount, leverage)
        balance = self._balance()
        return {
            "valid": info["is_open"] and (balance is None or balance >= margin),
            "market_open": info["is_open"],
            "leverage": leverage,
            "margin_required": margin,
            "balance": balance,
            "position_size": self.position_size(asset, amount, leverage,
                                                price=info.get("ask") or info.get("bid")),
        }

    # ==================================================================
    # Monitoring
    # ==================================================================
    def monitor(self) -> List[Dict[str, Any]]:
        rows = []
        for position in self.open_positions():
            rows.append({
                "position_id": position.position_id,
                "symbol": position.symbol,
                "direction": position.direction.value if position.direction else None,
                "invest": position.invest,
                "leverage": position.leverage,
                "open_price": position.open_price,
                "current_price": position.current_price,
                "floating_pnl": position.floating_pnl,
                "stop_loss": position.stop_loss,
                "take_profit": position.take_profit,
                "margin": position.margin,
            })
        return rows

    def total_margin(self) -> float:
        return sum(p.margin or 0.0 for p in self.open_positions())
