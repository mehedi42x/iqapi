#!/usr/bin/env python3
"""Live smoke test against a PRACTICE IQ Option account.

    python livetest.py              # connect, probe every layer, place $1 trades
    python livetest.py --no-trade   # read-only (no orders)

Credentials come from ``userbot/.env`` (never from source).  REAL accounts
are refused — this script will not spend live money.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path
from typing import Any, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import EnvConfig, UserBotCore, format_money, setup_logging  # noqa: E402


Check = Tuple[str, bool, str]


def _ok(name: str, detail: str = "") -> Check:
    return name, True, detail


def _fail(name: str, detail: str = "") -> Check:
    return name, False, detail


def _pick_open(core: UserBotCore, names: List[str]) -> Optional[str]:
    for name in names:
        try:
            if core.is_open(name):
                return name
        except Exception:
            continue
    try:
        return core.resolve_asset(names[0] if names else None)
    except Exception:
        return names[0] if names else None


def _direction_from_candles(candles: List[Any]) -> str:
    if len(candles) < 2:
        return "call"
    last = candles[-1]
    prev = candles[-2]
    last_c = float(getattr(last, "close", 0) or 0)
    prev_c = float(getattr(prev, "close", 0) or 0)
    return "call" if last_c >= prev_c else "put"


def run(no_trade: bool, amount: float) -> int:
    cfg = EnvConfig.load()
    cfg.account_mode = "PRACTICE"
    cfg.allow_real = False
    cfg.dry_run = False
    cfg.amount = float(amount)
    cfg.wait_result = True
    log = setup_logging(cfg.log_level)
    checks: List[Check] = []

    print()
    print(" IQ LIVE TEST  (PRACTICE only)")
    print("─" * 44)
    print(f" email     {cfg.email}")
    print(f" amount    ${amount:.2f}")
    print(f" trade     {'no' if no_trade else 'yes'}")
    print(f" imperson  {cfg.impersonate or 'off'}")
    print(f" ua        {(cfg.user_agent or 'Firefox default')[:52]}")
    print("─" * 44)

    try:
        cfg.validate_credentials()
        checks.append(_ok("credentials"))
    except Exception as exc:
        print(f" ✗ credentials: {exc}")
        return 2

    core = UserBotCore(cfg, logger=log)
    try:
        print(" connecting (Firefox impersonation)...")
        t0 = time.time()
        core.connect()
        elapsed = time.time() - t0
        st = {}
        try:
            st = core.client.connection_status() if core.client else {}
        except Exception:
            st = {}
        checks.append(_ok(
            "connect",
            f"{elapsed:.1f}s  {st.get('transport') or '?'}  {st.get('url') or ''}",
        ))
        print(f" ● connected via {st.get('transport') or '?'} in {elapsed:.1f}s")
    except Exception as exc:
        checks.append(_fail("connect", str(exc)))
        _report(checks)
        core.disconnect()
        return 1

    try:
        # ------------------------------------------------------------------
        # Account
        # ------------------------------------------------------------------
        try:
            if core.account_type() == "REAL":
                raise RuntimeError("livetest refuses to run on a REAL account")
            core.client.use_practice()
            bal = core.balance()
            cur = core.currency()
            checks.append(_ok("practice account",
                              f"{core.account_type()}  {format_money(bal, cur)}"))
            print(f" ● PRACTICE  {format_money(bal, cur)}")
            if bal < amount:
                checks.append(_fail("balance", f"{bal} < {amount}"))
        except Exception as exc:
            checks.append(_fail("practice account", str(exc)))

        try:
            accounts = core.client.list_accounts(refresh=True)
            kinds = ", ".join(sorted({a.type.value for a in accounts}))
            checks.append(_ok("list_accounts", f"{len(accounts)} ({kinds})"))
        except Exception as exc:
            checks.append(_fail("list_accounts", str(exc)))

        try:
            profile = core.client.get_profile()
            uid = profile.get("user_id") or profile.get("id") or "?"
            checks.append(_ok("profile", f"user_id={uid}"))
        except Exception as exc:
            checks.append(_fail("profile", str(exc)))

        try:
            health = core.client.health_check()
            checks.append(_ok("health", f"connected={health.get('connected')}"))
        except Exception as exc:
            checks.append(_fail("health", str(exc)))

        # ------------------------------------------------------------------
        # Market / candles
        # ------------------------------------------------------------------
        asset = _pick_open(core, [cfg.asset, "EURUSD", "EURUSD-OTC", "GBPUSD-OTC"])
        core.live_asset = asset or cfg.asset
        print(f" asset     {core.live_asset}")

        try:
            opened = core.is_open(core.live_asset)
            checks.append(_ok("market_open", f"{core.live_asset} open={opened}"))
        except Exception as exc:
            checks.append(_fail("market_open", str(exc)))
            opened = False

        candles: List[Any] = []
        try:
            candles = core.fetch_candles(core.live_asset, size=60, count=30)
            last = candles[-1] if candles else None
            detail = (f"{len(candles)} bars  close={getattr(last, 'close', '?')}"
                      if last else "empty")
            checks.append(_ok("candles", detail) if candles else _fail("candles", "empty"))
        except Exception as exc:
            checks.append(_fail("candles", str(exc)))

        try:
            payout = core.current_payout(core.live_asset)
            checks.append(_ok("payout", f"{payout}"))
        except Exception as exc:
            checks.append(_fail("payout", str(exc)))
            payout = None

        try:
            from iq_option_api import InstrumentType
            turbo_open = core.client.is_market_open(core.live_asset, InstrumentType.TURBO)
            binary_open = core.client.is_market_open(core.live_asset, InstrumentType.BINARY)
            digital_open = core.client.is_market_open(core.live_asset, InstrumentType.DIGITAL)
            blitz_open = core.client.is_market_open(core.live_asset, InstrumentType.BLITZ)
            checks.append(_ok(
                "instrument books",
                f"turbo={turbo_open} binary={binary_open} "
                f"digital={digital_open} blitz={blitz_open}",
            ))
        except Exception as exc:
            checks.append(_fail("instrument books", str(exc)))
            turbo_open = binary_open = digital_open = blitz_open = False

        try:
            top = core.client.top_assets("turbo") or core.client.top_assets("binary")
            checks.append(_ok("top_assets", f"keys={list(top)[:4] if isinstance(top, dict) else type(top).__name__}"))
        except Exception as exc:
            checks.append(_fail("top_assets", str(exc)))

        try:
            port = core.client.portfolio_summary()
            checks.append(_ok("portfolio", f"open={port.get('open_positions')}"))
        except Exception as exc:
            checks.append(_fail("portfolio", str(exc)))

        try:
            hist = core.client.get_history(limit=5)
            checks.append(_ok("history", f"{len(hist)} trades"))
        except Exception as exc:
            checks.append(_fail("history", str(exc)))

        try:
            core.client.sync_time()
            checks.append(_ok("server_time", f"{core.server_time():.0f}"))
        except Exception as exc:
            checks.append(_fail("server_time", str(exc)))

        # ------------------------------------------------------------------
        # Strategy (offline on the live tape)
        # ------------------------------------------------------------------
        try:
            core.load_strategy()
            ctx = core.build_context(core.live_asset, candles, payout=payout)
            signal = core.generate_signal(candles or [], ctx)
            checks.append(_ok("strategy",
                              f"{core.strategy.name} → {signal.action} "
                              f"{signal.confidence:.2f}"))
        except Exception as exc:
            checks.append(_fail("strategy", str(exc)))
            signal = None

        # ------------------------------------------------------------------
        # Practice trades
        # ------------------------------------------------------------------
        if no_trade:
            checks.append(_ok("trades", "skipped (--no-trade)"))
        else:
            direction = _direction_from_candles(candles)
            placed = 0

            # 1) turbo / binary 1 minute
            if turbo_open or binary_open:
                kind = "turbo" if turbo_open else "binary"
                try:
                    print(f" ORDER  {kind} {direction.upper()} {core.live_asset} ${amount}")
                    cfg.trade_type = "binary"
                    cfg.turbo = "true" if turbo_open else "false"
                    cfg.duration = 1
                    order = core.place_order(core.live_asset, direction, amount)
                    oid = getattr(order, "order_id", None)
                    checks.append(_ok(f"{kind} order", f"id={oid}"))
                    print(f"  accepted id={oid} — waiting for expiry...")
                    result = core.wait_result(order, timeout=120.0)
                    tag = getattr(result, "result", "?")
                    pnl = getattr(result, "pnl", 0.0)
                    checks.append(_ok(f"{kind} result", f"{tag} pnl={pnl:+.2f}"))
                    print(f"  {str(tag).upper()}  pnl={pnl:+.2f}")
                    placed += 1
                except Exception as exc:
                    checks.append(_fail(f"{kind} trade", str(exc)))
                    traceback.print_exc()
            else:
                checks.append(_ok("binary/turbo", "market closed — no order"))

            # 2) blitz 5s (fastest confirmation)
            if blitz_open:
                try:
                    print(f" ORDER  blitz {direction.upper()} {core.live_asset} $1 / 5s")
                    cfg.trade_type = "blitz"
                    cfg.duration = 5
                    order = core.place_order(core.live_asset, direction, amount)
                    oid = getattr(order, "order_id", None)
                    checks.append(_ok("blitz order", f"id={oid}"))
                    result = core.wait_result(order, timeout=40.0)
                    tag = getattr(result, "result", "?")
                    pnl = getattr(result, "pnl", 0.0)
                    checks.append(_ok("blitz result", f"{tag} pnl={pnl:+.2f}"))
                    print(f"  {str(tag).upper()}  pnl={pnl:+.2f}")
                    placed += 1
                except Exception as exc:
                    checks.append(_fail("blitz trade", str(exc)))
            else:
                checks.append(_ok("blitz", "market closed — no order"))

            # 3) digital 1m if the book is open
            if digital_open:
                try:
                    print(f" ORDER  digital {direction.upper()} {core.live_asset} ${amount}")
                    cfg.trade_type = "digital"
                    cfg.duration = 1
                    order = core.place_order(core.live_asset, direction, amount)
                    oid = getattr(order, "order_id", None)
                    checks.append(_ok("digital order", f"id={oid}"))
                    result = core.wait_result(order, timeout=120.0)
                    tag = getattr(result, "result", "?")
                    pnl = getattr(result, "pnl", 0.0)
                    checks.append(_ok("digital result", f"{tag} pnl={pnl:+.2f}"))
                    print(f"  {str(tag).upper()}  pnl={pnl:+.2f}")
                    placed += 1
                except Exception as exc:
                    checks.append(_fail("digital trade", str(exc)))
            else:
                checks.append(_ok("digital", "market closed — no order"))

            if placed == 0:
                checks.append(_ok("trades", "no open books this session (weekend/off-hours)"))

        try:
            bal2 = core.balance()
            checks.append(_ok("balance after", format_money(bal2, core.currency())))
        except Exception as exc:
            checks.append(_fail("balance after", str(exc)))

    finally:
        core.disconnect()

    return _report(checks)


def _report(checks: List[Check]) -> int:
    print()
    print(" RESULTS")
    print("─" * 44)
    failed = 0
    for name, ok, detail in checks:
        mark = "OK  " if ok else "FAIL"
        extra = f"  {detail}" if detail else ""
        print(f"  [{mark}] {name}{extra}")
        if not ok:
            failed += 1
    print("─" * 44)
    if failed:
        print(f" {failed} check(s) failed")
        return 1
    print(" all live checks passed")
    return 0


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="Practice-account live smoke test")
    p.add_argument("--no-trade", action="store_true", help="skip placing orders")
    p.add_argument("--amount", type=float, default=1.0, help="stake (PRACTICE)")
    args = p.parse_args(argv)
    return run(no_trade=args.no_trade, amount=max(1.0, float(args.amount)))


if __name__ == "__main__":
    raise SystemExit(main())
