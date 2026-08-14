# api — বট-ফেসিং মডিউল সিস্টেম

API এখানে শুধু **দালালের মতো কাজ করে** — বট যা চাইবে, তাই এনে দেবে।
কোনো strategy, কোনো অপ্রয়োজনীয় logic নেই। প্রতিটি module এক লাইনের কল।

```
api/
├── manager.py   ← সব module-কে maintain করে (IQAPI — একটাই entry point)
├── auth.py      ← login, ssid, balance, account type change, symbol set, account set
├── blitz.py     ← blitz-এর যাবতীয় সব control
├── binary.py    ← binary trade place, amount set, call/put, track, results
├── digital.py   ← digital trade place, amount set, track, results
├── forex.py     ← forex buy/sell, SL/TP set, leverage set, amount set, track
└── data.py      ← timeframe set সহ সকল data লেনদেন (candles, ticks, price)
```

ভেতরে সব কাজ করে আগের layered engine (`iq_option_api/`) — connection,
Cloudflare/TLS, reconnect, request correlation সব ওখানেই। এই `api/`
প্যাকেজটা তার উপরে বসানো পাতলা, বট-বান্ধব চেহারা।

## Quick start

```python
from api import IQAPI

with IQAPI() as iq:                      # credentials: IQ_EMAIL / IQ_PASSWORD env
    # --- auth ---------------------------------------------------------
    iq.auth.set_account("PRACTICE")      # PRACTICE / REAL
    iq.auth.set_symbol("EURUSD-OTC")     # সব module-এর default symbol
    print(iq.auth.balance(), iq.auth.currency())
    print(iq.auth.ssid())

    # --- binary -------------------------------------------------------
    iq.binary.set_amount(1)
    iq.binary.set_duration(1)            # minutes
    order = iq.binary.call()
    res = iq.binary.result(order)        # win / loss / equal
    print(res.result, res.pnl)

    # --- blitz --------------------------------------------------------
    iq.blitz.set_amount(1)
    iq.blitz.set_duration(30)            # seconds
    print(iq.blitz.durations())          # প্ল্যাটফর্ম যেসব expiry দেয়
    res = iq.blitz.trade_and_wait("put")
    print(res.result, res.pnl)

    # --- digital ------------------------------------------------------
    iq.digital.set_amount(2)
    order = iq.digital.put(duration=1)
    print(iq.digital.result(order).result)

    # --- forex --------------------------------------------------------
    iq.forex.set_amount(100)
    iq.forex.set_leverage(500)
    pos = iq.forex.buy(stop_loss=1.0500, take_profit=1.2000)
    iq.forex.set_sl_tp(pos.order_id, take_profit=1.2100)
    print(iq.forex.pnl(pos.order_id))
    iq.forex.close(pos.order_id)

    # --- data ---------------------------------------------------------
    iq.data.set_timeframe("M5")          # বা 300 (seconds)
    candles = iq.data.candles(count=100)
    iq.data.stream_candles(callback=lambda c: print(c.close))
    print(iq.data.price())
```

## Module reference

### `auth`
| Method | কাজ |
|---|---|
| `login(email, password)` | HTTPS login → SSID → websocket auth |
| `logout()` / `relogin()` | session শেষ / নতুন SSID |
| `ssid()` | বর্তমান session id |
| `balance()` / `currency()` / `balances()` | balance তথ্য |
| `set_account("PRACTICE"/"REAL")` | account type change |
| `use_practice()` / `use_real()` | shortcut |
| `account_type()` / `is_demo()` | কোন account-এ আছি |
| `set_symbol("EURUSD-OTC")` | সব module-এর default symbol set |

### `blitz`
`set_amount`, `set_duration` (সেকেন্ড), `durations`, `payout`, `is_open`,
`buy/call/put/place`, `track`, `result`, `trade_and_wait`, `open_trades`, `history`

### `binary`
`set_amount`, `set_duration` (মিনিট), `payout`, `is_open`,
`buy/call/put/place` (+`turbo=True`), `track`, `result`, `trade_and_wait`,
`open_trades`, `history`

### `digital`
`set_amount`, `set_duration` (মিনিট), `payout`, `strikes`,
`buy/call/put/place` (+ঐচ্ছিক `strike=`), `track`, `result`, `trade_and_wait`,
`close_early`, `open_trades`, `history`

### `forex`
`set_amount`, `set_leverage`, `leverages`, `pairs`, `price`, `bid_ask`,
`buy/sell` (`stop_loss=`, `take_profit=` absolute price),
`buy_pips/sell_pips` (`sl_pips=`, `tp_pips=`),
`set_sl_tp` / `set_stop_loss` / `set_take_profit` (open position-এ),
`track`, `position`, `pnl`, `open_trades`, `close`, `close_all`, `history`

### `data`
`set_timeframe("M1"/"M5"/"H1"/seconds)`, `timeframes`,
`candles`, `last_candle`, `stream_candles`,
`price`, `bid_ask`, `stream_ticks`, `traders_mood`,
`server_time`, `sync_time`

### `manager` (`IQAPI`)
`connect()`, `disconnect()`, `is_alive()`, `health()`, context manager
(`with IQAPI() as iq:`)। প্রতিটি module `iq.auth`, `iq.blitz`, `iq.binary`,
`iq.digital`, `iq.forex`, `iq.data` নামে হাতের কাছে।

## নিয়ম

* Symbol/amount/duration/timeframe একবার set করলে সব কলে default হিসেবে চলে,
  আবার প্রতি কলে override-ও করা যায়: `iq.binary.call(symbol="GBPUSD", amount=5)`।
* Credentials কখনো কোডে নয় — `IQ_EMAIL` / `IQ_PASSWORD` env, বা
  `IQAPI(email=..., password=...)`।
* Real account guard আগের মতোই `IQ_ALLOW_REAL` দিয়ে নিয়ন্ত্রিত।
