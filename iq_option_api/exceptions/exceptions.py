"""Exception hierarchy.

Every failure raised by the module derives from :class:`IQOptionError` so that
an application can catch a single base class, while still being able to react
to a specific failure mode (auth vs. market vs. order ...).

Note
----
``ConnectionError`` and ``TimeoutError`` shadow the builtins *inside this
package only*.  They also inherit from the builtins, so ``except
ConnectionError`` in user code keeps working.  The unambiguous aliases
``IQConnectionError`` / ``IQTimeoutError`` are provided as well.
"""

from __future__ import annotations

from typing import Any, Optional


class IQOptionError(Exception):
    """Base class of every error raised by the API module."""

    def __init__(self, message: str = "", *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.details is None:
            return self.message
        return f"{self.message} | details={self.details!r}"


# --------------------------------------------------------------------------
# Authentication / session
# --------------------------------------------------------------------------
class AuthenticationError(IQOptionError):
    """Login failed, wrong credentials, 2FA required/invalid."""


class TwoFactorRequired(AuthenticationError):
    """Server asked for a second authentication factor."""

    def __init__(self, message: str = "two factor authentication required",
                 *, token: Optional[str] = None, method: Optional[str] = None) -> None:
        super().__init__(message, details={"method": method})
        self.token = token
        self.method = method


class SessionError(IQOptionError):
    """SSID missing, expired, or rejected by the server."""


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------
class ConnectionError(IQOptionError, ConnectionError):  # type: ignore[misc]
    """WebSocket/network level failure."""


class TimeoutError(IQOptionError, TimeoutError):  # type: ignore[misc]
    """A request or an awaited event did not arrive in time."""


class ProtocolError(IQOptionError):
    """Malformed or unexpected server payload."""


# --------------------------------------------------------------------------
# Domain
# --------------------------------------------------------------------------
class AccountError(IQOptionError):
    """Account list / switching / verification failure."""


class BalanceError(IQOptionError):
    """Balance not found, insufficient funds, wrong ``user_balance_id``."""


class MarketError(IQOptionError):
    """Market closed or unavailable."""


class AssetError(IQOptionError):
    """Unknown or unsupported asset."""


class InstrumentError(IQOptionError):
    """Instrument could not be resolved (strike/expiration/instrument_id)."""


class OrderError(IQOptionError):
    """Order rejected, invalid, or not modifiable."""


class PositionError(IQOptionError):
    """Position not found or cannot be closed."""


class RiskError(IQOptionError):
    """Blocked by the local risk manager before hitting the server."""


class ConfigurationError(IQOptionError):
    """Invalid or incomplete configuration."""


# Unambiguous aliases -------------------------------------------------------
IQConnectionError = ConnectionError
IQTimeoutError = TimeoutError
