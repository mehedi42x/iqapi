#!/usr/bin/env python3
"""Live trader.

Reads ``userbot/.env``, loads the chosen strategy module, and lets
``core.py`` do every API call / risk check / order.  This file is the
operator console: banner, instructions, the loop, and a clean shutdown.

    python bot.py
    python bot.py --dry-run
    python bot.py --strategy digital_ai --asset XAUUSD
    python -m userbot
"""

from __future__ import annotations

import argparse
import signal as os_signal
import sys
import time
from typing import Optional

try:
    from core import (
        ENV_PATH,
        EnvConfig,
        Interrupted,
        UserBotCore,
        format_money,
        format_tf,
        list_strategies,
        setup_logging,
    )
except ImportError:  # python -m userbot.bot from the repo root
    from userbot.core import (
        ENV_PATH,
        EnvConfig,
        Interrupted,
        UserBotCore,
        format_money,
        format_tf,
        list_strategies,
        setup_logging,
    )


BANNER = r"""
╔══════════════════════════════════════════════════════════════════╗
║                  IQ OPTION  USERBOT                              ║
║          strategies generate signals · core executes             ║
╚══════════════════════════════════════════════════════════════════╝
"""

INSTRUCTIONS = """
  Configure     edit  userbot/.env
  Live trade    python bot.py
  Paper / dry   python bot.py --dry-run          (or DRY_RUN=true)
  Backtest      python backtest.py
  Custom strat  drop a .py in userbot/strategies/  (see README)
  Stop          Ctrl+C   — the engine never wedges, it always unwinds

  .env knobs you will actually touch
    IQ_EMAIL / IQ_PASSWORD     login
    IQ_ACCOUNT_MODE            PRACTICE | REAL   (+ IQ_ALLOW_REAL=true)
    ASSET                      EURUSD  XAUUSD  EURUSD-OTC  ...
    TIMEFRAME                  1m  5m  15m
    TRADE_TYPE                 binary | digital | blitz
    DURATION                   minutes (binary/digital) / seconds (blitz)
    STRATEGY                   auto | binary1 | digital_ai | gold_impulse | path.py
    AMOUNT / MM_MODE           fixed | percent | martingale
    MIN_CONFIDENCE / MIN_PAYOUT
    MAX_DAILY_LOSS / MAX_TRADES / MAX_CONSECUTIVE_LOSSES
"""


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


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bot.py",
        description="IQ Option userbot — modular strategies, core-executed.",
    )
    p.add_argument("--env", default=str(ENV_PATH), help="path to .env")
    p.add_argument("--strategy", help="override STRATEGY from .env")
    p.add_argument("--asset", help="override ASSET from .env")
    p.add_argument("--amount", type=float, help="override AMOUNT")
    p.add_argument("--dry-run", action="store_true", help="signals only, no orders")
    p.add_argument("--list", action="store_true", dest="list_only",
                   help="print installed strategies and exit")
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    return p.parse_args(argv)


def print_catalog() -> None:
    print(bold("Installed strategy modules"))
    print(f"{'name':<18} {'tf':<6} {'type':<10} description")
    print("-" * 78)
    for row in list_strategies():
        print(f"{row['name']:<18} {format_tf(int(row['timeframe'])):<6} "
              f"{str(row['instrument']):<10} {row['description']}")


def print_banner(cfg: EnvConfig) -> None:
    print(cyan(BANNER))
    print(INSTRUCTIONS)
    s = cfg.summary()
    rows = [
        ("account", f"{s['account']}" + ("  [DRY RUN]" if s["dry_run"] else "")),
        ("asset", s["asset"]),
        ("timeframe", s["timeframe"]),
        ("trade type", f"{s['trade_type']}" + ("  turbo" if s["turbo"] else "")),
        ("expiry", str(s["duration"])),
        ("strategy", s["strategy"]),
        ("amount / mm", f"{s['amount']}   ({s['mm_mode']})"),
        ("min confidence", f"{s['min_confidence']:.2f}"),
        ("min payout", f"{s['min_payout']:.0f}%"),
        ("env file", str(cfg.source)),
    ]
    print(bold("  Session"))
    for key, value in rows:
        print(f"    {key:<16} {value}")
    print()


