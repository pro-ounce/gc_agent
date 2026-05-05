"""
rbac/models.py
──────────────
User, Role, and APIKey dataclasses with Redis persistence.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.commons.logger import get_logger
from app.connections import redis_client, redis_get_json, redis_set_json

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── User ──────────────────────────────────────────────────────────────────────

@dataclass
class User:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    email: str = ""
    username: str = ""
    password_hash: str = ""
    roles: list[str] = field(default_factory=lambda: ["user"])
    is_active: bool = True
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self) -> None:
        self.updated_at = _now()
        redis_set_json(f"user:{self.id}", self.to_dict())
        redis_client.set(f"user:email:{self.email.lower()}", self.id)
        redis_client.set(f"user:username:{self.username.lower()}", self.id)

    @classmethod
    def find_by_id(cls, user_id: str) -> "User | None":
        data = redis_get_json(f"user:{user_id}")
        return cls(**data) if data else None

    @classmethod
    def find_by_email(cls, email: str) -> "User | None":
        raw = redis_client.get(f"user:email:{email.lower()}")
        if not raw:
            return None
        uid = raw.decode() if isinstance(raw, bytes) else raw
        return cls.find_by_id(uid)

    @classmethod
    def find_by_username(cls, username: str) -> "User | None":
        raw = redis_client.get(f"user:username:{username.lower()}")
        if not raw:
            return None
        uid = raw.decode() if isinstance(raw, bytes) else raw
        return cls.find_by_id(uid)

    # ── Password ──────────────────────────────────────────────────────────────

    def set_password(self, plain: str) -> None:
        import bcrypt
        self.password_hash = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

    def check_password(self, plain: str) -> bool:
        import bcrypt
        if not self.password_hash:
            return False
        return bcrypt.checkpw(plain.encode(), self.password_hash.encode())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "username": self.username,
            "password_hash": self.password_hash,
            "roles": self.roles,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_public_dict(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop("password_hash", None)
        return d


# ── API Key ───────────────────────────────────────────────────────────────────

@dataclass
class APIKey:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    key_hash: str = ""          # sha256(raw_key)
    user_id: str = ""
    name: str = ""
    roles: list[str] = field(default_factory=lambda: ["user"])
    is_active: bool = True
    created_at: str = field(default_factory=_now)
    last_used_at: str | None = None

    @classmethod
    def generate(cls, user_id: str, name: str, roles: list[str] | None = None) -> tuple["APIKey", str]:
        """Returns (APIKey instance, raw_key). Store only the hash."""
        raw = secrets.token_urlsafe(40)
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        obj = cls(
            user_id=user_id,
            key_hash=key_hash,
            name=name,
            roles=roles or ["user"],
        )
        obj.save()
        log.bind(func="APIKey.generate", user_id=user_id).info(f"API key '{name}' created")
        return obj, raw

    @classmethod
    def find_by_raw(cls, raw: str) -> "APIKey | None":
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        data = redis_get_json(f"apikey:hash:{key_hash}")
        if not data:
            return None
        obj = cls(**data)
        if not obj.is_active:
            return None
        return obj

    def save(self) -> None:
        redis_set_json(f"apikey:{self.id}", self.to_dict())
        redis_set_json(f"apikey:hash:{self.key_hash}", self.to_dict())

    def touch(self) -> None:
        self.last_used_at = _now()
        self.save()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key_hash": self.key_hash,
            "user_id": self.user_id,
            "name": self.name,
            "roles": self.roles,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


# ── Audit log ─────────────────────────────────────────────────────────────────

def audit(
    action: str,
    user_id: str | None,
    resource: str,
    detail: str = "",
    success: bool = True,
) -> None:
    from app.commons.logger import get_request_id
    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": _now(),
        "action": action,
        "user_id": user_id,
        "resource": resource,
        "detail": detail,
        "success": success,
        "request_id": get_request_id(),
    }
    redis_client.lpush("audit:log", str(entry))
    redis_client.ltrim("audit:log", 0, 9999)  # Keep last 10 000 entries
    log.bind(func="audit", action=action, resource=resource, user_id=user_id).info(
        f"AUDIT: {action} on {resource}"
    )
