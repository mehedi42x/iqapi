"""api — bot-facing flat module system.

Modules
-------
``auth``     login, ssid, balance, account switch, symbol set
``blitz``    everything blitz options
``binary``   binary/turbo trading: place, call/put, track, result
``digital``  digital options: place, track, result
``forex``    forex: buy/sell, sl/tp, leverage, track, close
``data``     timeframe + candles/ticks/prices
``manager``  the one file that maintains every module (:class:`IQAPI`)

Usage::

    from iqoptionapi import IQAPI

    with IQAPI() as iq:                 # credentials from IQ_EMAIL / IQ_PASSWORD
        iq.auth.set_account("PRACTICE")
        iq.auth.set_symbol("EURUSD-OTC")
        iq.binary.set_amount(1)
        order = iq.binary.call()
        print(iq.binary.result(order))
"""

from .manager import IQAPI

__all__ = ["IQAPI"]
