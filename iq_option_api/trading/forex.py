"""Forex trading (marginal-forex)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import Asset, InstrumentType, Order
from .marginal import MarginalTrading

MAJOR_PAIRS = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD")
CROSS_PAIRS = ("EURGBP", "EURJPY", "GBPJPY", "EURAUD", "AUDJPY", "CHFJPY",
               "CADJPY", "EURCAD", "GBPCHF", "AUDNZD")


class ForexTrading(MarginalTrading):
    INSTRUMENT_TYPE = InstrumentType.FOREX

    # ------------------------------------------------------------------
    def currency_pairs(self, *, only_open: bool = False) -> List[str]:
        return [a.name for a in self.assets(only_open=only_open)]

    def major_pairs(self) -> List[Asset]:
        names = set(MAJOR_PAIRS)
        found = [a for a in self.assets() if a.name.upper() in names]
        return found or [self.get_asset(n) for n in MAJOR_PAIRS]

    def cross_pairs(self) -> List[Asset]:
        names = set(CROSS_PAIRS)
        return [a for a in self.assets() if a.name.upper() in names]

    # convenience aliases used by many strategies -----------------------
    def eurusd(self) -> Asset:
        return self.get_asset("EURUSD")

    def gbpusd(self) -> Asset:
        return self.get_asset("GBPUSD")

    def usdjpy(self) -> Asset:
        return self.get_asset("USDJPY")

    # ------------------------------------------------------------------
    def pip_value(self, asset: "str | int", *, lot: float = 100000.0) -> float:
        """Approximate value of one pip for a standard position."""
        name = self.market.asset_name(asset).upper()
        pip = 0.01 if name.endswith("JPY") else 0.0001
        return pip * lot

    def pips_to_price(self, asset: "str | int", pips: float,
                      *, direction_long: bool = True,
                      reference: Optional[float] = None) -> float:
        """Convert a pip distance into an absolute SL/TP price."""
        name = self.market.asset_name(asset).upper()
        pip = 0.01 if name.endswith("JPY") else 0.0001
        price = reference or self.market.current_price(asset, self.INSTRUMENT_TYPE)
        delta = pips * pip
        return price + delta if direction_long else price - delta

    def buy_with_pips(self, asset: "str | int", amount: float, *,
                      stop_loss_pips: Optional[float] = None,
                      take_profit_pips: Optional[float] = None,
                      leverage: Optional[int] = None, **kwargs: Any) -> Order:
        price = self.market.current_price(asset, self.INSTRUMENT_TYPE)
        sl = (self.pips_to_price(asset, stop_loss_pips, direction_long=False, reference=price)
              if stop_loss_pips else None)
        tp = (self.pips_to_price(asset, take_profit_pips, direction_long=True, reference=price)
              if take_profit_pips else None)
        return self.buy(asset, amount, stop_loss=sl, take_profit=tp,
                        leverage=leverage, **kwargs)

    def sell_with_pips(self, asset: "str | int", amount: float, *,
                       stop_loss_pips: Optional[float] = None,
                       take_profit_pips: Optional[float] = None,
                       leverage: Optional[int] = None, **kwargs: Any) -> Order:
        price = self.market.current_price(asset, self.INSTRUMENT_TYPE)
        sl = (self.pips_to_price(asset, stop_loss_pips, direction_long=True, reference=price)
              if stop_loss_pips else None)
        tp = (self.pips_to_price(asset, take_profit_pips, direction_long=False, reference=price)
              if take_profit_pips else None)
        return self.sell(asset, amount, stop_loss=sl, take_profit=tp,
                         leverage=leverage, **kwargs)

    def market_info(self, asset: "str | int") -> Dict[str, Any]:
        quote = self.bid_ask(asset)
        return {
            "asset": self.market.asset_name(asset),
            "asset_id": self.market.asset_id(asset, self.INSTRUMENT_TYPE),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "spread": quote.get("spread"),
            "leverages": self.leverages(asset),
            "is_open": self.is_open(asset),
        }
