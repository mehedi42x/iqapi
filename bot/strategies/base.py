"""Strategy contract.

A strategy is a *pure signal generator*.  It must never place an order, open
a socket, sleep on the network, or touch ``.env``.  ``core.py`` feeds it
candles + a context dict and receives a :class:`Signal` back.

Anyone can drop a new ``*.py`` file in this folder (or point ``STRATEGY=``
at an external path) as long as it subclasses :class:`Strategy` and
implements :meth:`Strategy.analyze`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


CALL = "call"
PUT = "put"
HOLD = "hold"

# Trade types the engine understands.
INSTRUMENTS = ("binary", "turbo", "digital", "blitz")


@dataclass
class Signal:
    """Decision returned by every strategy."""

    action: str = HOLD          # call / put / hold
    confidence: float = 0.0     # 0.0 .. 1.0
    reason: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.action = str(self.action or HOLD).strip().lower()
        if self.action in {"up", "buy", "long", "higher", "1"}:
            self.action = CALL
        elif self.action in {"down", "sell", "short", "lower", "2"}:
            self.action = PUT
        elif self.action not in {CALL, PUT, HOLD}:
            self.action = HOLD
        try:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 0.0
        self.reason = str(self.reason or "")
        if self.meta is None:
            self.meta = {}

    @property
    def tradable(self) -> bool:
        return self.action in {CALL, PUT} and self.confidence > 0.0

    @property
    def direction(self) -> Optional[str]:
        return self.action if self.action in {CALL, PUT} else None

    @classmethod
    def hold(cls, reason: str = "no setup", **meta: Any) -> "Signal":
        return cls(HOLD, 0.0, reason, meta)

    @classmethod
    def call(cls, confidence: float, reason: str, **meta: Any) -> "Signal":
        return cls(CALL, confidence, reason, meta)

    @classmethod
    def put(cls, confidence: float, reason: str, **meta: Any) -> "Signal":
        return cls(PUT, confidence, reason, meta)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"Signal({self.action.upper()} conf={self.confidence:.2f} "
                f"reason={self.reason!r})")


class Strategy:
    """Base class for every signal module.

    Override class attributes and :meth:`analyze`.  Optionally override
    :meth:`on_result` if the strategy wants to learn from settled trades
    (see ``digital_ai``).
    """

    # Unique id used in .env ``STRATEGY=`` (defaults to the module file name).
    name: str = ""
    # One-line human description printed by ``bot.py`` / ``backtest.py``.
    description: str = ""
    # Preferred instrument.  ``any`` means the module works on every type.
    instrument: str = "any"
    # Preferred timeframe in seconds (documentation + auto-picker).
    timeframe: int = 60
    # Preferred symbols.  Empty = any symbol.
    assets: Tuple[str, ...] = ()
    # Minimum closed candles ``analyze`` needs before it can fire.
    min_candles: int = 80
    # Optional tags used by the auto-picker (e.g. "gold", "scalp", "ai").
    tags: Tuple[str, ...] = ()

    def __init__(self) -> None:
        if not self.name:
            self.name = type(self).__name__.lower()

    # ------------------------------------------------------------------
    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        """Return a :class:`Signal` from *closed* candles.

        ``context`` (filled by core, never required) typically contains::

            asset, timeframe, server_time, htf_candles, payout,
            instrument, price, dry_run
        """
        raise NotImplementedError(f"{type(self).__name__}.analyze is not implemented")

    def on_result(self, signal: Signal, result: str, pnl: float,
                  context: Optional[Dict[str, Any]] = None) -> None:
        """Optional hook after a trade settles.  ``result`` is win/loss/equal."""

    def reset(self) -> None:
        """Clear any runtime state (called at the start of a backtest/session)."""

    # ------------------------------------------------------------------
    def supports(self, instrument: str) -> bool:
        wanted = (self.instrument or "any").lower()
        if wanted in {"", "any", "*"}:
            return True
        return wanted == str(instrument).lower()

    def prefers_asset(self, asset: str) -> bool:
        if not self.assets:
            return True
        key = str(asset).upper().replace("/", "").replace("-OTC", "")
        aliases = {a.upper().replace("/", "").replace("-OTC", "") for a in self.assets}
        return key in aliases or "XAUUSD" in aliases and key in {"GOLD", "XAUUSD", "XAU"}

    def ready(self, candles: Sequence[Any]) -> bool:
        return candles is not None and len(candles) >= int(self.min_candles)

    def safe_analyze(self, candles: Sequence[Any],
                     context: Optional[Dict[str, Any]] = None) -> Signal:
        """Never-raising wrapper used by core."""
        ctx = dict(context or {})
        if not self.ready(candles):
            return Signal.hold(
                f"need {self.min_candles} candles, have {len(candles) if candles else 0}")
        try:
            signal = self.analyze(candles, ctx)
        except Exception as exc:  # noqa: BLE001 — strategy bugs must not kill the bot
            return Signal.hold(f"strategy error: {type(exc).__name__}: {exc}")
        if signal is None:
            return Signal.hold("strategy returned nothing")
        if not isinstance(signal, Signal):
            # allow a bare string or (action, confidence, reason) tuple
            signal = _coerce_signal(signal)
        return signal

    def info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "assets": list(self.assets),
            "min_candles": self.min_candles,
            "tags": list(self.tags),
            "class": type(self).__name__,
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


def _coerce_signal(value: Any) -> Signal:
    if isinstance(value, Signal):
        return value
    if isinstance(value, str):
        return Signal(action=value, confidence=0.5, reason="bare string signal")
    if isinstance(value, dict):
        return Signal(
            action=value.get("action", HOLD),
            confidence=value.get("confidence", 0.0),
            reason=value.get("reason", ""),
            meta=value.get("meta") or {},
        )
    if isinstance(value, (list, tuple)) and value:
        action = value[0]
        confidence = value[1] if len(value) > 1 else 0.5
        reason = value[2] if len(value) > 2 else ""
        return Signal(action=action, confidence=confidence, reason=str(reason))
    return Signal.hold(f"unrecognised signal type: {type(value).__name__}")


def closed_candles(candles: Sequence[Any], *, drop_forming: bool = True,
                   now: Optional[float] = None) -> List[Any]:
    """Drop the in-progress bar when ``drop_forming`` is set."""
    if not candles:
        return []
    if not drop_forming:
        return list(candles)
    last = candles[-1]
    to_ts = getattr(last, "to_ts", None) or 0.0
    if now is None:
        import time
        now = time.time()
    if to_ts and float(to_ts) > float(now) + 0.25:
        return list(candles[:-1])
    return list(candles)
