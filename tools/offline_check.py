"""Offline verification of the fixes for the five reported failures.

Drives the real client code against a fake gateway that replays the frame
shapes the platform actually sends, so the correlation, expiry, subscription
and payload logic can be checked without live credentials.

Run with::

    python tools/offline_check.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iq_option_api.connection.protocol import Protocol, RequestRegistry  # noqa: E402
from iq_option_api.connection.subscription import SubscriptionManager  # noqa: E402
from iq_option_api.connection.websocket import WebSocketClient  # noqa: E402
from iq_option_api.market.instruments import expiration_for  # noqa: E402
from iq_option_api.trading.option_events import option_matcher  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        detail = fn()
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  -- {detail}" if detail else ""))
    except Exception as exc:  # noqa: BLE001
        FAIL.append((name, exc))
        print(f"  FAIL  {name}  -- {type(exc).__name__}: {exc}")


class FakeGateway(WebSocketClient):
    """A WebSocketClient whose socket is a loopback into a scripted responder."""

    def __init__(self, responder):
        class _Cfg:
            request_timeout = 3.0
            host = "x"
            heartbeat_interval = 0
        self.config = _Cfg()
        self.protocol = Protocol()
        self.requests = RequestRegistry()
        self.subscriptions = SubscriptionManager()
        self.log = __import__("logging").getLogger("fake")
        self._send_lock = threading.Lock()
        self._raw_listeners = []
        self.messages_sent = 0
        self.last_error = None
        self._server_time = 0.0
        self._time_offset = 0.0
        self._responder = responder
        self.sent = []

    @property
    def is_connected(self):
        return True

    def send_frame(self, frame, *, request_id=None):
        if request_id is not None:
            frame["request_id"] = str(request_id)
        self.sent.append(frame)
        self.messages_sent += 1
        for reply in self._responder(frame) or []:
            threading.Timer(0.02, self._route, args=(reply,)).start()
        return str(frame.get("request_id", ""))


# ---------------------------------------------------------------------------
print("\n1. Expiry ladder (Fail 3/4 - unaligned 'expired' is dropped)")


def _expiry_whole_minute():
    now = 1_700_000_000.4          # 13:33:20 UTC
    ts, idx = expiration_for(1, now=now, ladder="turbo")
    assert ts % 60 == 0, f"expiry {ts} not on a whole minute"
    assert ts > now, "expiry in the past"
    assert idx < 5, f"turbo expiry must sit on the turbo ladder, got index {idx}"
    return f"turbo -> {int(ts)} (+{int(ts - now)}s, index {idx})"


def _expiry_guard():
    # 35s past the minute -> next minute is only 25s away (< 30s guard),
    # so the platform offers the one after.
    now = 1_700_000_000.0 - 20 + 60 * 0 + 55   # :55 of the minute
    ts, _ = expiration_for(1, now=now, ladder="turbo")
    assert ts - now > 30, f"expiry only {ts - now:.0f}s away, gateway would reject"
    return f"lead {ts - now:.0f}s respects the 30s guard"


def _expiry_binary_quarter():
    now = 1_700_000_000.0
    ts, idx = expiration_for(15, now=now, ladder="binary")
    assert idx >= 5, "binary must not use the turbo ladder"
    assert ts % 900 == 0, f"binary expiry {ts} not on a quarter hour"
    return f"binary -> {int(ts)} (quarter-hour, index {idx})"


check("turbo expiry is the next whole minute", _expiry_whole_minute)
check("expiry keeps a >30s lead", _expiry_guard)
check("binary expiry lands on a quarter hour", _expiry_binary_quarter)


# ---------------------------------------------------------------------------
print("\n2. Open-option correlation (Fail 3 - 'no response for request_id')")


def _echoed_request_id():
    def responder(frame):
        if frame.get("name") != "sendMessage":
            return []
        rid = frame["request_id"]
        return [{"name": "option", "request_id": rid,
                 "msg": {"id": 555, "active_id": 1, "direction": "call"}}]

    ws = FakeGateway(responder)
    payload = ws.call("binary-options.open-option",
                      {"price": 1, "active_id": 1, "expired": 0,
                       "direction": "call", "option_type_id": 3,
                       "user_balance_id": 7})
    assert payload["id"] == 555, payload
    sent = ws.sent[0]["msg"]
    assert sent["name"] == "binary-options.open-option", sent["name"]
    assert sent["version"] == "1.0", sent["version"]
    return "resolved via echoed request_id"


def _nested_request_id():
    """The failing case: request_id only inside msg."""
    def responder(frame):
        rid = frame["request_id"]
        return [{"name": "option",            # no envelope request_id
                 "msg": {"request_id": rid, "id": 777, "active_id": 1}}]

    ws = FakeGateway(responder)
    payload = ws.call("binary-options.open-option", {"active_id": 1})
    assert payload["id"] == 777, payload
    return "resolved via msg.request_id"


def _broadcast_option_opened():
    """The worst case: a pure broadcast with no request_id at all."""
    def responder(frame):
        return [{"name": "option-opened",
                 "msg": {"id": 999, "active_id": 76, "expired": 1700000060,
                         "direction": "put", "user_balance_id": 42}}]

    ws = FakeGateway(responder)
    matcher = option_matcher(active_id=76, expired=1700000060,
                             direction="put", balance_id=42)
    payload = ws.call("binary-options.open-option", {"active_id": 76},
                      matcher=matcher)
    assert payload["id"] == 999, payload
    return "resolved via field matcher"


def _broadcast_rejected():
    def responder(frame):
        return [{"name": "option-rejected",
                 "msg": {"active_id": 76, "user_balance_id": 42,
                         "message": "insufficient funds"}}]

    ws = FakeGateway(responder)
    matcher = option_matcher(active_id=76, balance_id=42)
    payload = ws.call("blitz-options.open-option", {"active_id": 76},
                      matcher=matcher)
    assert "insufficient" in payload["message"], payload
    return "rejection surfaces instead of a 25s timeout"


def _matcher_ignores_other_orders():
    m = option_matcher(active_id=76, expired=100, direction="call", balance_id=1)
    assert not m({"name": "option-opened",
                  "msg": {"active_id": 99, "expired": 100,
                          "direction": "call", "user_balance_id": 1}})
    assert not m({"name": "candle-generated", "msg": {"active_id": 76}})
    assert m({"name": "option-opened",
              "msg": {"active_id": 76, "expired": 100,
                      "direction": "call", "user_balance_id": 1, "id": 5}})
    return "foreign broadcasts are not stolen"


check("reply echoing request_id", _echoed_request_id)
check("reply with request_id nested in msg", _nested_request_id)
check("pure 'option-opened' broadcast", _broadcast_option_opened)
check("pure 'option-rejected' broadcast", _broadcast_rejected)
check("matcher rejects unrelated frames", _matcher_ignores_other_orders)


# ---------------------------------------------------------------------------
print("\n3. Digital quotes subscription (Fail 5 - event never received)")


def _digital_subscription_shape():
    import logging
    from iq_option_api.trading.digital import DigitalOptions

    class _Market:
        class assets:
            pass

        def asset_id(self, asset, itype=None):
            return 1

    captured = {}

    class _WS(FakeGateway):
        def subscribe(self, event_name, *, params=None, callback=None,
                      version=None, send_frame=True):
            captured["event"] = event_name
            captured["params"] = params
            captured["version"] = version
            captured["callback"] = callback
            return type("S", (), {"subscription_id": "s1"})()

    ws = _WS(lambda f: [])
    d = DigitalOptions.__new__(DigitalOptions)
    d.ws, d.market = ws, _Market()
    d._books, d._by_asset, d._subs = {}, {}, {}
    d._lock = threading.RLock()
    d.log = logging.getLogger("d")

    d.subscribe_prices(1, period=300)
    assert captured["event"] == "instrument-quotes-generated", captured["event"]
    assert captured["params"] == {"active": 1, "expiration_period": 300,
                                  "kind": "digital-option"}, captured["params"]
    assert captured["version"] == "1.2" or captured["version"] == "1.0"

    # a second period must get its own subscription, not reuse the first
    captured.clear()
    d.subscribe_prices(1, period=60)
    assert captured.get("params", {}).get("expiration_period") == 60, captured
    return "active + expiration_period + kind, one sub per period"


def _digital_ingest_and_instrument():
    import logging
    from iq_option_api.trading.digital import DigitalOptions

    d = DigitalOptions.__new__(DigitalOptions)
    d._books, d._by_asset, d._subs = {}, {}, {}
    d._lock = threading.RLock()
    d.log = logging.getLogger("d")

    book = d._ingest_price_event({"msg": {
        "active": 1,
        "expiration": {"period": 60, "timestamp": 1700000060},
        "quotes": [
            {"price": {"ask": 42.0},
             "symbols": ["doEURUSD202401151230PT1MCSPT",
                         "doEURUSD202401151230PT1MPSPT"]},
            {"price": {"ask": 80.0},
             "symbols": ["doEURUSD202401151230PT1MC11350481"]},
        ]}})
    assert book["asset_id"] == 1 and book["period"] == 60, book
    atm = book["strikes"]["SPT"]
    assert atm.instrument_id_call == "doEURUSD202401151230PT1MCSPT"
    assert abs(atm.profit_call - ((100 - 42) * 100) / 42) < 1e-9, atm.profit_call
    assert abs(book["strikes"]["11350481"].value - 11.350481) < 1e-9
    return f"{len(book['strikes'])} strikes, ATM payout {atm.profit_call:.1f}%"


check("subscribes to instrument-quotes-generated", _digital_subscription_shape)
check("quote frame -> strike book with instrument ids", _digital_ingest_and_instrument)


# ---------------------------------------------------------------------------
print("\n4. Top assets (Fail 1 - top-assets-info empty)")


def _top_assets_via_subscription():
    import logging
    from iq_option_api.market.market import MarketManager

    class _WS(FakeGateway):
        def __init__(self):
            super().__init__(lambda f: [])
            self.subs = []

        def subscribe(self, event_name, *, params=None, callback=None,
                      version=None, send_frame=True):
            self.subs.append((event_name, params, version))
            # the platform pushes the first frame right after subscribing
            threading.Timer(0.05, callback, args=({
                "name": "top-assets-updated",
                "msg": {"instrument_type": params["instrument_type"],
                        "data": [{"active_id": 1, "popularity": {"value": 10}},
                                 {"active_id": 76, "popularity": {"value": 5}}]},
            },)).start()
            return type("S", (), {"subscription_id": "s"})()

    m = MarketManager.__new__(MarketManager)
    m.ws = _WS()
    m.log = logging.getLogger("m")
    m._top_assets_cache, m._top_assets_subs = {}, {}

    data = m.top_assets("turbo", timeout=3)
    assert data, "no top assets returned"
    assert set(data) == {"1", "76"}, data
    event, params, version = m.ws.subs[0]
    assert event == "top-assets-updated", event
    assert params == {"instrument_type": "turbo-option"}, params
    assert version == "1.2", version

    # second call is served from cache, no extra subscription
    again = m.top_assets("turbo", timeout=3)
    assert again == data and len(m.ws.subs) == 1
    return f"turbo-option -> {len(data)} entries (v1.2 subscription)"


def _top_assets_wire_names():
    from iq_option_api.market.market import MarketManager
    from iq_option_api.models import InstrumentType
    w = MarketManager.wire_instrument_type
    assert w("turbo") == "turbo-option"
    assert w("binary") == "binary-option"
    assert w("digital-option") == "digital-option"
    assert w(InstrumentType.TURBO) == "turbo-option"
    return "turbo -> turbo-option, digital-option unchanged"


check("top_assets subscribes and waits for data", _top_assets_via_subscription)
check("instrument type names normalised", _top_assets_wire_names)


# ---------------------------------------------------------------------------
print("\n5. Instruments book (Fail 2 - get-instruments returned no entries)")


def _binary_book_from_init_data():
    import logging
    from iq_option_api.market.market import MarketManager
    from iq_option_api.models import Asset, InstrumentType

    calls = []

    class _Assets:
        def binary_assets(self, turbo=False):
            return [Asset(asset_id=1, name="EURUSD",
                          instrument_type=InstrumentType.TURBO if turbo
                          else InstrumentType.BINARY,
                          minimal_amount=1.0, maximal_amount=20000.0,
                          profit_percent=85.0)]

        def blitz_assets(self):
            return [Asset(asset_id=76, name="USDJPY")]

    class _WS(FakeGateway):
        def call(self, microservice, body, **kw):
            calls.append((microservice, body, kw.get("version")))
            return {"instruments": [{"id": "x"}]}

    m = MarketManager.__new__(MarketManager)
    m.ws = _WS(lambda f: [])
    m.assets = _Assets()
    m.log = logging.getLogger("m")
    m._top_assets_cache, m._top_assets_subs = {}, {}

    book = m.get_instruments("binary")
    assert book["instruments"], "binary book still empty"
    assert book["instruments"][0]["active_id"] == 1, book
    assert not calls, "binary must not hit get-instruments"

    assert m.get_instruments("turbo")["instruments"], "turbo book empty"
    assert m.get_instruments("blitz")["instruments"], "blitz book empty"

    # non-option types still use the microservice, now at v4.0
    m.get_instruments("crypto")
    assert calls and calls[0][0] == "get-instruments", calls
    assert calls[0][2] == "4.0", f"expected v4.0, got {calls[0][2]}"
    return "binary/turbo/blitz from init data; crypto via get-instruments v4.0"


check("binary/turbo book built from init data", _binary_book_from_init_data)


# ---------------------------------------------------------------------------
print("\n6. Payload shapes sent on the wire")


def _binary_body():
    import logging
    from iq_option_api.trading.binary import BinaryOptions
    from iq_option_api.models import Asset, Direction, InstrumentType

    sent = {}

    class _Orders:
        def create(self, **kw):
            from iq_option_api.models import Order, OrderState
            return Order(state=OrderState.CREATED, **{
                k: v for k, v in kw.items()
                if k in ("direction", "amount", "balance_id")})

        def validate(self, order, balance=None):
            return order

        def submit(self, order, ms, body, *, version="1.0", timeout=None, matcher=None):
            sent.update(microservice=ms, body=body, version=version,
                        has_matcher=matcher is not None)
            return order

    class _Market:
        class instruments:
            @staticmethod
            def expiration_for(minutes, ladder=None):
                from iq_option_api.models import Expiration
                ts, idx = expiration_for(minutes, now=1_700_000_000, ladder=ladder)
                return Expiration(timestamp=ts, period=minutes * 60, index=idx)

        def ensure_open(self, *a, **k):
            return True

        def get_asset(self, asset, itype=None):
            return Asset(asset_id=1, name="EURUSD", instrument_type=itype or
                         InstrumentType.TURBO, minimal_amount=1.0,
                         maximal_amount=20000.0, profit_percent=85.0)

    class _Accounts:
        user_balance_id = 42

    b = BinaryOptions.__new__(BinaryOptions)
    b.ws = None
    b.market = _Market()
    b.accounts = _Accounts()
    b.orders = _Orders()
    b.positions = None
    b.log = logging.getLogger("b")
    b.payout = lambda *a, **k: 85.0
    b._balance = lambda: 1000.0

    b.buy("EURUSD", 1.0, Direction.CALL, duration=1, turbo=True)

    body = sent["body"]
    assert sent["microservice"] == "binary-options.open-option", sent
    assert sent["version"] == "1.0", sent
    assert sent["has_matcher"], "no broadcast fallback registered"
    assert body["option_type_id"] == 3, body
    assert "type" not in body, "legacy 'type' field still sent"
    assert body["direction"] == "call", body
    assert body["expired"] % 60 == 0, body
    assert isinstance(body["expired"], int), body

    sent.clear()
    b.buy("EURUSD", 1.0, Direction.PUT, duration=15, turbo=False)
    assert sent["body"]["option_type_id"] == 1, sent["body"]
    return json.dumps(body, sort_keys=True)


def _digital_body():
    import logging
    from iq_option_api.trading.digital import DigitalOptions
    from iq_option_api.models import Direction, Expiration, Instrument, InstrumentType

    sent = {}

    class _Orders:
        def create(self, **kw):
            from iq_option_api.models import Order
            return Order(direction=kw["direction"], amount=kw["amount"],
                         balance_id=kw["balance_id"])

        def validate(self, order, balance=None):
            return order

        def submit(self, order, ms, body, *, version="1.0", timeout=None, matcher=None):
            sent.update(microservice=ms, body=body, version=version)
            return order

    class _Market:
        def ensure_open(self, *a, **k):
            return True

    class _Accounts:
        user_balance_id = 42

    d = DigitalOptions.__new__(DigitalOptions)
    d.market, d.accounts, d.orders = _Market(), _Accounts(), _Orders()
    d.log = logging.getLogger("d")
    d._lock = threading.RLock()
    d.get_instrument = lambda *a, **k: Instrument(
        instrument_id="doEURUSD202401151230PT1MCSPT", asset_id=1,
        instrument_type=InstrumentType.DIGITAL, direction=Direction.CALL,
        expiration=Expiration(timestamp=1700000060, period=60))
    d._balance = lambda: 1000.0

    d.buy("EURUSD", 1.0, Direction.CALL, duration=1)
    body = sent["body"]
    assert sent["microservice"] == "digital-options.place-digital-option", sent
    assert set(body) == {"user_balance_id", "instrument_id", "amount"}, body
    assert isinstance(body["amount"], str), body
    return json.dumps(body, sort_keys=True)


check("binary/turbo body matches the v1.0 contract", _binary_body)
check("digital body matches the v1.0 contract", _digital_body)


# ---------------------------------------------------------------------------
print()
print("=" * 70)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for name, exc in FAIL:
    print(f"  - {name}: {exc}")
sys.exit(1 if FAIL else 0)
