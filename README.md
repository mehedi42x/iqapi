# iq_option_api

IQ Option-এর জন্য একটি সম্পূর্ণ **modular trading API module**। প্রতিটি capability আলাদা layer-এ,
প্রতিটি layer আলাদাভাবে ব্যবহার/টেস্ট করা যায়, আর `IQOptionClient` হলো সবকিছুকে একসাথে বেঁধে দেওয়া facade।

> `core.py` / `main.py` এই মডিউলের অংশ নয় — সেগুলো আলাদা diagnostic/application project।
>
> লাইভ বট + ব্যাকটেস্ট আলাদা প্যাকেজ: [`userbot/`](userbot/README.md)
> (`bot.py`, `core.py`, `backtest.py`, প্লাগইন `strategies/` — `.env` দিয়ে কনফিগ)।

---

## Install / Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# websocket-client, requests, and curl_cffi (Firefox TLS impersonation)
```

IQ Option sits behind Cloudflare.  A stock Python TLS fingerprint is silently
dropped (the websocket handshake just hangs until `connect_timeout`).  The
client impersonates **Firefox 147** via `curl_cffi` and logs in over HTTPS
*before* opening `wss://…/echo/websocket`, carrying the same cookies.

Credentials কখনো কোডে hardcode হয় না — environment বা JSON config থেকে আসে:

```bash
export IQ_EMAIL="you@example.com"
export IQ_PASSWORD="••••••••"
export IQ_ACCOUNT_MODE=PRACTICE      # PRACTICE | REAL
export IQ_ALLOW_REAL=false           # real account trading গার্ড
```

## Quick start

```python
from iq_option_api import IQOptionClient, InstrumentType

with IQOptionClient() as iq:                 # connect + authenticate + account select
    iq.use_practice()
    print(iq.balance(), iq.currency())

    # market
    print(iq.is_market_open("EURUSD", InstrumentType.BINARY))
    print(iq.price("EURUSD"))
    print(iq.candles("EURUSD", size=60, count=100)[-1])

    # binary trade
    order = iq.binary.buy("EURUSD", 10, "call", duration=1)
    result = iq.binary.check_result(order)
    print(result.status, result.profit)

    # portfolio & history
    print(iq.portfolio_summary())
    print(iq.trade_statistics())
```

## Layers

| Layer | Path | কী করে |
|---|---|---|
| Connection | `connection/` | WebSocket lifecycle, protocol frames, request/response + request-id, heartbeat, timeSync, auto-reconnect, subscription routing |
| Auth | `auth/` | email/password login, SSID acquire/persist/restore, session validation ও expiry detect, auto re-login |
| Account | `account/` | account list, REAL/PRACTICE/TOURNAMENT, **server-verified** switching, `user_balance_id`, balance, currency, statistics |
| Billing | `billing/` | `internal-billing.get-balances` — trading balance-এর সাথে **মেশানো হয় না** |
| Market | `market/` | asset discovery, open/close status ও schedule, price/bid-ask/tick stream, candle ও historical data, server time |
| Instruments | `market/instruments.py` | সব instrument type-এর common abstraction (id, asset id, symbol, expiration, strike, direction, payout, leverage) |
| Orders | `trading/orders.py` | creation → validation → submission → id → state (pending/filled/rejected), cancel/modify, order history |
| Positions | `trading/positions.py` | `portfolio.position-changed`, entry/current price, floating P/L, SL/TP, close, settlement, `wait_for_close` |
| Binary | `trading/binary.py` | CALL/PUT, expiration, payout, result, P/L, history |
| Digital | `trading/digital.py` | সম্পূর্ণ আলাদা flow: `digital-option-client-price-generated` → instrument_index → asset_id → strike → CALL/PUT symbol → instrument_id → trade |
| Blitz | `trading/blitz.py` | 5/10/15/30/60s blitz option, position subscription, result |
| Marginal | `trading/marginal.py` | leverage/margin ভিত্তিক common engine — Forex, CFD, Stocks, Crypto, Commodities, ETF, Indices একই logic reuse করে (duplicate নেই) |
| Portfolio | `portfolio/` | `portfolio.get-positions`, `portfolio.get-stats`, `position-changed`, exposure by asset |
| History | `history/` | সব instrument-এর closed trade history + statistics |
| Risk | `risk/` | balance/min/max amount, max exposure, duplicate order block, order frequency, real-account protection, emergency kill-switch |
| Models | `models/` | Account, Balance, Asset, Instrument, Price, Candle, Tick, Order, Position, Trade, TradeResult, Portfolio, PortfolioStats, History, MarketStatus |
| Errors | `exceptions/` | Authentication, Session, Connection, Account, Market, Asset, Instrument, Order, Position, Balance, Protocol, Timeout, Risk, Configuration |
| Config | `config/` | credentials, SSID storage, account mode, default asset, timeouts, reconnect policy, WS settings, trading limits, logging |

## Main flow

```
CONNECT → AUTHENTICATE → SESSION → ACCOUNT → BALANCE → MARKET → ASSET
   → INSTRUMENT → PRICE/CANDLE/STREAM → ORDER → POSITION → PORTFOLIO
   → RESULT → HISTORY → MONITOR/RECONNECT
```

## Product APIs

```python
iq.binary.buy(asset, amount, "call", duration=1)
iq.digital.call(asset, amount, duration=1)
iq.blitz.put(asset, amount, 30)          # 5/10/15/30/60 সেকেন্ড

iq.forex.buy("EURUSD", 20, leverage=50, stop_loss=..., take_profit=...)
iq.cfd.sell("AAPL", 25, leverage=20)
iq.stocks.buy(...); iq.crypto.buy(...); iq.commodities.buy(...)
iq.etf.buy(...);    iq.indices.buy(...)
```

Crypto/Stock/Commodity/ETF/Index যদি সার্ভার থেকে CFD হিসেবে আসে, তা CFD wire type (`marginal-cfd`)
দিয়েই যায় — trading logic একবারই লেখা।

## Risk & safety

```python
iq.risk_status()                      # limits, exposure, order rate, kill-switch
iq.disable_trading("manual stop")     # emergency stop
iq.enable_trading()
```

- REAL account-এ trade করতে হলে `allow_real_account_trading=True` লাগবেই।
- একই order 1 সেকেন্ডের মধ্যে duplicate হলে block হয়।
- account type ও `user_balance_id` সবসময় server data দিয়ে verify হয় — কোনো hardcoded id নেই।

## Streams

```python
iq.subscribe_ticks("EURUSD", callback)
iq.subscribe_candles("EURUSD", 60, callback)
iq.start_streams(callback)                 # portfolio.position-changed
iq.stop_streams()
```

Reconnect হলে subscription গুলো নিজে থেকেই resubscribe হয় এবং session/account restore হয়।
