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
dropped on some networks (the websocket handshake just hangs until
`connect_timeout`).  The client logs in over HTTPS **first** (same flow as the
known-good snippet: `POST /api/v2/login` → SSID), then opens
`wss://…/echo/websocket` with `Cookie: ssid=…`, `Origin` and a real browser
UA, then sends the `ssid` frame.

`curl_cffi` is optional: when it imports cleanly the client impersonates
**Firefox** (TLS/JA3).  On Termux / Python 3.14 a broken wheel or an old
`websocket-client` used to crash with `'Thread' object has no attribute
'isAlive'` — that alias was removed in Python 3.13.  The bot now patches it
and uses a ping-free recv loop, so stock `requests` + `websocket-client`
works the same way the standalone example does.

```bash
pip install -U 'websocket-client>=1.6' requests
pip install 'curl_cffi>=0.7'   # optional, helps on strict Cloudflare
```

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
| Digital | `trading/digital.py` | সম্পূর্ণ আলাদা flow: price stream (`digital-option-client-price-generated` **বা** `instrument-quotes-generated`, দুটোতেই subscribe) → instrument_index → asset_id → strike → CALL/PUT symbol → instrument_id → `place-digital-option` v3.0. স্ট্রিম না এলে `get-strike-list` fallback |
| Blitz | `trading/blitz.py` | 5/10/15/30/60s blitz option — `binary-options.open-option` v2.0 + `option_type_id=12` (আলাদা `blitz-options.*` microservice নেই), position subscription, result |
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

---

## Trade placement — wire contracts

তিনটা প্রোডাক্টের আসল wire contract (লগ থেকে ভেরিফাই করা):

| Product | Microservice | Version | মূল ফিল্ড |
|---|---|---|---|
| Binary | `binary-options.open-option` | `1.0` | `option_type_id` 1, ladder-aligned `expired` |
| Turbo | `binary-options.open-option` | `1.0` | `option_type_id` 3, 1–5 মিনিট ladder |
| Blitz | `binary-options.open-option` | `2.0` | `option_type_id` **12**, `expired` **এবং** `expiration_size`, non-zero `value` |
| Digital | `digital-options.place-digital-option` | `3.0` | `instrument_id` (স্ট্রিম থেকে আসা symbol), string `amount`, `instrument_index`, `asset_id` |

### কেন আগের কোড fail করত

* **Blitz → `no response for request_id`** — `blitz-options.open-option` নামে gateway-তে কোনো
  microservice নেই; ফ্রেম accept হয়ে চুপচাপ drop হতো, তাই ২৫ সেকেন্ড পর timeout। এখন blitz
  binary চ্যানেলেই যায় (`option_type_id=12`, v2.0) এবং `expired` + `value` দুটোই পাঠায়।
* **Binary/Turbo → `asset is not available`** — asset-এর trading `schedule` চেক করা হতো না
  (`enabled` true থাকলেই "open" ধরা হতো), blitz asset-এ তো `market_status` বসানোই হতো না।
  ফলে বন্ধ মার্কেটে অর্ডার যেত আর সার্ভার reject করত। এখন schedule মানা হয় এবং catalog-এ
  ৬০ সেকেন্ডের TTL আছে, তাই স্ট্যাটাস আর বাসি থাকে না।
* **Digital → `instrument-quotes-generated not received`** — দুটো আলাদা বাগ। (১) অ্যাকাউন্টভেদে
  প্ল্যাটফর্ম `digital-option-client-price-generated` পাঠায়, ওই ইভেন্টে subscribe করা হতো না।
  (২) `routingFilters` **server-side**, কিন্তু ক্লায়েন্ট লোকালিও ফিল্টার মেলাত আর payload-এ
  `kind`/`instrument_type`/`expiration_period` না থাকায় **প্রতিটা** ফ্রেম ফেলে দিত — এতে
  candle আর position স্ট্রিমও নীরবে ভাঙা ছিল। এখন দুটো স্ট্রিমেই subscribe হয়, filter শুধু
  contradiction-এ reject করে, আর স্ট্রিম না এলে `get-strike-list` fallback আছে।

`place-digital-option` আর `open-option` দুটোই মাঝে মাঝে request_id echo না করে শুধু
broadcast করে (`digital-option-placed` / `option-opened` / `option-rejected`) — সেগুলো
`trading/option_events.py`-এর matcher দিয়ে correlate হয়, তাই আর timeout-এ ঝুলে থাকে না।

### লগ থেকে ভেরিফাই করা ইভেন্ট ও সাবস্ক্রিপশন

| কী | নাম / ভার্সন | নোট |
|---|---|---|
| Digital placement | `digital-options.place-digital-option` v3.0 | body ঠিক ৩টা ফিল্ড: `user_balance_id`, `instrument_id`, string `amount` |
| Digital reply | `digital-option-placed` → `{id}`, `status: 2000` | numeric `2xxx` = accepted; request_id echo না করলেও correlate হয় |
| Digital price | `digital-option-client-price-generated` | `prices[].strike` + `call/put.symbol` → সরাসরি `instrument_id` |
| Position stream | `portfolio.position-changed` v3.0 | routingFilters: `user_id` + `user_balance_id` + `instrument_type` |
| Position query | `portfolio.get-positions` v4.0 | `user_balance_id` + `instrument_types` + `offset`/`limit` |
| Live candle | `candle-generated` | `open`/`close`/`min`/`max` + `ask`/`bid`/`phase` |

`Candle`-এ এখন `ask`, `bid`, `phase` আর `spread` প্রপার্টি আছে (হিস্টোরিক্যাল ক্যান্ডেলে `None`)।
অ্যাকাউন্ট সিলেক্ট করলেই `PositionManager` সেই `user_balance_id`-তে bind হয়ে যায়, তাই রেজাল্ট
পোলিং কখনো অন্য ব্যালেন্স থেকে পজিশন পড়ে না।

অফলাইনে সব ফিক্স যাচাই:

```bash
python3 tools/offline_check.py     # 25 checks, কোনো credential লাগে না
```
