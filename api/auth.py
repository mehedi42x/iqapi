"""auth — login, ssid, balance, account type change, symbol set, account set.

The bot asks, this module delivers.  Nothing else lives here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class Auth:
    """Session + account control for the bot."""

    def __init__(self, client: Any, state: Any) -> None:
        self._iq = client          # IQOptionClient
        self._state = state        # shared SharedState (symbol, amount, ...)

    # ------------------------------------------------------------------
    # login / session
    # ------------------------------------------------------------------
    def login(self, email: Optional[str] = None, password: Optional[str] = None,
              *, force: bool = False, two_factor_code: Optional[str] = None) -> bool:
        """Login (HTTPS -> SSID -> websocket).  Credentials optional if in env."""
        return self._iq.login(email, password, force=force,
                              two_factor_code=two_factor_code)

    def logout(self) -> None:
        self._iq.logout()

    def relogin(self) -> bool:
        return self._iq.auth.relogin()

    def ssid(self) -> Optional[str]:
        """The current session id."""
        return self._iq.ssid

    def is_connected(self) -> bool:
        return self._iq.is_connected

    def is_logged_in(self) -> bool:
        return self._iq.is_authenticated

    def profile(self, *, refresh: bool = False) -> Dict[str, Any]:
        return self._iq.get_profile(refresh=refresh)

    def session_status(self) -> Dict[str, Any]:
        return self._iq.session_status()

    # ------------------------------------------------------------------
    # balance
    # ------------------------------------------------------------------
    def balance(self, *, refresh: bool = True) -> float:
        return self._iq.balance(refresh=refresh)

    def currency(self) -> str:
        return self._iq.currency()

    def balances(self) -> List[Any]:
        """Every account/balance of the user, from the server."""
        return self._iq.list_accounts(refresh=True)

    # ------------------------------------------------------------------
    # account type change  (PRACTICE / REAL)
    # ------------------------------------------------------------------
    def set_account(self, mode: str = "PRACTICE") -> Any:
        """Switch account: ``"PRACTICE"`` / ``"DEMO"`` / ``"REAL"``."""
        return self._iq.change_balance(str(mode).upper())

    change_account = set_account

    def use_practice(self) -> Any:
        return self._iq.use_practice()

    use_demo = use_practice

    def use_real(self) -> Any:
        return self._iq.use_real()

    def account_type(self) -> str:
        return str(self._iq.account_type.value)

    def is_demo(self) -> bool:
        return self._iq.is_demo

    def balance_id(self) -> Optional[int]:
        return self._iq.accounts.active_balance_id

    # ------------------------------------------------------------------
    # symbol set  (default asset every module trades on)
    # ------------------------------------------------------------------
    def set_symbol(self, symbol: str) -> str:
        """Set the default symbol for every trading module (e.g. ``EURUSD-OTC``)."""
        self._state.symbol = str(symbol).upper()
        return self._state.symbol

    def get_symbol(self) -> str:
        return self._state.symbol

    symbol = get_symbol
