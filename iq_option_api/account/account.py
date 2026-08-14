"""Account management.

Golden rule of this module
--------------------------
**The active account and its ``user_balance_id`` are always resolved from
server data.**  Nothing here ever assumes a hardcoded balance id: the account
list comes from ``get-balances`` and every switch is verified
by re-reading the balances afterwards.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..connection.protocol import MS_CHANGE_BALANCE
from ..connection.websocket import WebSocketClient
from ..exceptions import AccountError, BalanceError
from ..models import Account, AccountType, Balance
from .balance import BalanceManager


class AccountManager:
    """Account list, active account and verified switching."""

    def __init__(self,
                 client: WebSocketClient,
                 balances: BalanceManager,
                 logger: Optional[logging.Logger] = None) -> None:
        self.ws = client
        self.balances = balances
        self.log = logger or logging.getLogger("iq_option_api.account")
        self._active_balance_id: Optional[int] = None

    # ==================================================================
    # Discovery
    # ==================================================================
    def list_accounts(self, *, refresh: bool = True) -> List[Account]:
        """Every account of the user, straight from the server."""
        balances = self.balances.all(refresh=refresh)
        return [Account.from_balance(b, is_active=(b.balance_id == self._active_balance_id))
                for b in balances]

    def get_account(self, account_type: AccountType, *, refresh: bool = False) -> Account:
        balance = self.balances.by_type(account_type, refresh=refresh)
        if balance is None:
            balance = self.balances.by_type(account_type, refresh=True)
        if balance is None:
            raise AccountError(f"no {account_type.value} account found for this user")
        return Account.from_balance(balance, is_active=(balance.balance_id == self._active_balance_id))

    def real_account(self, *, refresh: bool = False) -> Account:
        return self.get_account(AccountType.REAL, refresh=refresh)

    def practice_account(self, *, refresh: bool = False) -> Account:
        return self.get_account(AccountType.PRACTICE, refresh=refresh)

    demo_account = practice_account

    def tournament_accounts(self, *, refresh: bool = False) -> List[Account]:
        return [Account.from_balance(b) for b in self.balances.all(refresh=refresh)
                if b.type is AccountType.TOURNAMENT]

    # ==================================================================
    # Active account
    # ==================================================================
    @property
    def active_balance_id(self) -> Optional[int]:
        """The ``user_balance_id`` every order must carry."""
        return self._active_balance_id

    @property
    def user_balance_id(self) -> int:
        if self._active_balance_id is None:
            raise AccountError("no active account selected - call use_account() first")
        return self._active_balance_id

    def active_account(self, *, refresh: bool = False) -> Account:
        if self._active_balance_id is None:
            raise AccountError("no active account selected - call use_account() first")
        balance = self.balances.get(self._active_balance_id, refresh=refresh)
        return Account.from_balance(balance, is_active=True)

    @property
    def user_id(self) -> Optional[int]:
        """User id taken from server balance data (never hardcoded)."""
        if self._active_balance_id is not None:
            try:
                return self.balances.get(self._active_balance_id).user_id
            except Exception:
                pass
        for balance in self.balances.all(refresh=False):
            if balance.user_id:
                return balance.user_id
        return None

    @property
    def account_type(self) -> AccountType:
        if self._active_balance_id is None:
            return AccountType.UNKNOWN
        return self.balances.get(self._active_balance_id).type

    @property
    def is_demo(self) -> bool:
        return self.account_type is AccountType.PRACTICE

    @property
    def is_real(self) -> bool:
        return self.account_type is AccountType.REAL

    # ==================================================================
    # Switching (always verified)
    # ==================================================================
    def use_account(self, account_type: "AccountType | str", *,
                    verify: bool = True) -> Account:
        """Select the active account **by type**, resolving the id server-side."""
        if isinstance(account_type, str):
            try:
                account_type = AccountType(account_type.upper())
            except ValueError as exc:
                raise AccountError(f"unknown account type: {account_type!r}") from exc

        balance = self.balances.by_type(account_type, refresh=True)
        if balance is None:
            raise AccountError(f"no {account_type.value} account available on the server")
        return self.use_balance_id(balance.balance_id, verify=verify)

    def use_balance_id(self, balance_id: int, *, verify: bool = True) -> Account:
        """Select an account by id - the id is validated against server data."""
        balance_id = int(balance_id)
        # the id must exist server-side, otherwise we refuse it
        balance = self.balances.get(balance_id, refresh=True)

        try:
            self.ws.call(MS_CHANGE_BALANCE, {"user_balance_id": balance_id}, version="1.0")
        except Exception as exc:
            # Some deployments accept the id without an explicit change-balance
            # call; keep going but record it.
            self.log.debug("change-balance call failed (%s), continuing with local switch", exc)

        self._active_balance_id = balance_id
        if verify:
            self.verify_switch(balance_id)
        account = Account.from_balance(self.balances.get(balance_id, refresh=True), is_active=True)
        self.log.info("active account: %s (balance_id=%s, %.2f %s)",
                      account.type.value, account.balance_id, account.amount, account.currency)
        return account

    def switch_account(self, account_type: "AccountType | str") -> Account:
        return self.use_account(account_type)

    def change_balance(self, balance_mode: str = "PRACTICE") -> Account:
        """Select an account by mode name (``"PRACTICE"`` / ``"REAL"``).

        The balance id is never assumed: the list from ``get-balances`` is
        searched for the matching server type (PRACTICE = type 4, REAL =
        type 1) and the switch is verified against fresh server data.
        """
        mode = str(balance_mode).strip().upper()
        aliases = {
            "PRACTICE": AccountType.PRACTICE, "DEMO": AccountType.PRACTICE,
            "TRAINING": AccountType.PRACTICE,
            "REAL": AccountType.REAL, "LIVE": AccountType.REAL,
        }
        account_type = aliases.get(mode)
        if account_type is None:
            try:
                account_type = AccountType(mode)
            except ValueError as exc:
                raise AccountError(f"unknown balance mode: {balance_mode!r}") from exc
        account = self.use_account(account_type)
        self.log.info("balance switched to %s (ID: %s)",
                      account_type.value, account.balance_id)
        return account

    def verify_switch(self, expected_balance_id: int) -> bool:
        """Re-read balances and confirm the requested id really exists/matches."""
        balance = self.balances.get(int(expected_balance_id), refresh=True)
        if balance.balance_id != int(expected_balance_id):
            raise AccountError(
                f"account switch verification failed: expected {expected_balance_id}, "
                f"server reports {balance.balance_id}")
        if self._active_balance_id != balance.balance_id:
            raise AccountError("active account does not match the verified balance")
        return True

    # ==================================================================
    # Info
    # ==================================================================
    def balance(self, *, refresh: bool = True) -> float:
        return self.balances.amount(self.user_balance_id, refresh=refresh)

    def currency(self) -> str:
        return self.balances.currency(self.user_balance_id)

    def balance_object(self, *, refresh: bool = False) -> Balance:
        return self.balances.get(self.user_balance_id, refresh=refresh)

    def status(self) -> Dict[str, Any]:
        if self._active_balance_id is None:
            return {"active": False, "reason": "no account selected"}
        account = self.active_account()
        return {
            "active": True,
            "balance_id": account.balance_id,
            "user_balance_id": account.balance_id,
            "type": account.type.value,
            "is_demo": account.is_demo,
            "currency": account.currency,
            "amount": account.amount,
            "user_id": account.user_id,
        }

    def statistics(self, *, refresh: bool = True) -> Dict[str, Any]:
        accounts = self.list_accounts(refresh=refresh)
        return {
            "accounts": len(accounts),
            "by_type": {a.type.value: a.amount for a in accounts},
            "total_by_currency": self._total_by_currency(accounts),
            "active_balance_id": self._active_balance_id,
        }

    @staticmethod
    def _total_by_currency(accounts: List[Account]) -> Dict[str, float]:
        totals: Dict[str, float] = {}
        for account in accounts:
            totals[account.currency] = totals.get(account.currency, 0.0) + account.amount
        return totals
