from .browser import (  # noqa: F401
    CHROME_USER_AGENT,
    FIREFOX_USER_AGENT,
    impersonation_available,
    resolve_impersonate,
)
from .compat import patch_thread_is_alive, thread_is_alive  # noqa: F401

patch_thread_is_alive()
from .protocol import Protocol, RequestRegistry, build_message  # noqa: F401
from .subscription import Subscription, SubscriptionManager  # noqa: F401
from .websocket import ConnectionState, WebSocketClient  # noqa: F401

__all__ = [
    "WebSocketClient",
    "ConnectionState",
    "Protocol",
    "RequestRegistry",
    "build_message",
    "SubscriptionManager",
    "Subscription",
    "FIREFOX_USER_AGENT",
    "CHROME_USER_AGENT",
    "impersonation_available",
    "resolve_impersonate",
    "patch_thread_is_alive",
    "thread_is_alive",
]
