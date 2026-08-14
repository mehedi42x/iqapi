#!/usr/bin/env python3
"""Point-to-point test suite for the whole iqapi IQ Option API.

    python quicktest.py                 # every check + $1 PRACTICE trades
    python quicktest.py --no-trade      # read-only (no orders at all)
    python quicktest.py --asset EURUSD-OTC --amount 1
    python quicktest.py --verbose       # full tracebacks on failure
    python quicktest.py --result-timeout 180

What it checks, in order
------------------------
  1. login / auth .......... credentials, HTTPS login, websocket, ssid, profile
  2. account ............... list accounts, get-balances, switch PRACTICE/REAL,
                             balance of each account, switch back to PRACTICE
  3. market data ........... server time, asset resolve, open/closed flags,
                             market price (tick + bid/ask), payout, payout books
  4. candles ............... latest N candles, candles up to a *specific time*,
                             candles inside a *specific time range*, paged history
  5. trades (PRACTICE) ..... blitz, binary/turbo, digital options incl. results,
                             forex and CFD positions incl. floating pnl + close
  6. wrap-up ............... order/position/portfolio/history, balance after,
                             clean disconnect

The final report prints every check with its error message, plus the total
counts (passed / failed / skipped).  Exit code: 0 = all passed, 1 = some
failed, 2 = setup error.  Orders are only ever placed on the PRACTICE
account - this script will not spend real money.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import EnvConfig, UserBotCore, format_money, setup_logging  # noqa: E402


# ---------------------------------------------------------------------------
# Test bookkeeping
# ---------------------------------------------------------------------------
class _Skip(Exception):
    """Raised inside a check when it cannot run in this environment."""


@dataclass
class Check:
    name: str
    ok: bool = False
    skipped: bool = False
    detail: str = ""
    error: str = ""
    elapsed: float = 0.0


@dataclass
class Runner:
    verbose: bool = False
    checks: List[Check] = field(default_factory=list)

    def section(self, title: str) -> None:
        print()
        print(f" ── {title} " + "─" * max(4, 48 - len(title)))

    def run(self, name: str, fn: Callable[[], Optional[str]]) -> Check:
        t0 = time.perf_counter()
        check = Check(name=name)
        try:
            detail = fn()
            check.ok = True
            check.detail = detail or ""
            mark = "✓"
        except _Skip as skipped:
            check.skipped = True
            check.detail = str(skipped) or "skipped"
            mark = "–"
        except Exception as exc:  # noqa: BLE001 - every error becomes a FAIL row
            check.error = f"{type(exc).__name__}: {exc}"
            mark = "✗"
            if self.verbose:
                traceback.print_exc()
        check.elapsed = time.perf_counter() - t0
        self.checks.append(check)

        extra = check.detail if (check.ok or check.skipped) else check.error
        suffix = f"  {extra[:90]}" if extra else ""
        print(f"  {mark} {name:<34} {check.elapsed:>6.1f}s{suffix}")
        return check


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _money(value: Any, currency: str = "") -> str:
    try:
        return format_money(float(value), currency)
    except Exception:
        return f"{value} {currency}".strip()


def _direction_from_candles(candles: List[Any]) -> str:
    """Deterministic direction so the test can place a real order."""
    if len(candles) < 2:
        return "call"
    last_c = float(getattr(candles[-1], "close", 0) or 0)
    prev_c = float(getattr(candles[-2], "close", 0) or 0)
    return "call" if last_c >= prev_c else "put"


def _pick_asset(core: UserBotCore, preferred: str) -> str:
    from iq_option_api import InstrumentType  # local import keeps --help fast
    for name in [preferred, "EURUSD", "EURUSD-OTC", "GBPUSD-OTC", "AUDCAD-OTC"]:
        try:
            if core.client.is_market_open(name, InstrumentType.TURBO) or \
                    core.client.is_market_open(name, InstrumentType.BINARY):
                return name
        except Exception:
            continue
    return preferred


def _result_for(core: UserBotCore, order: Any, timeout: float):
    """Resolve an order id to its position and block until it settles."""
    client = core.client
    position = client.positions.by_order_id(order.order_id) or \
        client.positions.get(order.order_id)
    position_id = position.position_id if position else order.order_id
    return client.positions.wait_for_close(position_id, timeout=timeout)


def _fmt_result(result: Any) -> str:
    tag = str(getattr(result, "result", "?")).upper()
    pnl = float(getattr(result, "pnl", 0.0) or 0.0)
    return f"{tag}  pnl={pnl:+.2f}"


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def run_suite(args: argparse.Namespace) -> int:
    cfg = EnvConfig.load()
    cfg.account_mode = "PRACTICE"
    cfg.allow_real = False
    cfg.dry_run = False
    cfg.amount = max(1.0, float(args.amount))
    setup_logging(cfg.log_level)
    runner = Runner(verbose=args.verbose)

    print()
    print(" IQ API QUICKTEST  (point-to-point suite)")
    print("═" * 56)
    print(f" email      {cfg.email or '(missing)'}")
    print(f" asset      {args.asset}    amount ${args.amount:.2f}    "
          f"trades {'OFF (--no-trade)' if args.no_trade else 'ON (practice)'}")
    print(f" imperson   {cfg.impersonate or 'off'}")
    print("═" * 56)

    # ------------------------------------------------------------------
    # 0. credentials + connect + login
    # ------------------------------------------------------------------
    runner.section("1. LOGIN / AUTH")
    try:
        cfg.validate_credentials()
        runner.run("credentials (.env)", lambda: f"account_mode={cfg.account_mode}")
    except Exception as exc:
        runner.run("credentials (.env)", lambda: (_ for _ in ()).throw(exc))
        _report(runner)
        return 2

    core = UserBotCore(cfg)
    connect_ok = runner.run("connect + login (HTTPS → WS)", _connect(core))
    if not connect_ok.ok:
        _report(runner)
        core.disconnect()
        return 1

    client = core.client  # IQOptionClient — the layer under test

    def _c(name: str, fn: Callable[[], Optional[str]]) -> Check:
        return runner.run(name, fn)

    try:
        _c("session ssid",
           lambda: f"len={len(client.ssid or '')}"
           if client.ssid else (_ for _ in ()).throw(RuntimeError("empty ssid")))
        _c("is_authenticated",
           lambda: "True" if client.is_authenticated
           else (_ for _ in ()).throw(RuntimeError("not authenticated")))

        def _profile():
            profile = client.get_profile()
            uid = profile.get("user_id") or profile.get("id")
            if not uid:
                raise RuntimeError("profile has no user_id")
            return f"user_id={uid}  email={profile.get('email', '?')}"
        _c("get-profile", _profile)

        # --------------------------------------------------------------
        # 2. account switch + balance (PRACTICE and REAL)
        # --------------------------------------------------------------
        runner.section("2. ACCOUNT SWITCH + BALANCE")
        accounts = client.list_accounts(refresh=True)
        kinds = sorted({a.type.value for a in accounts})
        _c("list_accounts", lambda: f"{len(accounts)} accounts ({', '.join(kinds)})")

        def _billing():
            balances = client.billing_balances()   # raw get-balances
            if not balances:
                raise RuntimeError("get-balances returned nothing")
            ids = sorted({b.type_id for b in balances})
            return f"{len(balances)} balances  type_ids={ids}"
        _c("get-balances (billing)", _billing)

        has_real = any(a.type.value == "REAL" for a in accounts)
        has_practice = any(a.type.value == "PRACTICE" for a in accounts)

        def _switch_practice():
            if not has_practice:
                raise _Skip("user has no PRACTICE account")
            acc = client.change_balance("PRACTICE")
            if acc.type.value != "PRACTICE":
                raise RuntimeError(f"server says type={acc.type.value}")
            return f"type={acc.type.value}  id={acc.balance_id}"
        _c("switch → PRACTICE", _switch_practice)

        def _balance_practice():
            if not has_practice:
                raise _Skip("user has no PRACTICE account")
            bal = client.balance(refresh=True)
            return _money(bal, client.currency())
        _c("balance (PRACTICE)", _balance_practice)

        def _switch_real():
            if not has_real:
                raise _Skip("user has no REAL account")
            acc = client.change_balance("REAL")
            if acc.type.value != "REAL":
                raise RuntimeError(f"server says type={acc.type.value}")
            return f"type={acc.type.value}  id={acc.balance_id}"
        _c("switch → REAL", _switch_real)

        def _balance_real():
            if not has_real:
                raise _Skip("user has no REAL account")
            return _money(client.balance(refresh=True), client.currency())
        _c("balance (REAL)", _balance_real)

        def _back_to_practice():
            if not has_practice:
                raise _Skip("user has no PRACTICE account")
            acc = client.change_balance("PRACTICE")
            if not client.is_demo:
                raise RuntimeError("active account is not PRACTICE after switch")
            return f"active={client.account_type.value}  id={acc.balance_id}"
        _c("switch back → PRACTICE", _back_to_practice)

        # --------------------------------------------------------------
        # 3. market data / price
        # --------------------------------------------------------------
        runner.section("3. MARKET DATA / PRICE")
        _c("server time sync", lambda: f"offset={client.sync_time():+.2f}s")

        asset = _pick_asset(core, args.asset)
        print(f"     using asset: {asset}")

        from iq_option_api import InstrumentType

        def _resolve():
            asset_id = client.market.asset_id(asset)
            if not asset_id:
                raise RuntimeError(f"cannot resolve {asset}")
            return f"{asset} → id={asset_id}"
        _c("resolve asset id", _resolve)

        flags = {}

        def _open_flags():
            for key, itype in (("turbo", InstrumentType.TURBO),
                               ("binary", InstrumentType.BINARY),
                               ("digital", InstrumentType.DIGITAL),
                               ("blitz", InstrumentType.BLITZ),
                               ("forex", InstrumentType.FOREX)):
                flags[key] = bool(client.is_market_open(asset, itype))
            return "  ".join(f"{k}={'open' if v else 'closed'}"
                             for k, v in flags.items())
        _c("market open flags", _open_flags)

        def _price():
            try:
                price = client.price(asset)          # live tick stream
                value = price.value or price.mid
            except Exception:
                value = client.market.current_price(asset)   # candle fallback
            if not value:
                raise RuntimeError("no price available")
            return f"{asset} = {float(value):.6f}"
        _c("market price (tick)", _price)

        def _bid_ask():
            quote = {}
            try:
                quote = client.bid_ask(asset) or {}
            except Exception:
                quote = {}
            bid, ask = quote.get("bid"), quote.get("ask")
            if bid is None and ask is None:
                raise _Skip("no bid/ask book for this asset")
            return f"bid={bid}  ask={ask}"
        _c("bid / ask", _bid_ask)

        def _payout():
            payout = core.current_payout(asset)
            if payout is None:
                raise _Skip("payout not published for this asset")
            return f"payout={payout}%"
        _c("payout", _payout)

        _c("top_assets", lambda: _top_assets(client))
        _c("instruments book (binary)", lambda: _instruments(client))

        # --------------------------------------------------------------
        # 4. candles — latest / specific time / specific range
        # --------------------------------------------------------------
        runner.section("4. CANDLE DATA")
        candles: List[Any] = _candles_now(client, asset)

        def _latest():
            if not candles:
                raise RuntimeError("no candles returned")
            last = candles[-1]
            return (f"{len(candles)} bars ×60s  "
                    f"last close={last.close}  @{_utc(last.from_ts)}")
        _c("candles — latest 30 × 1m", _latest)

        server_now = client.server_time
        one_hour_back = int(server_now // 3600 * 3600) - 3600   # last full hour

        def _specific_time():
            rows = client.market.get_candles(asset, 60, 5, end_time=one_hour_back)
            if not rows:
                raise RuntimeError("no candles at the requested time")
            aligned = [c for c in rows if c.from_ts <= one_hour_back <= c.to_ts + 90]
            target = aligned[-1] if aligned else rows[-1]
            if target.to_ts and target.to_ts - one_hour_back > 180 and not aligned:
                raise RuntimeError("candles not aligned with the requested time")
            return (f"candle @{_utc(target.from_ts)}  "
                    f"o={target.open} c={target.close}")
        _c("candles — specific time data", _specific_time)

        def _time_range():
            range_end = int(server_now - 1800)        # 30 minutes ago
            range_start = int(server_now - 5400)      # 90 minutes ago
            span = (range_end - range_start) // 60 + 10
            rows = client.market.get_candles(asset, 60, span, end_time=range_end)
            inside = [c for c in rows
                      if c.from_ts >= range_start - 90
                      and (c.to_ts or c.from_ts) <= range_end + 90]
            if len(inside) < 3:
                raise RuntimeError(f"only {len(inside)} candles inside the range")
            first, last = inside[0], inside[-1]
            return (f"{len(inside)} bars within "
                    f"{_utc(first.from_ts)} → {_utc(last.from_ts)}")
        _c("candles — specific range data", _time_range)

        def _paged_history():
            rows = client.historical_data(asset, 60, 260)
            if len(rows) < 200:
                raise RuntimeError(f"paging returned only {len(rows)} candles")
            return f"{len(rows)} candles via backward paging"
        _c("candles — paged history (260)", _paged_history)

        # --------------------------------------------------------------
        # 5. trades — PRACTICE ONLY
        # --------------------------------------------------------------
        runner.section("5. TRADES  (practice account only)")
        direction = _direction_from_candles(candles)
        amount = float(args.amount)

        def _ensure_practice() -> None:
            if not has_practice or not client.is_demo:
                raise _Skip("no PRACTICE account selected — refusing to trade")

        if args.no_trade:
            _c("trade engine", lambda: (_ for _ in ()).throw(
                _Skip("--no-trade: orders disabled")))
        else:
            print(f"     direction hint from candles: {direction.upper()}")

            _trade_options(runner, core, client, asset, direction, amount,
                           flags, args, _ensure_practice)
            _trade_marginal(runner, core, client, asset, amount, args,
                            _ensure_practice)

        # --------------------------------------------------------------
        # 6. wrap-up
        # --------------------------------------------------------------
        runner.section("6. PORTFOLIO / HISTORY / WRAP-UP")

        def _open_positions():
            positions = client.open_positions()
            return f"{len(positions)} open position(s)"
        _c("open positions", _open_positions)

        _c("portfolio summary",
           lambda: f"open={client.portfolio_summary().get('open_positions')}")

        def _orders():
            orders = client.order_history(limit=10)
            return f"{len(orders)} order record(s)"
        _c("order history", _orders)

        def _history():
            history = client.get_history(limit=5)
            rows = getattr(history, "positions", None) or \
                getattr(history, "items", None) or []
            return f"{len(rows)} closed trade(s) in history"
        _c("trade history (last 5)", _history)

        def _balance_after():
            bal = client.balance(refresh=True)
            return _money(bal, client.currency())
        _c("balance after tests", _balance_after)

    finally:
        runner.section("7. DISCONNECT")
        t0 = time.perf_counter()
        try:
            core.disconnect()
            runner.checks.append(Check(name="disconnect", ok=True,
                                       detail="socket closed",
                                       elapsed=time.perf_counter() - t0))
            print(f"  ✓ {'disconnect':<34} {time.perf_counter() - t0:>6.1f}s  socket closed")
        except Exception as exc:  # noqa: BLE001
            runner.checks.append(Check(name="disconnect", ok=False,
                                       error=str(exc),
                                       elapsed=time.perf_counter() - t0))
            print(f"  ✗ {'disconnect':<34} {time.perf_counter() - t0:>6.1f}s  {exc}")

    return _report(runner)


# ---------------------------------------------------------------------------
# Individual probe helpers
# ---------------------------------------------------------------------------
def _connect(core: UserBotCore) -> Callable[[], str]:
    def _inner() -> str:
        t0 = time.time()
        core.connect()
        status = {}
        try:
            status = core.client.connection_status()
        except Exception:
            pass
        transport = status.get("transport") or "?"
        return f"logged in via {transport} in {time.time() - t0:.1f}s"
    return _inner


def _top_assets(client: Any) -> str:
    for kind in ("turbo", "binary", "digital-option"):
        try:
            data = client.top_assets(kind)
        except Exception:
            continue
        if isinstance(data, dict) and data:
            return f"{kind}: {len(data)} entries"
    raise RuntimeError("top-assets-info empty for turbo/binary/digital")


def _instruments(client: Any) -> str:
    for kind in ("binary", "turbo"):
        try:
            data = client.get_instruments(kind)
        except Exception:
            continue
        instruments = data.get("instruments") if isinstance(data, dict) else None
        if instruments:
            return f"{kind}: {len(instruments)} instrument(s)"
    raise RuntimeError("get-instruments returned no entries")


def _candles_now(client: Any, asset: str) -> List[Any]:
    try:
        return client.candles(asset, 60, 30)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Trade flows
# ---------------------------------------------------------------------------
def _trade_options(runner: Runner, core: UserBotCore, client: Any, asset: str,
                   direction: str, amount: float, flags: dict,
                   args: argparse.Namespace, ensure_practice: Callable[[], None]) -> None:
    """Blitz + binary/turbo + digital, each with its settlement result."""

    # -- blitz (fastest confirmation - 5s) --------------------------------
    def _blitz() -> str:
        ensure_practice()
        if not flags.get("blitz"):
            raise _Skip("blitz book closed for this asset")
        duration = min(client.blitz.durations(asset) or [5])
        order = client.blitz.buy(asset, amount, direction, duration=duration)
        if not order.order_id:
            raise RuntimeError("order accepted but no order id returned")
        result = _result_for(core, order, timeout=args.result_timeout)
        return f"id={order.order_id}  {duration}s  {_fmt_result(result)}"
    runner.run("blitz trade → result", _blitz)

    # -- binary / turbo 1 minute ------------------------------------------
    def _binary() -> str:
        ensure_practice()
        turbo = bool(flags.get("turbo"))
        if not (turbo or flags.get("binary")):
            raise _Skip("binary & turbo books closed (weekend/off-hours)")
        kind = "turbo" if turbo else "binary"
        order = client.binary.buy(asset, amount, direction, duration=1, turbo=turbo)
        if not order.order_id:
            raise RuntimeError("order accepted but no order id returned")
        result = client.binary.check_result(order, timeout=args.result_timeout)
        return f"{kind}  id={order.order_id}  {_fmt_result(result)}"
    runner.run("binary/turbo trade → result", _binary)

    # -- digital 1 minute ---------------------------------------------------
    def _digital() -> str:
        ensure_practice()
        if not flags.get("digital"):
            raise _Skip("digital book closed for this asset")
        order = client.digital.buy(asset, amount, direction, duration=1)
        if not order.order_id:
            raise RuntimeError("order accepted but no order id returned")
        result = client.digital.check_result(order, timeout=args.result_timeout)
        return f"id={order.order_id}  {_fmt_result(result)}"
    runner.run("digital trade → result", _digital)


def _trade_marginal(runner: Runner, core: UserBotCore, client: Any, asset: str,
                    amount: float, args: argparse.Namespace,
                    ensure_practice: Callable[[], None]) -> None:
    """Forex + CFD: open, read floating pnl, close, read settled result."""

    # -- forex --------------------------------------------------------------
    def _forex() -> str:
        ensure_practice()
        from iq_option_api import InstrumentType
        if not client.is_market_open(asset, InstrumentType.FOREX):
            raise _Skip("forex market closed for this asset")
        order = client.forex.buy(asset, amount)
        oid = order.order_id
        if not oid:
            raise RuntimeError("forex order accepted but no order id")
        status = client.order_status(oid)
        position = client.forex.position_of_order(order)
        if position is None:
            raise RuntimeError("forex order placed but no position appeared")
        pid = position.position_id
        time.sleep(3)
        floating = client.forex.floating_pnl(pid)
        closed = client.forex.close_position(pid)
        if not closed:
            raise RuntimeError("close_position returned False")
        result = client.positions.wait_for_close(pid, timeout=args.result_timeout)
        float_txt = f"{floating:+.2f}" if isinstance(floating, (int, float)) else "n/a"
        return (f"id={oid}  status={status}  floating={float_txt}  "
                f"closed → {_fmt_result(result)}")
    runner.run("forex trade → open/close/result", _forex)

    # -- cfd -----------------------------------------------------------------
    def _cfd() -> str:
        ensure_practice()
        cfd_asset = None
        try:
            open_assets = client.cfd.open_assets()
        except Exception:
            open_assets = []
        for candidate in open_assets or []:
            if getattr(candidate, "is_open", False):
                cfd_asset = candidate.name or candidate.asset_id
                break
        if cfd_asset is None:
            raise _Skip("no open CFD asset right now")
        order = client.cfd.buy(cfd_asset, amount)
        oid = order.order_id
        if not oid:
            raise RuntimeError("cfd order accepted but no order id")
        position = client.cfd.position_of_order(order)
        if position is None:
            raise RuntimeError("cfd order placed but no position appeared")
        pid = position.position_id
        time.sleep(3)
        floating = client.cfd.floating_pnl(pid)
        closed = client.cfd.close_position(pid)
        if not closed:
            raise RuntimeError("close_position returned False")
        result = client.positions.wait_for_close(pid, timeout=args.result_timeout)
        float_txt = f"{floating:+.2f}" if isinstance(floating, (int, float)) else "n/a"
        return f"{cfd_asset}  id={oid}  floating={float_txt}  {_fmt_result(result)}"
    runner.run("cfd trade → open/close/result", _cfd)


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------
def _report(runner: Runner) -> int:
    print()
    print("═" * 56)
    print(" FINAL TEST REPORT / ফাইনাল টেস্ট রিপোর্ট")
    print("═" * 56)
    for check in runner.checks:
        if check.ok:
            mark, state = "✓", "PASS"
            extra = check.detail
        elif check.skipped:
            mark, state = "–", "SKIP"
            extra = check.detail
        else:
            mark, state = "✗", "FAIL"
            extra = check.error
        suffix = f"  {extra[:86]}" if extra else ""
        print(f"  {mark} [{state}] {check.name:<32} {check.elapsed:>6.1f}s{suffix}")

    passed = sum(1 for c in runner.checks if c.ok)
    failed = sum(1 for c in runner.checks if not c.ok and not c.skipped)
    skipped = sum(1 for c in runner.checks if c.skipped)
    total = len(runner.checks)

    print("─" * 56)
    print(f" total={total}   passed={passed}   failed={failed}   skipped={skipped}")
    print(f" মোট টেস্ট: {total} | পাস: {passed} | ফেইল: {failed} | স্কিপ: {skipped}")
    print("─" * 56)

    failures = [c for c in runner.checks if not c.ok and not c.skipped]
    if failures:
        print(" FAILURES / এরর সমূহ:")
        for i, check in enumerate(failures, 1):
            print(f"  {i}. {check.name}: {check.error}")
        print("─" * 56)
        print(" ✗ quicktest FAILED")
        return 1
    print(" ✓ every check passed — quicktest OK")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Point-to-point test of the whole iqapi IQ Option API")
    parser.add_argument("--no-trade", action="store_true",
                        help="skip every order placement (read-only)")
    parser.add_argument("--asset", default=None,
                        help="asset to test with (default: ASSET from .env or EURUSD)")
    parser.add_argument("--amount", type=float, default=None,
                        help="stake per test trade in PRACTICE (default: 1 or AMOUNT)")
    parser.add_argument("--result-timeout", type=float, default=150.0,
                        help="seconds to wait for each trade result (default 150)")
    parser.add_argument("--verbose", action="store_true",
                        help="print tracebacks for failing checks")
    args = parser.parse_args(argv)

    cfg_probe = EnvConfig.load()
    if args.asset is None:
        args.asset = cfg_probe.asset or "EURUSD"
    if args.amount is None:
        args.amount = max(1.0, float(cfg_probe.amount or 1.0))
    args.amount = max(0.01, float(args.amount))
    return run_suite(args)


if __name__ == "__main__":
    raise SystemExit(main())