def _describe_cycle(cycle: dict) -> str:
    signal = cycle.get("signal")
    if cycle.get("skipped"):
        reason = cycle.get("reason") or (signal.reason if signal else "")
        if signal and signal.tradable:
            return yel(f"SKIP  {signal.action.upper()} {signal.confidence:.2f}  {reason}")
        return f"hold  {reason}"
    if cycle.get("dry_run"):
        return cyan(
            f"DRY   {signal.action.upper()} {cycle.get('asset')}  "
            f"${cycle.get('amount')}  conf={signal.confidence:.2f}  {signal.reason}"
        )
    settlement = cycle.get("settlement") or {}
    tag = settlement.get("result", "?")
    pnl = settlement.get("pnl", 0.0)
    colour = green if tag == "win" else red if tag == "loss" else yel
    return colour(
        f"{tag.upper():<5} {signal.action.upper()} {cycle.get('asset')}  "
        f"${cycle.get('amount')}  pnl={pnl:+.2f}  {signal.reason}"
    )


def _print_summary(core: UserBotCore) -> None:
    snap = core.risk.snapshot()
    cur = core.currency()
    print()
    print(bold("  Session summary"))
    print(f"    trades      {snap['trades']}   "
          f"(W {snap['wins']} / L {snap['losses']} / = {snap['equals']})")
    print(f"    win rate    {snap['win_rate']:.1f}%")
    print(f"    pnl         {format_money(snap['pnl'], cur)}")
    if snap["stop_reason"]:
        print(f"    stopped     {snap['stop_reason']}")
    try:
        print(f"    balance     {format_money(core.balance(), cur)}")
    except Exception:
        pass
    print()


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    if args.list_only:
        print_catalog()
        return 0

    cfg = EnvConfig.load(args.env)
    if args.strategy:
        cfg.strategy = args.strategy
    if args.asset:
        cfg.asset = args.asset.upper()
    if args.amount is not None:
        cfg.amount = float(args.amount)
    if args.dry_run:
        cfg.dry_run = True

    log = setup_logging(cfg.log_level)
    print_banner(cfg)

    try:
        cfg.validate_credentials()
    except Exception as exc:
        print(red(f"  config: {exc}"))
        print(f"  edit {cfg.source} and run again.\n")
        return 2

    core = UserBotCore(cfg, logger=log)

    def _shutdown(signum, _frame):
        log.info("signal %s — stopping", signum)
        core.stop()

    os_signal.signal(os_signal.SIGINT, _shutdown)
    os_signal.signal(os_signal.SIGTERM, _shutdown)

    try:
        print(yel("  connecting..."))
        core.connect()
        core.load_strategy()
        print(green(
            f"  online  {core.account_type()}  "
            f"{format_money(core.balance(), core.currency())}  "
            f"strategy={core.strategy.name}"
        ))
        print()
    except Exception as exc:
        print(red(f"  connect failed: {exc}"))
        core.disconnect()
        return 1

    exit_code = 0
    try:
        while not core.stopped():
            try:
                cycle = core.run_once()
            except Interrupted:
                break
            except Exception as exc:
                log.exception("cycle error (will keep going): %s", exc)
                print(red(f"  cycle error: {exc}"))
                try:
                    core.interruptible_sleep(core.cfg.reconnect_delay)
                except Interrupted:
                    break
                continue

            line = _describe_cycle(cycle)
            stamp = time.strftime("%H:%M:%S")
            print(f"  {stamp}  {line}")

            if cycle.get("stop"):
                print(yel(f"  risk halt: {cycle.get('reason')}"))
                break
            if args.once:
                break
    except KeyboardInterrupt:
        core.stop()
    except Exception as exc:
        log.exception("fatal: %s", exc)
        print(red(f"  fatal: {exc}"))
        exit_code = 1
    finally:
        _print_summary(core)
        core.disconnect()
        print("  disconnected.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
