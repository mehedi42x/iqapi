"""manager — the one file that maintains every api module.

:class:`IQAPI` wires up ``auth``, ``blitz``, ``binary``, ``digital``,
``forex`` and ``data`` on top of one shared :class:`~iq_option_api.IQOptionClient`
connection.  The bot talks to the modules; the modules talk to the platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from iq_option_api import IQConfig, IQOptionClient, load_config

from .auth import Auth
from .binary import Binary
from .blitz import Blitz
from .data import Data
from .digital import Digital
from .forex import Forex


@dataclass
class SharedState:
    """Defaults every module reads (symbol, amounts, durations, timeframe)."""

    symbol: str = "EURUSD"

    binary_amount: float = 1.0
    binary_duration: int = 1          # minutes

    digital_amount: float = 1.0
    digital_duration: int = 1         # minutes

    blitz_amount: float = 1.0
    blitz_duration: int = 30          # seconds

    forex_amount: float = 100.0
    forex_leverage: Optional[int] = None   # None -> platform default

    timeframe: int = 60               # seconds


class IQAPI:
    """Single entry point for the bot.

    ::

        from iqoptionapi import IQAPI

        iq = IQAPI()                       # credentials from IQ_EMAIL / IQ_PASSWORD
        iq.connect()
        iq.auth.set_account("PRACTICE")
        iq.auth.set_symbol("EURUSD-OTC")

        iq.binary.set_amount(1)
        order = iq.binary.call(duration=1)
        print(iq.binary.result(order).result)

        iq.disconnect()
    """

    def __init__(self, config: Optional[IQConfig] = None,
                 *, email: Optional[str] = None, password: Optional[str] = None,
                 **overrides: Any) -> None:
        if config is None:
            config = load_config(**overrides)
        if email or password:
            from iq_option_api import Credentials
            config.credentials = Credentials(
                email=email or config.credentials.email,
                password=password or config.credentials.password)

        self.client = IQOptionClient(config)
        self.state = SharedState(symbol=config.default_asset or "EURUSD")

        # --- the modules the bot talks to --------------------------------
        self.auth = Auth(self.client, self.state)
        self.blitz = Blitz(self.client, self.state)
        self.binary = Binary(self.client, self.state)
        self.digital = Digital(self.client, self.state)
        self.forex = Forex(self.client, self.state)
        self.data = Data(self.client, self.state)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def connect(self, **kwargs: Any) -> bool:
        """Connect + login + select the configured account."""
        return self.client.connect(**kwargs)

    def disconnect(self) -> None:
        self.client.close()

    close = disconnect

    def is_alive(self) -> bool:
        return self.client.is_connected and self.client.is_authenticated

    def health(self) -> dict:
        return self.client.health_check()

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------
    def __enter__(self) -> "IQAPI":
        if not self.client.is_connected:
            self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        return (f"IQAPI(connected={self.client.is_connected}, "
                f"account={self.auth.account_type() if self.client.is_authenticated else '-'}, "
                f"symbol={self.state.symbol})")
