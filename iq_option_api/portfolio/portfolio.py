"""Portfolio management.

Wraps the three portfolio microservices:

* ``portfolio.get-positions``      - snapshot of open positions
* ``portfolio.get-stats``          - aggregated statistics
* ``portfolio.position-changed``   - live position stream (owned by
  :class:`~iq_option_api.trading.positions.PositionManager`, re-exposed here)

The portfolio never touches the *billing* balances - those live in the
``billing`` layer and are deliberately kept separate from trading data.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..account import AccountManager
from ..connection.protocol import MS_PORTFOLIO_STATS
from ..connection.websocket import WebSocketClient
from ..exceptions import ProtocolError
from ..models import (
    InstrumentType,
    Portfolio,
    PortfolioStats,
    Position,
    Trade,
)
from ..trading.positions import PositionManager

ALL_TYPES: List[InstrumentType] = [
    InstrumentType.BINARY, InstrumentType.TURBO, InstrumentType.DIGITAL,
    InstrumentType.BLITZ, InstrumentType.FOREX, InstrumentType.CFD,
    InstrumentType.CRYPTO,
]


class PortfolioManager:
    """Aggregated view over every open position of the active account."""

    def __init__(self, client: WebSocketClient, accounts: AccountManager,
                 positions: PositionManager,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.accounts = accounts
        self.positions = positions
        self.log = logger or logging.getLogger("iq_option_api.portfolio")
        self._stats: Optional[PortfolioStats] = None
        self._stats_at: float = 0.0

    # ==================================================================
    # Positions (portfolio.get-positions)
    # ==================================================================
    def get_positions(self, *, instrument_types: Optional[List[InstrumentType]] = None,
                      refresh: bool = True) -> List[Position]:
        types = instrument_types or ALL_TYPES
        if refresh:
            return self.positions.refresh(instrument_types=types)
        return self.positions.all()

    def open_positions(self, *, instrument_type: Optional[InstrumentType] = None
                       ) -> List[Position]:
        return self.positions.open_positions(instrument_type=instrument_type)

    def closed_positions(self) -> List[Position]:
        return self.positions.closed_positions()

    def get_position(self, position_id: int) -> Optional[Position]:
        return self.positions.get(position_id)

    def positions_by_asset(self, asset_id: int) -> List[Position]:
        return [p for p in self.open_positions() if p.asset_id == int(asset_id)]

    # ==================================================================
    # Live stream (portfolio.position-changed)
    # ==================================================================
    def subscribe(self, callback: Optional[Callable[[Position], None]] = None,
                  *, instrument_types: Optional[List[InstrumentType]] = None):
        """Subscribe to ``portfolio.position-changed`` for this account."""
        return self.positions.subscribe(
            user_id=self.accounts.user_id,
            balance_id=self.accounts.active_balance_id,
            instrument_types=instrument_types or ALL_TYPES,
            callback=callback,
        )

    def unsubscribe(self) -> None:
        self.positions.unsubscribe()

    def on_position_changed(self, callback: Callable[[Position], None]) -> None:
        self.positions.on_change(callback)

    # ==================================================================
    # Stats (portfolio.get-stats)
    # ==================================================================
    def get_stats(self, *, refresh: bool = True, max_age: float = 5.0,
                  timeout: Optional[float] = None) -> PortfolioStats:
        if (not refresh and self._stats is not None
                and time.time() - self._stats_at < max_age):
            return self._stats

        balance_id = self.accounts.active_balance_id
        body: Dict[str, Any] = {
            "user_balance_id": balance_id,
            "instrument_types": [
                self._wire(t) for t in ALL_TYPES
            ],
        }
        try:
            payload = self.ws.call(MS_PORTFOLIO_STATS, body, version="1.0",
                                   timeout=timeout)
        except Exception as exc:                       # server-side variations
            self.log.debug("portfolio.get-stats failed (%s), computing locally", exc)
            return self.local_stats()

        stats = self._parse_stats(payload)
        self._stats, self._stats_at = stats, time.time()
        return stats

    def local_stats(self) -> PortfolioStats:
        """Statistics computed from the locally known positions."""
        positions = self.open_positions()
        by_instrument: Dict[str, Dict[str, float]] = {}
        total_invest = expected = sell = pnl = 0.0
        for position in positions:
            total_invest += position.invest or 0.0
            expected += position.expected_profit or 0.0
            sell += position.sell_profit or 0.0
            pnl += position.floating_pnl or 0.0
            key = position.instrument_type.value
            bucket = by_instrument.setdefault(
                key, {"count": 0.0, "invest": 0.0, "pnl": 0.0})
            bucket["count"] += 1
            bucket["invest"] += position.invest or 0.0
            bucket["pnl"] += position.floating_pnl or 0.0

        stats = PortfolioStats(
            total_positions=len(positions),
            total_invest=total_invest,
            expected_profit=expected,
            sell_profit=sell,
            pnl=pnl,
            by_instrument=by_instrument,
        )
        self._stats, self._stats_at = stats, time.time()
        return stats

    # ==================================================================
    # Aggregate view
    # ==================================================================
    def snapshot(self, *, refresh: bool = True) -> Portfolio:
        positions = self.get_positions(refresh=refresh)
        stats = self.get_stats(refresh=refresh)
        return Portfolio(positions=positions, stats=stats,
                         balance_id=self.accounts.active_balance_id)

    def total_invest(self) -> float:
        return sum(p.invest or 0.0 for p in self.open_positions())

    def total_floating_pnl(self) -> float:
        return self.positions.total_floating_pnl()

    def expected_profit(self) -> float:
        return sum(p.expected_profit or 0.0 for p in self.open_positions())

    def exposure_by_asset(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for position in self.open_positions():
            key = position.symbol or str(position.asset_id)
            out[key] = out.get(key, 0.0) + (position.invest or 0.0)
        return out

    def exposure_by_instrument(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for position in self.open_positions():
            key = position.instrument_type.value
            out[key] = out.get(key, 0.0) + (position.invest or 0.0)
        return out

    def summary(self) -> Dict[str, Any]:
        stats = self.local_stats()
        return {
            "balance_id": self.accounts.active_balance_id,
            "open_positions": stats.total_positions,
            "total_invest": stats.total_invest,
            "floating_pnl": stats.pnl,
            "expected_profit": stats.expected_profit,
            "by_instrument": stats.by_instrument,
            "by_asset": self.exposure_by_asset(),
        }

    # ==================================================================
    # Closing
    # ==================================================================
    def close_position(self, position_id: int) -> bool:
        return self.positions.close(position_id)

    def close_all(self, *, instrument_type: Optional[InstrumentType] = None) -> int:
        return self.positions.close_all(instrument_type=instrument_type)

    def closed_trades(self) -> List[Trade]:
        return [Trade.from_position(p) for p in self.closed_positions()]

    # ==================================================================
    # Helpers
    # ==================================================================
    @staticmethod
    def _wire(instrument_type: InstrumentType) -> str:
        return {
            InstrumentType.BINARY: "binary-option",
            InstrumentType.TURBO: "turbo-option",
            InstrumentType.DIGITAL: "digital-option",
            InstrumentType.BLITZ: "blitz-option",
            InstrumentType.FOREX: "marginal-forex",
            InstrumentType.CFD: "marginal-cfd",
            InstrumentType.CRYPTO: "marginal-crypto",
        }.get(instrument_type, instrument_type.value)

    def _parse_stats(self, payload: Any) -> PortfolioStats:
        if not isinstance(payload, dict):
            raise ProtocolError("unexpected portfolio.get-stats payload",
                                details={"payload": payload})
        items = payload.get("items") or payload.get("stats") or []
        if isinstance(items, dict):
            items = [items]

        by_instrument: Dict[str, Dict[str, float]] = {}
        total = count = invest = expected = sell = pnl = 0.0
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("instrument_type", "unknown"))
            c = float(item.get("count", 0) or 0)
            inv = float(item.get("invest", item.get("total_invest", 0)) or 0)
            pr = float(item.get("pnl", item.get("total_pnl", 0)) or 0)
            exp = float(item.get("expected_profit", 0) or 0)
            sp = float(item.get("sell_profit", 0) or 0)
            by_instrument[key] = {"count": c, "invest": inv, "pnl": pr}
            count += c
            invest += inv
            pnl += pr
            expected += exp
            sell += sp
        total = float(payload.get("total", count) or count)

        return PortfolioStats(
            total_positions=int(total),
            total_invest=invest,
            expected_profit=expected,
            sell_profit=sell,
            pnl=pnl,
            by_instrument=by_instrument,
            raw=payload,
        )
