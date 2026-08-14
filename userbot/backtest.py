#!/usr/bin/env python3
"""Walk-forward backtester.

Asks for a history window (1 day / 1 week / 1 month / 6 months / 1 year),
pulls candles through :class:`UserBotCore` (same code path the live bot
uses), then replays every closed bar against the chosen strategy.

No orders are sent.  Expiry is simulated as the close ``DURATION`` bars
later versus the signal bar's close — the same rule a 1-minute binary /
digital option follows.

    python backtest.py
    python backtest.py --range 1w --strategy digital_ai
    python backtest.py --range 1m --asset XAUUSD --payout 82
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from core import (
        DATA_DIR,
        ENV_PATH,
        EnvConfig,
        Interrupted,
        UserBotCore,
        format_money,
        format_tf,
        list_strategies,
        setup_logging,
    )
except ImportError:  # python -m userbot.backtest from the repo root
    from userbot.core import (
        DATA_DIR,
        ENV_PATH,
        EnvConfig,
        Interrupted,
        UserBotCore,
        format_money,
        format_tf,
        list_strategies,
        setup_logging,
    )


RANGES = {
    "1d": ("1 day", 1 * 86400),
    "1w": ("1 week", 7 * 86400),
    "1m": ("1 month", 30 * 86400),
    "6m": ("6 months", 182 * 86400),
    "1y": ("1 year", 365 * 86400),
}


def _paint(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t: str) -> str:
    return _paint(t, "32")


def red(t: str) -> str:
    return _paint(t, "31")


def yel(t: str) -> str:
    return _paint(t, "33")


def cyan(t: str) -> str:
    return _paint(t, "36")


def bold(t: str) -> str:
    return _paint(t, "1")


@dataclass
class Fill:
    index: int
    time: float
    action: str
    confidence: float
    reason: str
    entry: float
    exit: float
    result: str
    pnl: float
    amount: float


@dataclass
class Report:
    strategy: str
    asset: str
    timeframe: int
    trade_type: str
    duration_bars: int
    payout: float
    range_key: str
    candles: int
    fills: List[Fill] = field(default_factory=list)
    equity: List[float] = field(default_factory=list)
    skipped: int = 0
    elapsed: float = 0.0

    @property
    def wins(self) -> int:
        return sum(1 for f in self.fills if f.result == "win")

    @property
    def losses(self) -> int:
        return sum(1 for f in self.fills if f.result == "loss")

    @property
    def equals(self) -> int:
        return sum(1 for f in self.fills if f.result == "equal")

    @property
    def pnl(self) -> float:
        return sum(f.pnl for f in self.fills)

    @property
    def invested(self) -> float:
        return sum(f.amount for f in self.fills)

    @property
    def win_rate(self) -> float:
        settled = self.wins + self.losses
        return 0.0 if not settled else 100.0 * self.wins / settled

    @property
    def profit_factor(self) -> float:
        gp = sum(f.pnl for f in self.fills if f.pnl > 0)
        gl = -sum(f.pnl for f in self.fills if f.pnl < 0)
        if gl <= 0:
            return float("inf") if gp > 0 else 0.0
        return gp / gl

    @property
    def max_drawdown(self) -> float:
        peak = 0.0
        dd = 0.0
        equity = 0.0
        for fill in self.fills:
            equity += fill.pnl
            peak = max(peak, equity)
            dd = min(dd, equity - peak)
        return dd

    @property
    def max_consec_loss(self) -> int:
        best = run = 0
        for fill in self.fills:
            if fill.result == "loss":
                run += 1
                best = max(best, run)
            else:
                run = 0
        return best

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "asset": self.asset,
            "timeframe": self.timeframe,
            "trade_type": self.trade_type,
            "duration_bars": self.duration_bars,
            "payout": self.payout,
            "range": self.range_key,
            "candles": self.candles,
            "trades": len(self.fills),
            "wins": self.wins,
            "losses": self.losses,
            "equals": self.equals,
            "win_rate": round(self.win_rate, 2),
            "pnl": round(self.pnl, 2),
            "invested": round(self.invested, 2),
            "profit_factor": None if self.profit_factor == float("inf")
            else round(self.profit_factor, 3),
            "max_drawdown": round(self.max_drawdown, 2),
            "max_consecutive_losses": self.max_consec_loss,
            "skipped": self.skipped,
            "elapsed_sec": round(self.elapsed, 1),
        }


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="backtest.py",
                                description="Backtest any userbot strategy.")
    p.add_argument("--env", default=str(ENV_PATH))
    p.add_argument("--range", choices=list(RANGES),
                   help="history window (skip the interactive prompt)")
    p.add_argument("--strategy", help="override STRATEGY")
    p.add_argument("--asset", help="override ASSET")
    p.add_argument("--payout", type=float, help="override PAYOUT_PERCENT")
    p.add_argument("--amount", type=float, help="override AMOUNT")
    p.add_argument("--list", action="store_true", dest="list_only")
    p.add_argument("--save", action="store_true", help="write JSON report to data/")
    return p.parse_args(argv)


def ask_range() -> str:
    print()
    print(bold(" range"))
    keys = list(RANGES)
    for i, key in enumerate(keys, 1):
        label, _ = RANGES[key]
        print(f"  {i}) {key:<3} {label}")
    while True:
        try:
            raw = input(" > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(0)
        if not raw:
            return "1w"
        if raw in RANGES:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        print(yel(" pick 1d / 1w / 1m / 6m / 1y"))


def duration_bars(cfg: EnvConfig) -> int:
    """How many *timeframe* bars equal one option expiry."""
    expiry = cfg.duration_seconds()
    bars = max(1, int(round(expiry / max(1, cfg.timeframe))))
    return bars


def simulate(core: UserBotCore, candles: List[Any], *,
             payout: float, amount: float, range_key: str) -> Report:
    strategy = core.strategy
    assert strategy is not None
    bars = duration_bars(core.cfg)
    lookback = max(int(strategy.min_candles), 40)
    # Never feed the whole history into analyze() — indicators are O(window)
    # and a 1-year 1m tape is ~500k bars.  A fixed lookback matches live.
    window_cap = max(lookback + 40, 220)
    report = Report(
        strategy=strategy.name,
        asset=core.live_asset or core.cfg.asset,
        timeframe=core.cfg.timeframe,
        trade_type=core.cfg.trade_type,
        duration_bars=bars,
        payout=payout,
        range_key=range_key,
        candles=len(candles),
    )
    htf_size = 5
    started = time.time()
    last_draw = 0.0
    total = max(1, len(candles) - lookback - bars)

    strategy.reset()
    for end in range(lookback, len(candles) - bars):
        if core.stopped():
            raise Interrupted("stop requested")
        window = candles[max(0, end + 1 - window_cap):end + 1]
        # cheap synthetic HTF: every 5th closed bar of the same window
        htf = window[::htf_size] if len(window) >= htf_size * 8 else []
        context = {
            "asset": report.asset,
            "timeframe": core.cfg.timeframe,
            "server_time": getattr(window[-1], "to_ts", None) or getattr(window[-1], "from_ts", 0),
            "htf_candles": htf,
            "payout": payout,
            "instrument": core.cfg.trade_type,
            "price": window[-1].close,
            "dry_run": True,
            "duration": core.cfg.duration,
            "backtest": True,
        }
        signal = strategy.safe_analyze(window, context)
        if not signal.tradable or signal.confidence < core.cfg.min_confidence:
            report.skipped += 1
            continue

        entry = float(window[-1].close)
        exit_px = float(candles[end + bars].close)
        if signal.action == "call":
            won = exit_px > entry
            lost = exit_px < entry
        else:
            won = exit_px < entry
            lost = exit_px > entry
        if won:
            tag, pnl = "win", amount * payout / 100.0
        elif lost:
            tag, pnl = "loss", -amount
        else:
            tag, pnl = "equal", 0.0

        fill = Fill(
            index=end,
            time=float(getattr(window[-1], "from_ts", 0.0) or 0.0),
            action=signal.action,
            confidence=signal.confidence,
            reason=signal.reason,
            entry=entry,
            exit=exit_px,
            result=tag,
            pnl=pnl,
            amount=amount,
        )
        report.fills.append(fill)
        try:
            strategy.on_result(signal, tag, pnl, {"amount": amount, "backtest": True})
        except Exception:
            pass

        now = time.time()
        if now - last_draw >= 0.25:
            last_draw = now
            done = end - lookback
            pct = 100.0 * done / total
            sys.stdout.write(
                f"\r {pct:5.1f}%  n={len(report.fills)}  "
                f"pnl={report.pnl:+.2f}  wr={report.win_rate:.1f}%  "
            )
            sys.stdout.flush()

    report.elapsed = time.time() - started
    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()
    return report


def print_report(report: Report) -> None:
    wr_colour = green if report.win_rate >= 55 else yel if report.win_rate >= 50 else red
    pnl_colour = green if report.pnl > 0 else red if report.pnl < 0 else yel
    pf = "inf" if report.profit_factor == float("inf") else f"{report.profit_factor:.2f}"
    print()
    print(bold(" BACKTEST"))
    print("─" * 40)
    print(f" strategy   {report.strategy}")
    print(f" asset      {report.asset}  {report.trade_type}  {format_tf(report.timeframe)}")
    print(f" window     {RANGES[report.range_key][0]}  {report.candles} candles")
    print(f" payout     {report.payout:.1f}%")
    print(f" trades     {len(report.fills)}  W{report.wins} L{report.losses} ={report.equals}")
    print(f" win rate   {wr_colour(f'{report.win_rate:.2f}%')}")
    print(f" pnl        {pnl_colour(format_money(report.pnl))}")
    print(f" pf / dd    {pf} / {report.max_drawdown:.2f}")
    print(f" elapsed    {report.elapsed:.1f}s")
    if report.fills:
        print("─" * 40)
        for fill in report.fills[-8:]:
            when = (datetime.fromtimestamp(fill.time, tz=timezone.utc).strftime("%m-%d %H:%M")
                    if fill.time else "-")
            colour = green if fill.result == "win" else red if fill.result == "loss" else yel
            print(f" {when} {colour(fill.result.upper()[:4]):<13} "
                  f"{fill.action.upper():<4} {fill.pnl:+.2f}")
    print()


def save_report(report: Report) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = DATA_DIR / f"backtest_{report.strategy}_{report.asset}_{report.range_key}_{stamp}.json"
    payload = report.to_dict()
    payload["fills"] = [
        {
            "time": f.time, "action": f.action, "confidence": f.confidence,
            "result": f.result, "pnl": f.pnl, "entry": f.entry, "exit": f.exit,
            "reason": f.reason,
        }
        for f in report.fills
    ]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    if args.list_only:
        print(bold("Installed strategy modules"))
        for row in list_strategies():
            print(f"  {row['name']:<18} {row['description']}")
        return 0

    cfg = EnvConfig.load(args.env)
    if args.strategy:
        cfg.strategy = args.strategy
    if args.asset:
        cfg.asset = args.asset.upper()
    if args.payout is not None:
        cfg.payout_percent = float(args.payout)
    if args.amount is not None:
        cfg.amount = float(args.amount)
    # backtest never places live orders
    cfg.dry_run = True

    log = setup_logging(cfg.log_level, name="userbot.backtest")
    print(cyan("\n BACKTEST"))
    print("─" * 40)
    print(f" {cfg.resolved_strategy_name()}  {cfg.asset}  "
          f"{format_tf(cfg.timeframe)}  {cfg.trade_type}")

    range_key = args.range or ask_range()
    label, seconds = RANGES[range_key]
    print(yel(f" downloading {label} of candles..."))

    core = UserBotCore(cfg, logger=log)
    try:
        try:
            cfg.validate_credentials()
        except Exception as exc:
            print(red(f"  config: {exc}"))
            return 2
        core.connect()
        core.load_strategy()
        core.live_asset = core.resolve_asset()

        def _progress(have: int, need: int) -> None:
            sys.stdout.write(f"\r  candles  {have}/{need}          ")
            sys.stdout.flush()

        candles = core.fetch_history(asset=core.live_asset, seconds=seconds,
                                     progress=_progress)
        sys.stdout.write("\r" + " " * 60 + "\r")
        if len(candles) < (core.strategy.min_candles + duration_bars(cfg) + 5):
            print(red(f" not enough candles ({len(candles)})"))
            return 1
        print(green(f" {len(candles)} candles  {core.live_asset} {format_tf(cfg.timeframe)}"))
        print(yel(" simulating..."))
        report = simulate(core, candles,
                          payout=cfg.payout_percent,
                          amount=cfg.amount,
                          range_key=range_key)
    except Interrupted:
        print(yel("\n  interrupted."))
        return 130
    except KeyboardInterrupt:
        print(yel("\n  interrupted."))
        return 130
    except Exception as exc:
        log.exception("backtest failed: %s", exc)
        print(red(f"  failed: {exc}"))
        return 1
    finally:
        core.stop()
        core.disconnect()

    print_report(report)
    if args.save or _ask_save():
        path = save_report(report)
        print(f"  saved {path}")
    return 0


def _ask_save() -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        raw = input("  save JSON report to userbot/data/? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return raw in {"y", "yes"}


if __name__ == "__main__":
    raise SystemExit(main())
