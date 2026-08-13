"""Session object + on-disk SSID persistence."""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import SessionStoreConfig
from ..exceptions import SessionError


@dataclass
class Session:
    """Everything that identifies an authenticated connection."""

    ssid: Optional[str] = None
    user_id: Optional[int] = None
    email: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_validated_at: float = 0.0
    authenticated: bool = False
    max_age: float = 60 * 60 * 12
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    @property
    def is_expired(self) -> bool:
        if not self.ssid:
            return True
        return self.max_age > 0 and self.age > self.max_age

    @property
    def is_valid(self) -> bool:
        return bool(self.ssid) and self.authenticated and not self.is_expired

    def mark_authenticated(self, user_id: Optional[int] = None) -> None:
        self.authenticated = True
        self.last_validated_at = time.time()
        if user_id is not None:
            self.user_id = user_id

    def invalidate(self, reason: str = "") -> None:
        self.authenticated = False
        self.extra["invalidated_reason"] = reason

    def clear(self) -> None:
        self.ssid = None
        self.user_id = None
        self.authenticated = False
        self.extra.clear()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ssid": self.ssid,
            "user_id": self.user_id,
            "email": self.email,
            "created_at": self.created_at,
            "last_validated_at": self.last_validated_at,
            "max_age": self.max_age,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        return cls(
            ssid=data.get("ssid"),
            user_id=data.get("user_id"),
            email=data.get("email"),
            created_at=float(data.get("created_at", time.time())),
            last_validated_at=float(data.get("last_validated_at", 0.0)),
            max_age=float(data.get("max_age", 60 * 60 * 12)),
            extra=dict(data.get("extra", {})),
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        masked = f"{self.ssid[:6]}...{self.ssid[-4:]}" if self.ssid and len(self.ssid) > 12 else bool(self.ssid)
        return (f"Session(ssid={masked}, user_id={self.user_id}, "
                f"authenticated={self.authenticated}, age={self.age:.0f}s)")


class SessionStore:
    """Persists the SSID so a restart does not need a fresh login."""

    def __init__(self, config: Optional[SessionStoreConfig] = None) -> None:
        self.config = config or SessionStoreConfig()

    @property
    def path(self) -> Path:
        return Path(self.config.path)

    def save(self, session: Session) -> bool:
        if not self.config.enabled or not session.ssid:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)   # 0600, secret material
            return True
        except OSError as exc:
            raise SessionError(f"cannot persist session to {self.path}: {exc}") from exc

    def load(self, email: Optional[str] = None) -> Optional[Session]:
        if not self.config.enabled or not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        session = Session.from_dict(data)
        session.max_age = self.config.max_age
        if email and session.email and session.email != email:
            return None       # stored session belongs to a different user
        if session.is_expired:
            return None
        return session

    def clear(self) -> bool:
        try:
            if self.path.exists():
                self.path.unlink()
            return True
        except OSError:
            return False
