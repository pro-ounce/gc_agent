"""
routers/auth.py
───────────────
Authentication endpoints:
  POST /api/auth/login          — username/password → JWT
  POST /api/auth/refresh        — refresh token → new access token
  GET  /api/auth/me             — current user profile
  POST /api/auth/keys           — create API key
  GET  /api/auth/keys           — list API keys
  DELETE /api/auth/keys/{id}    — revoke API key
  POST /api/auth/register       — register new user (admin or open, based on config)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from app.commons.config import cfg
from app.rbac.jwt_handler import JWTError, create_access_token, create_refresh_token, decode_token
from app.rbac.middleware import get_current_user, require_permission
from app.rbac.models import APIKey, User, audit
from app.rbac.permissions import Permissions

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Request / response models ─────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
    roles: list[str] = ["user"]


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = cfg.JWT_EXPIRE_MINUTES * 60


class CreateKeyRequest(BaseModel):
    name: str
    roles: list[str] = ["user"]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register", summary="Register a new user", status_code=201)
async def register(body: RegisterRequest) -> dict:
    if User.find_by_username(body.username):
        raise HTTPException(status_code=409, detail="Username already taken")
    if User.find_by_email(body.email):
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(username=body.username, email=body.email, roles=body.roles)
    user.set_password(body.password)
    user.save()
    audit("register", user.id, "user", f"username={body.username}")
    return {"id": user.id, "username": user.username, "roles": user.roles}


@router.post("/login", response_model=TokenResponse, summary="Login with username/password")
async def login(body: LoginRequest) -> TokenResponse:
    user = User.find_by_username(body.username)
    if not user or not user.check_password(body.password) or not user.is_active:
        audit("login_failed", None, "user", f"username={body.username}", success=False)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access = create_access_token({"sub": user.id, "roles": user.roles})
    refresh = create_refresh_token(user.id)
    audit("login", user.id, "user")
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenResponse, summary="Refresh access token")
async def refresh_token(body: dict) -> TokenResponse:
    token = body.get("refresh_token", "")
    try:
        payload = decode_token(token)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")
    user = User.find_by_id(payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    access = create_access_token({"sub": user.id, "roles": user.roles})
    refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.get("/me", summary="Get current user profile")
async def me(user: User = Depends(get_current_user)) -> dict:
    return user.to_public_dict()


@router.post("/keys", summary="Create an API key", status_code=201)
async def create_api_key(
    body: CreateKeyRequest,
    user: User = Depends(get_current_user),
) -> dict:
    api_key, raw = APIKey.generate(user_id=user.id, name=body.name, roles=body.roles)
    audit("create_api_key", user.id, "api_key", f"name={body.name}")
    return {
        "id": api_key.id,
        "name": api_key.name,
        "key": raw,  # Only shown once
        "roles": api_key.roles,
        "created_at": api_key.created_at,
    }


@router.get("/keys", summary="List API keys for current user")
async def list_api_keys(user: User = Depends(get_current_user)) -> dict:
    from app.connections import redis_client, redis_get_json

    raw_keys = redis_client.keys("apikey:*")
    keys = []
    seen: set[str] = set()
    for rk in raw_keys:
        k = rk.decode() if isinstance(rk, bytes) else rk
        if k.startswith("apikey:hash:"):
            continue
        data = redis_get_json(k)
        if data and data.get("user_id") == user.id and data["id"] not in seen:
            seen.add(data["id"])
            keys.append(
                {
                    "id": data["id"],
                    "name": data["name"],
                    "roles": data["roles"],
                    "created_at": data["created_at"],
                    "last_used_at": data.get("last_used_at"),
                    "is_active": data.get("is_active", True),
                }
            )
    return {"total": len(keys), "keys": keys}


@router.delete("/keys/{key_id}", summary="Revoke an API key")
async def revoke_api_key(
    key_id: str,
    user: User = Depends(get_current_user),
) -> dict:
    from app.connections import redis_get_json, redis_set_json

    data = redis_get_json(f"apikey:{key_id}")
    if not data or data.get("user_id") != user.id:
        raise HTTPException(status_code=404, detail="API key not found")
    data["is_active"] = False
    redis_set_json(f"apikey:{key_id}", data)
    redis_set_json(f"apikey:hash:{data['key_hash']}", data)
    audit("revoke_api_key", user.id, "api_key", f"key_id={key_id}")
    return {"revoked": True, "id": key_id}
