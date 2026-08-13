"""Trade history for every instrument type.

Sources used, in order of preference:

* ``portfolio.get-history-positions``  - unified closed-position history
* ``get-user-profile-client`` fallbacks are *not* used; if the microservice is
  unavailable the locally tracked closed positions are returned so the caller
  always gets a usable :class:`~iq_option_api.models.History`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..account import AccountManager
from ..connection.protocol import MS_PORTFOLIO_HISTORY
from ..connection.websocket import WebSocketClient
from ..models import History, InstrumentType, Position, Trade
from ..trading.positions import PositionManager

WIRE_TYPES: Dict[InstrumentType, str] = {
    InstrumentType.BINARY: "binary-option",
    InstrumentType.TURBO: "turbo-option",
    InstrumentType.DIGITAL: "digital-option",
    InstrumentType.BLITZ: "blitz-option",
    InstrumentType.FOREX: "marginal-forex",
    InstrumentType.CFD: "marginal-cfd",
    InstrumentType.CRYPTO: "marginal-crypto",
    InstrumentType.STOCK: "marginal-cfd",
    InstrumentType.COMMODITY: "marginal-cfd",
    InstrumentType.ETF: "marginal-cfd",
    InstrumentType.INDEX: "marginal-cfd",
}

ALL_TYPES = list(dict.fromkeys(WIRE_TYPES.values()))


class HistoryManager:
    """Closed-trade history, per instrument type or unified."""

    def __init__(self, client: WebSocketClient, accounts: AccountManager,
                 positions: Optional[PositionManager] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.accounts = accounts
        self.positions = positions
        self.log = logger or logging.getLogger("iq_option_api.history")

    # ==================================================================
    # Generic query
    # ==================================================================
    def get_history(self, *, instrument_types: Optional[List[InstrumentType]] = None,
                    limit: int = 50, offset: int = 0,
                    start: Optional[float] = None, end: Optional[float] = None,
                    timeout: Optional[float] = None) -> History:
        wire = ([WIRE_TYPES.get(t, t.value) for t in instrument_types]
                if instrument_types else list(ALL_TYPES))
        body: Dict[str, Any] = {
            "user_id": self.accounts.user_id,
            "user_balance_id": self.accounts.active_balance_id,
            "instrument_types": list(dict.fromkeys(wire)),
            "limit": int(limit),
            "offset": int(offset),
        }
        if start is not None:
            body["start"] = int(start)
        if end is not None:
            body["end"] = int(end)

        try:
            payload = self.ws.call(MS_PORTFOLIO_HISTORY, body, version="2.0",
                                   timeout=timeout)
        except Exception as exc:
            self.log.debug("%s failed (%s) - falling back to local history",
                           MS_PORTFOLIO_HISTORY, exc)
            return self._local_history(instrument_types, limit, offset)

        trades = self._parse(payload)
        itype = instrument_types[0] if instrument_types and len(instrument_types) == 1 \
            else InstrumentType.UNKNOWN
        total = None
        if isinstance(payload, dict):
            total = payload.get("total") or payload.get("count")
        return History(trades=trades, instrument_type=itype, limit=limit,
                       offset=offset, total=total,
                       raw=payload if isinstance(payload, dict) else {})

    # ==================================================================
    # Per instrument helpers
    # ==================================================================
    def binary_history(self, limit: int = 50, **kw: Any) -> History:
        return self.get_history(instrument_types=[InstrumentType.BINARY,
                                                  InstrumentType.TURBO],
                                limit=limit, **kw)

    def digital_history(self, limit: int = 50, **kw: Any) -> History:
        return self.get_history(instrument_types=[InstrumentType.DIGITAL],
                                limit=limit, **kw)

    def blitz_history(self, limit: int = 50, **kw: Any) -> History:
        return self.get_history(instrument_types=[InstrumentType.BLITZ],
                                limit=limit, **kw)

    def forex_history(self, limit: int = 50, **kw: Any) -> History:
        return self.get_history(instrument_types=[InstrumentType.FOREX],
                                limit=limit, **kw)

    def cfd_history(self, limit: int = 50, **kw: Any) -> History:
        return self.get_history(instrument_types=[InstrumentType.CFD],
                                limit=limit, **kw)

    def crypto_history(self, limit: int = 50, **kw: Any) -> History:
        return self.get_history(instrument_types=[InstrumentType.CRYPTO],
                                limit=limit, **kw)

    stocks_history = cfd_history
    commodities_history = cfd_history
    etf_history = cfd_history
    indices_history = cfd_history

    # ==================================================================
    # Filters / analytics
    # ==================================================================
    def by_asset(self, asset_id: int, *, limit: int = 100) -> List[Trade]:
        return [t for t in self.get_history(limit=limit) if t.asset_id == int(asset_id)]

    def by_date_range(self, start: float, end: float, *, limit: int = 200) -> History:
        return self.get_history(limit=limit, start=start, end=end)

    def last_trades(self, count: int = 10) -> List[Trade]:
        history = self.get_history(limit=count)
        return sorted(history.trades, key=lambda t: t.close_time or 0, reverse=True)[:count]

    def statistics(self, *, limit: int = 200) -> Dict[str, Any]:
        history = self.get_history(limit=limit)
        wins = [t for t in history.trades if t.result == "win"]
        losses = [t for t in history.trades if t.result == "loss"]
        return {
            "trades": len(history),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": history.win_rate,
            "total_invest": history.total_invest,
            "realized_pnl": history.realized_pnl,
            "best": max((t.pnl or 0.0 for t in history.trades), default=0.0),
            "worst": min((t.pnl or 0.0 for t in history.trades), default=0.0),
        }

    # ==================================================================
    # Internals
    # ==================================================================
    def _parse(self, payload: Any) -> List[Trade]:
        items: List[Any] = []
        if isinstance(payload, dict):
            for key in ("positions", "items", "data", "history"):
                value = payload.get(key)
                if isinstance(value, list):
                    items = value
                    break
        elif isinstance(payload, list):
            items = payload

        trades: List[Trade] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            trades.append(Trade.from_position(Position.from_payload(item)))
        return trades

    def _local_history(self, instrument_types: Optional[List[InstrumentType]],
                       limit: int, offset: int) -> History:
        if self.positions is None:
            return History(limit=limit, offset=offset)
        closed = self.positions.closed_positions()
        if instrument_types:
            wanted = set(instrument_types)
            closed = [p for p in closed if p.instrument_type in wanted]
        closed.sort(key=lambda p: p.close_time or 0, reverse=True)
        window = closed[offset:offset + limit]
        return History(trades=[Trade.from_position(p) for p in window],
                       instrument_type=(instrument_types[0] if instrument_types
                                        and len(instrument_types) == 1
                                        else InstrumentType.UNKNOWN),
                       limit=limit, offset=offset, total=len(closed))
