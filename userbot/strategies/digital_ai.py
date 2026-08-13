"""Digital AI — adaptive multi-factor ensemble.

Not a black-box neural net (those need labelled data you do not have at
first boot).  This is a *real* online learner:

1. Extract a 14-dimensional, normalised feature vector from the tape.
2. Detect the regime (trend vs. range) via ADX + BB-width percentile.
3. Score = dot(weights[regime], features).
4. After every settled trade ``on_result`` nudges the weights toward
   (win) or away from (loss) the feature vector that produced it.

Weights persist in ``userbot/data/ai_weights.json`` so the module gets
sharper the longer you run it.  Works on any asset; shines on 1-minute
digital because the feedback loop is fast.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .base import Strategy, Signal
from . import indicators as ta


_WEIGHT_FILE = Path(__file__).resolve().parent.parent / "data" / "ai_weights.json"

# Hand-seeded priors — overwritten as the learner sees live results.
_PRIOR_TREND = [
    0.90,   # rsi location
    1.20,   # macd hist
    1.10,   # price vs ema21
    0.95,   # price vs ema50
    0.40,   # %b (less important in trend)
    0.55,   # stochastic
    1.30,   # adx * di direction
    0.85,   # last candle body
    1.00,   # 3-bar momentum
    0.45,   # volume impulse
    1.15,   # htf bias
    0.70,   # atr expansion
    0.60,   # cci
    0.50,   # williams %R
]
_PRIOR_RANGE = [
    -0.85,  # fade rsi extremes
    -0.40,  # fade macd in range
    -0.55,  # fade ema extension
    -0.35,
    1.25,   # %b mean-reversion
    0.90,   # stoch extremes
    0.20,   # adx weak
    0.70,   # rejection candle
    -0.30,
    0.25,
    0.40,   # still respect htf a little
    -0.20,
    0.95,   # cci fade
    0.85,   # williams fade
]

_FEATURE_NAMES = (
    "rsi", "macd_hist", "ema21", "ema50", "pct_b", "stoch", "adx_di",
    "body", "mom3", "volume", "htf", "atr_exp", "cci", "willr",
)


def _tanh(x: float, scale: float = 1.0) -> float:
    try:
        return math.tanh(float(x) / max(1e-9, scale))
    except (TypeError, ValueError):
        return 0.0


class DigitalAI(Strategy):
    name = "digital_ai"
    description = "Digital AI ensemble — 14 features, regime switch, online weight update"
    instrument = "digital"
    timeframe = 60
    min_candles = 120
    tags = ("digital", "1m", "ai", "ensemble")

    lr = 0.035          # learning rate
    decay = 0.002       # pull weights back toward the prior
    threshold = 0.34    # |score| must clear this
    max_weight = 2.4

    def __init__(self) -> None:
        super().__init__()
        self.weights_trend: List[float] = list(_PRIOR_TREND)
        self.weights_range: List[float] = list(_PRIOR_RANGE)
        self._last_features: Optional[List[float]] = None
        self._last_regime: str = "trend"
        self._trades_seen = 0
        self._prev_score: Optional[float] = None
        self._load()

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._last_features = None
        self._prev_score = None

    def analyze(self, candles: Sequence[Any], context: Dict[str, Any]) -> Signal:
        features, regime, debug = self._features(candles, context)
        if features is None:
            return Signal.hold(debug or "feature warmup")

        weights = self.weights_trend if regime == "trend" else self.weights_range
        score = sum(w * f for w, f in zip(weights, features))
        self._last_features = list(features)
        self._last_regime = regime
        prev = self._prev_score
        self._prev_score = score

        if abs(score) < self.threshold:
            return Signal.hold(f"ai {regime} score={score:+.3f} < {self.threshold}")

        # Edge trigger: fire on a fresh cross / flip, not on every bar the
        # score stays elevated (that would overtrade a clean trend).
        same_side = prev is not None and abs(prev) >= self.threshold and (prev >= 0) == (score >= 0)
        if same_side:
            return Signal.hold(f"ai {regime} holding {score:+.2f}")

        conf = ta.score_to_confidence(abs(score), floor=0.60, ceiling=0.94, scale=1.6)
        # slightly more conservative while the learner is still cold
        if self._trades_seen < 12:
            conf *= 0.92
        top = sorted(
            ((n, w * f) for n, w, f in zip(_FEATURE_NAMES, weights, features)),
            key=lambda kv: abs(kv[1]),
            reverse=True,
        )[:3]
        why = f"ai-{regime} score={score:+.2f} " + " ".join(f"{n}{v:+.2f}" for n, v in top)
        if score > 0:
            return Signal.call(conf, why, score=score, regime=regime, features=features)
        return Signal.put(conf, why, score=score, regime=regime, features=features)

    def on_result(self, signal: Signal, result: str, pnl: float,
                  context: Optional[Dict[str, Any]] = None) -> None:
        feats = signal.meta.get("features") if signal and signal.meta else None
        if feats is None:
            feats = self._last_features
        if not feats or result not in {"win", "loss"}:
            return
        regime = (signal.meta or {}).get("regime", self._last_regime)
        weights = self.weights_trend if regime == "trend" else self.weights_range
        prior = _PRIOR_TREND if regime == "trend" else _PRIOR_RANGE

        # The feature vector already points toward the side we took
        # (positive = call).  A win reinforces it; a loss flips the update.
        side = 1.0 if signal.action == "call" else -1.0
        outcome = 1.0 if result == "win" else -1.0
        # magnitude: bigger |pnl| / amount → slightly stronger update
        mag = 1.0
        if context and context.get("amount"):
            try:
                mag = min(2.0, 0.6 + abs(float(pnl)) / float(context["amount"]))
            except (TypeError, ValueError, ZeroDivisionError):
                mag = 1.0

        for i, feat in enumerate(feats):
            update = self.lr * outcome * side * float(feat) * mag
            # L2 pull toward the prior so one bad night cannot wreck the model
            update += -self.decay * (weights[i] - prior[i])
            weights[i] = max(-self.max_weight, min(self.max_weight, weights[i] + update))

        self._trades_seen += 1
        # never persist a backtest walk-forward over the live weight file
        if not (context or {}).get("backtest"):
            self._save()

    # ------------------------------------------------------------------
    def _features(self, candles: Sequence[Any],
                  context: Dict[str, Any]) -> tuple:
        c = ta.closes(candles)
        h = ta.highs(candles)
        l = ta.lows(candles)
        bar = candles[-1]
        if len(c) < self.min_candles:
            return None, "trend", "warmup"

        rsi0 = ta.last(ta.rsi(c, 14))
        _m, _s, hist = ta.macd(c, 12, 26, 9)
        h0 = ta.last(hist)
        e21 = ta.last(ta.ema(c, 21))
        e50 = ta.last(ta.ema(c, 50))
        pb = ta.last(ta.percent_b(c, 20, 2.0))
        k_line, _ = ta.stochastic(h, l, c, 14, 3)
        k0 = ta.last(k_line)
        adx_line, pdi, mdi = ta.adx(h, l, c, 14)
        adx0, p0, m0 = ta.last(adx_line), ta.last(pdi), ta.last(mdi)
        atr_line = ta.atr(h, l, c, 14)
        atr0, atr5 = ta.last(atr_line), ta.last(atr_line, 5)
        cci0 = ta.last(ta.cci(candles, 20))
        wr0 = ta.last(ta.williams_r(h, l, c, 14))
        if None in (rsi0, h0, e21, e50, pb, k0, adx0, p0, m0, atr0, cci0, wr0):
            return None, "trend", "indicator none"

        price = c[-1]
        vol = ta.volumes(candles)
        vol_ratio = 0.0
        if ta.has_volume(candles) and len(vol) >= 20:
            avg = sum(vol[-20:]) / 20.0
            if avg > 0:
                vol_ratio = (vol[-1] - avg) / avg

        htf = float(ta.htf_bias(context.get("htf_candles") or []))
        mom3 = 0.0 if c[-4] == 0 else (c[-1] - c[-4]) / abs(c[-4])
        body = ta.body(bar) / max(ta.range_(bar), 1e-12)
        body *= 1.0 if ta.is_bull(bar) else -1.0
        atr_exp = 0.0 if not atr5 else (atr0 - atr5) / max(atr5, 1e-12)
        di = (p0 - m0) / 100.0

        feats = [
            _tanh((rsi0 - 50.0) / 25.0),
            _tanh(h0, scale=max(atr0, 1e-9) * 0.6),
            _tanh((price - e21) / max(atr0, 1e-9)),
            _tanh((price - e50) / max(atr0, 1e-9)),
            _tanh((pb - 0.5) * 2.0),
            _tanh((k0 - 50.0) / 30.0),
            _tanh(di * (adx0 / 25.0)),
            _tanh(body * 1.4),
            _tanh(mom3, scale=0.004),
            _tanh(vol_ratio),
            htf,                                      # already -1/0/+1
            _tanh(atr_exp, scale=0.35),
            _tanh(cci0 / 150.0),
            _tanh((wr0 + 50.0) / 35.0),
        ]

        width = ta.bb_width(c, 20, 2.0)
        w_rank = ta.percentile_rank(width, 50)
        # trend regime if ADX is alive *or* bands are expanding
        if (adx0 is not None and adx0 >= 22) or (w_rank is not None and w_rank >= 0.65):
            regime = "trend"
        else:
            regime = "range"
        return feats, regime, ""

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if not _WEIGHT_FILE.exists():
                return
            raw = json.loads(_WEIGHT_FILE.read_text(encoding="utf-8"))
            if isinstance(raw.get("trend"), list) and len(raw["trend"]) == len(_PRIOR_TREND):
                self.weights_trend = [float(x) for x in raw["trend"]]
            if isinstance(raw.get("range"), list) and len(raw["range"]) == len(_PRIOR_RANGE):
                self.weights_range = [float(x) for x in raw["range"]]
            self._trades_seen = int(raw.get("trades", 0) or 0)
        except Exception:
            # corrupt file must never stop the bot
            pass

    def _save(self) -> None:
        try:
            _WEIGHT_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _WEIGHT_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "trend": self.weights_trend,
                "range": self.weights_range,
                "trades": self._trades_seen,
                "features": list(_FEATURE_NAMES),
            }, indent=2), encoding="utf-8")
            tmp.replace(_WEIGHT_FILE)
        except Exception:
            pass
