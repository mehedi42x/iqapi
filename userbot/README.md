# IQ Option Userbot

মডিউলার ট্রেডিং বট — **স্ট্র্যাটেজি শুধু সিগন্যাল দেয়**, বাকি সব (`login`, ক্যান্ডেল, রিস্ক, অর্ডার, রেজাল্ট) `core.py` হ্যান্ডেল করে। কোথাও আটকে থাকার কথা নয়: প্রতিটা wait চাঙ্ক করা, মার্কেট-ডাটায় retry, অর্ডার কখনো অন্ধভাবে রিট্রাই হয় না।

A modular trader on top of `iq_option_api`. Strategies are plugins. Core owns the broker session.

```
userbot/
├── .env / .env.example     ← email, asset, timeframe, amount, strategy, risk
├── bot.py                  ← live console + instructions
├── core.py                 ← API, risk, money, execution (the only place that trades)
├── backtest.py             ← 1d / 1w / 1m / 6m / 1y walk-forward
└── strategies/             ← drop a .py here, it becomes a module
    ├── binary1.py          binary 1m triple confluence
    ├── binary_sniper.py    binary 1m price-action sniper
    ├── digital1.py         digital 1m regime-shift
    ├── digital_ai.py       digital AI ensemble + online learning
    ├── blitz_flash.py      blitz 5–30s momentum
    ├── blitz_snap.py       blitz fade / snap
    ├── gold_scalp.py       XAUUSD VWAP fade
    ├── gold_breakout.py    XAUUSD squeeze breakout
    ├── gold_impulse.py     XAUUSD impulse-pullback
    └── gold_session.py     XAUUSD session sweep (London / NY)
```

---

## Setup

রিপোর রুট থেকে:

```bash
python3 -m venv .venv
.venv/bin/pip install -r userbot/requirements.txt
cp userbot/.env.example userbot/.env
# edit userbot/.env  →  IQ_EMAIL / IQ_PASSWORD
```

Login is HTTPS-first (SSID cookie on the websocket handshake), matching the
standalone snippet that works on Termux.  `curl_cffi` is **optional** — it
impersonates Firefox's TLS fingerprint when Cloudflare is strict.  On
Python 3.13+ / Termux an old `websocket-client` used to crash with
`'Thread' object has no attribute 'isAlive'`; the bot now avoids that path.

```bash
pip install -U 'websocket-client>=1.6' requests
pip install 'curl_cffi>=0.7'   # optional
```

```bash
python livetest.py              # PRACTICE smoke test + $1 trades
python livetest.py --no-trade   # connect / candles / payout only
```

`.env` এ যা সেট করা যায়:

| Key | অর্থ |
|---|---|
| `IQ_EMAIL` / `IQ_PASSWORD` | লগইন — কোডে কখনো hardcode হয় না |
| `IQ_ACCOUNT_MODE` | `PRACTICE` বা `REAL` |
| `IQ_ALLOW_REAL` | রিয়েল অ্যাকাউন্টে ট্রেড করতে `true` লাগবেই |
| `ASSET` | `EURUSD`, `XAUUSD`, `GOLD`, `EURUSD-OTC`, ... |
| `OTC_FALLBACK` | স্পট বন্ধ থাকলে `ASSET-OTC` অটো-ট্রাই |
| `TIMEFRAME` | `1m` `5m` `15m` `1h` বা সেকেন্ড |
| `TRADE_TYPE` | `binary` / `turbo` / `digital` / `blitz` |
| `DURATION` | binary/digital = মিনিট, blitz = সেকেন্ড (`5/10/15/30/60`) |
| `STRATEGY` | মডিউল নাম, `auto`, অথবা কাস্টম `.py` পাথ |
| `AMOUNT` / `MM_MODE` | `fixed` · `percent` · `martingale` · `anti_martingale` |
| `MIN_CONFIDENCE` / `MIN_PAYOUT` | দুর্বল সিগন্যাল / লো পেআউট স্কিপ |
| `MAX_DAILY_LOSS` / `MAX_DAILY_PROFIT` / `MAX_TRADES` / `MAX_CONSECUTIVE_LOSSES` | সেশন গার্ড |
| `DRY_RUN` | সিগন্যাল প্রিন্ট, অর্ডার যাবে না |
| `TRADE_ON_CLOSE` | ক্যান্ডেল ক্লোজ না হওয়া পর্যন্ত অপেক্ষা (রিকমেন্ডেড) |

---

## Run

```bash
cd userbot

python bot.py                     # লাইভ লুপ
python bot.py --dry-run           # সিগন্যাল-অনলি
python bot.py --strategy digital_ai --asset XAUUSD
python bot.py --list              # ইনস্টল করা স্ট্র্যাটেজি

python backtest.py                # রেঞ্জ জিজ্ঞেস করবে: 1d 1w 1m 6m 1y
python backtest.py --range 1w --strategy gold_impulse --asset XAUUSD
```

