#!/usr/bin/env python3
"""Live trader console.

    python bot.py
    python bot.py --dry-run
    python bot.py --strategy digital_ai --asset XAUUSD
    python -m userbot
"""

from __future__ import annotations

import argparse
import os
import signal as os_signal
import sys
import time
from typing import Optional

try:
    from core import (
        ENV_PATH,
        RUNTIME_DIR,
        EnvConfig,
        Interrupted,
        UserBotCore,
        format_money,
        format_tf,
        init_runtime,
        list_strategies,
        setup_logging,
    )
except ImportError:  # python -m userbot.bot from the repo root
    from userbot.core import (
        ENV_PATH,
        RUNTIME_DIR,
        EnvConfig,
        Interrupted,
        UserBotCore,
        format_money,
        format_tf,
        init_runtime,
        list_strategies,
        setup_logging,
    )

W = 40  # panel width — fits phone terminals


def _tty() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


def green(t: str) -> str: return _c(t, "38;5;42")
def red(t: str) -> str: return _c(t, "38;5;203")
def yellow(t: str) -> str: return _c(t, "38;5;220")
def cyan(t: str) -> str: return _c(t, "38;5;51")
def dim(t: str) -> str: return _c(t, "2")
def bold(t: str) -> str: return _c(t, "1")


def rule(char: str = "─") -> str:
    return dim(char * W)


def header(title: str) -> None:
    print()
    print(cyan(bold(f" {title}")))
    print(rule())


def row(key: str, value: str) -> None:
    print(f" {dim(key.ljust(12))} {value}")


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bot", description="IQ Option userbot")
    p.add_argument("--env", default=str(ENV_PATH), help="path to .env")
    p.add_argument("--init", action="store_true",
                   help="scaffold the writable runtime dir (.env, logs, data) and exit")
    p.add_argument("--strategy", help="override STRATEGY")
    p.add_argument("--asset", help="override ASSET")
    p.add_argument("--amount", type=float, help="override AMOUNT")
    p.add_argument("--dry-run", action="store_true", help="signals only, no orders")
    p.add_argument("--list", action="store_true", dest="list_only", help="list strategies")
    p.add_argument("--once", action="store_true", help="single cycle")
    return p.parse_args(argv)


def print_catalog() -> None:
    header("STRATEGIES")
    for item in list_strategies():
        tf = format_tf(int(item["timeframe"]))
        print(f" {bold(item['name'].ljust(15))} {dim(tf.ljust(4))} {dim(str(item['instrument']))}")
        print(f"   {dim(item['description'])}")
    print()


def print_banner(cfg: EnvConfig) -> None:
    s = cfg.summary()
    mode = s["account"] + ("  DRY" if s["dry_run"] else "")
    mode = yellow(mode) if s["dry_run"] or s["account"] != "REAL" else red(mode)
    header("IQ USERBOT")
    row("account", mode)
    row("asset", bold(s["asset"]))
    row("timeframe", s["timeframe"])
    row("type", s["trade_type"] + (" turbo" if s["turbo"] else ""))
    row("expiry", str(s["duration"]))
    row("strategy", s["strategy"])
    row("stake", f"{s['amount']} ({s['mm_mode']})")
    row("min conf", f"{s['min_confidence']:.2f}")
    row("min payout", f"{s['min_payout']:.0f}%")
    print(rule())


def describe_cycle(cycle: dict) -> str:
    signal = cycle.get("signal")
    if cycle.get("skipped"):
        reason = cycle.get("reason") or (signal.reason if signal else "")
        if signal and signal.tradable:
            return yellow(f"SKIP {signal.action.upper()} {signal.confidence:.2f}") + f" {dim(reason)}"
        return dim(f"hold  {reason}")
    if cycle.get("dry_run"):
        return cyan(f"DRY  {signal.action.upper()} ${cycle.get('amount')} "
                    f"{signal.confidence:.2f}")
    settlement = cycle.get("settlement") or {}
    tag = settlement.get("result", "?")
    pnl = settlement.get("pnl", 0.0)
    paint = green if tag == "win" else red if tag == "loss" else yellow
    return paint(f"{tag.upper():<4} {signal.action.upper()} ${cycle.get('amount')} {pnl:+.2f}")


def print_summary(core: UserBotCore) -> None:
    snap = core.risk.snapshot()
    cur = core.currency()
    pnl = snap["pnl"]
    header("SESSION")
    row("trades", f"{snap['trades']}  W{snap['wins']} L{snap['losses']} ={snap['equals']}")
    row("win rate", f"{snap['win_rate']:.1f}%")
    row("pnl", (green if pnl >= 0 else red)(format_money(pnl, cur)))
    if snap["stop_reason"]:
        row("stopped", yellow(snap["stop_reason"]))
    try:
        row("balance", format_money(core.balance(), cur))
    except Exception:
        pass
    print(rule())
    print()


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    if args.list_only:
        print_catalog()
        return 0

    if args.init:
        init_runtime()
        print(f" {green('●')} runtime dir: {bold(str(RUNTIME_DIR))}")
        print(f"   env:   {dim(str(RUNTIME_DIR / '.env'))}")
        print(f"   logs:  {dim(str(RUNTIME_DIR / 'logs'))}")
        print(f"   data:  {dim(str(RUNTIME_DIR / 'data'))}")
        print(dim(f"   edit {RUNTIME_DIR / '.env'}  →  IQ_EMAIL / IQ_PASSWORD"))
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
        print(red(f" ✗ {exc}"))
        print(dim(f"   edit {cfg.source}"))
        print()
        return 2

    core = UserBotCore(cfg, logger=log)

    def _shutdown(signum, _frame):
        core.stop()

    os_signal.signal(os_signal.SIGINT, _shutdown)
    os_signal.signal(os_signal.SIGTERM, _shutdown)

    try:
        print(dim(" connecting..."))
        core.connect()
        core.load_strategy()
        print(green(f" ● {core.account_type()}  "
                    f"{format_money(core.balance(), core.currency())}  "
                    f"{core.strategy.name}"))
        try:
            st = core.client.connection_status() if core.client else {}
            via = st.get("transport") or ""
            if via:
                print(dim(f" via {via}  {st.get('url') or ''}"))
        except Exception:
            pass
        print(rule())
    except Exception as exc:
        print(red(f" ✗ connect: {exc}"))
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
                log.exception("cycle error: %s", exc)
                print(red(f" ✗ {exc}"))
                try:
                    core.interruptible_sleep(core.cfg.reconnect_delay)
                except Interrupted:
                    break
                continue

            stamp = dim(time.strftime("%H:%M"))
            print(f" {stamp} {describe_cycle(cycle)}")

            if cycle.get("stop"):
                print(yellow(f" ■ {cycle.get('reason')}"))
                break
            if args.once:
                break
    except KeyboardInterrupt:
        core.stop()
    except Exception as exc:
        log.exception("fatal: %s", exc)
        print(red(f" ✗ fatal: {exc}"))
        exit_code = 1
    finally:
        print_summary(core)
        core.disconnect()
    return exit_code


def console_main() -> int:
    """Entry point for the installed ``bot`` console command.

    Mirrors ``python -m userbot``: runs :func:`main` and translates its
    integer return code into the process exit code.
    """
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
