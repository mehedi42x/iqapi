#!/usr/bin/env python3
"""Offline smoke test — no network, no credentials.

Builds a synthetic OHLC tape, runs every installed strategy through
``safe_analyze``, and checks the plugin loader + indicator helpers.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from strategies import discover, list_strategies, load_strategy  # noqa: E402
from strategies import indicators as ta  # noqa: E402
from strategies.base import Signal  # noqa: E402
from core import EnvConfig, MoneyManager, SessionRisk, parse_timeframe  # noqa: E402

from iq_option_api.connection.browser import (  # noqa: E402
    FIREFOX_USER_AGENT,
    cookie_header,
    looks_like_challenge,
    origin_for,
    resolve_impersonate,
    ws_header_list,
)
from iq_option_api.config import ConnectionConfig  # noqa: E402


@dataclass
class C:
    open: float
    high: float
    low: float
    close: float
    volume: float
    from_ts: float
    to_ts: float
    size: int = 60


def make_tape(n: int = 220, start: float = 1900.0, drift: float = 0.08,
              wave: float = 4.5) -> list:
    now = time.time() - n * 60
    price = start
    out = []
    for i in range(n):
        osc = math.sin(i / 9.0) * wave + math.sin(i / 3.3) * (wave * 0.35)
        step = drift + osc * 0.05
        o = price
        c = price + step + ((-1) ** i) * 0.15
        h = max(o, c) + abs(osc) * 0.12 + 0.4
        l = min(o, c) - abs(osc) * 0.10 - 0.3
        out.append(C(o, h, l, c, 100 + (i % 17) * 3, now + i * 60, now + (i + 1) * 60))
        price = c
    return out


def main() -> int:
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        mark = "OK " if cond else "FAIL"
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
        if not cond:
            failed += 1

    print("indicators")
    tape = make_tape()
    closes = ta.closes(tape)
    check("ema", ta.last(ta.ema(closes, 21)) is not None)
    check("rsi", 0 < (ta.last(ta.rsi(closes, 14)) or 0) < 100)
    macd = ta.macd(closes)
    check("macd", ta.last(macd[2]) is not None)
    check("atr", (ta.last(ta.atr(ta.highs(tape), ta.lows(tape), closes, 14)) or 0) > 0)
    adx, _, _ = ta.adx(ta.highs(tape), ta.lows(tape), closes, 14)
    check("adx", ta.last(adx) is not None)
    check("stoch", ta.last(ta.stochastic(ta.highs(tape), ta.lows(tape), closes)[0]) is not None)
    check("bb", ta.last(ta.bollinger(closes)[1]) is not None)
    check("supertrend", ta.last(ta.supertrend(ta.highs(tape), ta.lows(tape), closes)[1]) in {-1, 1, -1.0, 1.0})
    check("vwap", ta.last(ta.vwap(tape, 20)) is not None)

    print("loader")
    catalog = discover()
    names = sorted(catalog)
    print(f"  discovered: {', '.join(names)}")
    expected = {
        "binary1", "binary_sniper", "digital1", "digital_ai",
        "blitz_flash", "blitz_snap",
        "gold_scalp", "gold_breakout", "gold_impulse", "gold_session",
    }
    check("all shipped modules", expected <= set(names),
          f"missing={sorted(expected - set(names))}")
    check("list_strategies", len(list_strategies()) == len(catalog))

    print("signals")
    ctx = {
        "asset": "XAUUSD",
        "timeframe": 60,
        "server_time": tape[-1].to_ts,
        "htf_candles": tape[::5],
        "payout": 80,
        "instrument": "binary",
        "price": tape[-1].close,
        "dry_run": True,
    }
    for name, strat in sorted(catalog.items()):
        try:
            sig = strat.safe_analyze(tape, ctx)
        except Exception as exc:  # noqa: BLE001
            check(name, False, f"raised {exc}")
            continue
        ok = isinstance(sig, Signal) and sig.action in {"call", "put", "hold"}
        check(name, ok, repr(sig))
        # short tape must hold, not crash
        short = strat.safe_analyze(tape[:5], ctx)
        check(f"{name} warmup", short.action == "hold")

    print("browser / firefox handshake")
    check("firefox ua", "Firefox/" in FIREFOX_USER_AGENT and "Gecko/" in FIREFOX_USER_AGENT)
    check("origin wss", origin_for("wss://iqoption.com/echo/websocket") == "https://iqoption.com")
    check("cookie header", cookie_header({"ssid": "abc", "cf": "1"}) == "ssid=abc; cf=1")
    headers = ws_header_list(FIREFOX_USER_AGENT, "https://iqoption.com")
    check("ws ua header", any(h.startswith("User-Agent: ") and "Firefox/" in h for h in headers))
    check("ws origin header", "Origin: https://iqoption.com" in headers)
    check("impersonate off", resolve_impersonate("off") == "")
    check("impersonate firefox", resolve_impersonate("firefox") in {"firefox", "firefox147", "firefox144", "firefox135", "firefox133", ""})
    conn = ConnectionConfig()
    check("default ua is firefox", "Firefox/" in conn.user_agent)
    check("default impersonate", conn.impersonate == "firefox")
    check("connect timeout 30", conn.connect_timeout >= 30)

    class _Resp:
        status_code = 403
        headers = {"content-type": "text/html"}
        text = "<html>Just a moment...</html>"
    check("cf challenge detect", looks_like_challenge(_Resp()))

    print("core helpers")
    check("tf 1m", parse_timeframe("1m") == 60)
    check("tf 5", parse_timeframe("5") == 300)
    check("tf 300", parse_timeframe("300") == 300)
    cfg = EnvConfig.load()
    check("env loaded", cfg.timeframe == 60 and cfg.trade_type in {"binary", "digital", "blitz", "turbo"})
    mm = MoneyManager(cfg)
    mm.mode = "martingale"
    mm.base = mm.current = 1.0
    mm.factor = 2.0
    mm.steps = 2
    mm.on_result("loss")
    check("martingale step", mm.current == 2.0)
    mm.on_result("win")
    check("martingale reset", mm.current == 1.0)
    risk = SessionRisk(cfg, 1000)
    risk.cfg.max_consecutive_losses = 2
    risk.register("loss", -1)
    risk.register("loss", -1)
    ok, why = risk.allow()
    check("risk halt", (not ok) and "consecutive" in why, why)

    print()
    if failed:
        print(f"{failed} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