রিপো রুট থেকেও চালানো যায়: `python -m userbot` / `python userbot/bot.py`।

`backtest.py` চালালে প্রথমে ডেটা রেঞ্জ চাইবে। Enter দিলে সেই উইন্ডোর ক্যান্ডেল `core.py` দিয়ে নামবে (`.env` এর symbol / timeframe / amount / payout), তারপর স্ট্র্যাটেজি বার-বাই-বার সিমুলেট হবে। Win rate, PnL, profit factor, max drawdown, consecutive losses রিপোর্ট করবে। চাইলে JSON `userbot/data/` তে সেভ হয়।

---

## Strategies

প্রতিটা ট্রেড টাইপের জন্য **২টা** মডিউল, গোল্ড/XAUUSD এর জন্য **৪টা** স্ক্যাল্প:

| Module | For | Idea |
|---|---|---|
| `binary1` | binary 1m | EMA 9/21/50 + RSI recovery + stochastic + impulse candle |
| `binary_sniper` | binary 1m | pin / engulf at swing or EMA21, ATR floor |
| `digital1` | digital 1m | Supertrend flip + MACD hist + BB expansion + ADX |
| `digital_ai` | digital 1m | 14-feature ensemble, trend/range regime, online weight update |
| `blitz_flash` | blitz 5–30s | EMA 3/8 + RSI5 + Heikin-Ashi run |
| `blitz_snap` | blitz 5–15s | fade a 4-bar micro-extension on a rejection wick |
| `gold_scalp` | XAUUSD | VWAP fade, RSI(7) extreme, 0.6–1.4 ATR band |
| `gold_breakout` | XAUUSD | Donchian-20 *close* break after a BB squeeze, rising ADX |
| `gold_impulse` | XAUUSD | EMA 8/21/55 stack → pullback kiss → resume bar |
| `gold_session` | XAUUSD | Asia/London sweep + London/NY killzone reversal |

`STRATEGY=auto` হলে: গোল্ড অ্যাসেটে `gold_impulse`, digital এ `digital1`, blitz এ `blitz_flash`, নাহলে `binary1`।

`digital_ai` ওয়েট `userbot/data/ai_weights.json` এ সেভ হয় — যত ট্রেড সেটেল হবে, মডেল তত অ্যাডাপ্ট করবে।

---

## নিজের স্ট্র্যাটেজি (custom module)

`strategies/_template.py` কপি করে `strategies/my_strategy.py` বানান:

```python
from .base import Strategy, Signal
from . import indicators as ta

class MyStrategy(Strategy):
    name = "my_strategy"
    description = "RSI bounce"
    instrument = "binary"
    min_candles = 50

    def analyze(self, candles, context):
        rsi = ta.last(ta.rsi(ta.closes(candles), 14))
        if rsi is None:
            return Signal.hold("warmup")
        if rsi < 28:
            return Signal.call(0.72, f"rsi {rsi:.0f}")
        if rsi > 72:
            return Signal.put(0.72, f"rsi {rsi:.0f}")
        return Signal.hold(f"rsi={rsi:.0f}")
```

`.env` এ `STRATEGY=my_strategy` — অথবা বাইরের ফাইল: `STRATEGY=/home/you/strats/foo.py`।

`analyze` শুধু `Signal` ফেরত দেবে। অর্ডার, সকেট, `.env` — কিছুই স্পর্শ করবে না। ক্র্যাশ হলে core সেটাকে `hold` বানিয়ে লুপ চালিয়ে যাবে।

`context` এ থাকে: `asset`, `timeframe`, `server_time`, `htf_candles` (5× TF), `payout`, `instrument`, `price`।

---

## Safety

- REAL ট্রেড করতে `IQ_ACCOUNT_MODE=REAL` **এবং** `IQ_ALLOW_REAL=true` দুটোই লাগে।
- মার্কেট ক্লোজ / লো পেআউট / লো কনফিডেন্স = স্কিপ, আটকে থাকে না।
- কানেকশন মারা গেলে অটো-রিকানেক্ট (ম্যাক্স ৭ বার, backoff)।
- `Ctrl+C` যেকোনো wait থেকে বের হয়।
- একই অর্ডার টাইমআউট হলে রিট্রাই হয় **না** (ডাবল-স্পেন্ড রোধ)।
- সেশন লিমিট ভাঙলে লুপ নিজে থেকে থামে এবং সামারি প্রিন্ট করে।

---

## Architecture

```
.env  →  bot.py / backtest.py  →  core.py  →  iq_option_api
                                   ↑
                            strategies/*.py   (Signal only)
```

`core.py` ই IQ Option ক্লায়েন্ট খোলে, অ্যাকাউন্ট সিলেক্ট করে, ক্যান্ডেল পেজ করে, HTF ফিড দেয়, পেআউট চেক করে, মানি-ম্যানেজমেন্ট অ্যাপ্লাই করে, অর্ডার পাঠায়, রেজাল্ট ধরে `strategy.on_result` কল করে।
