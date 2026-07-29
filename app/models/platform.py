"""
models/platform.py
──────────────────
The GC platform's standard response envelope (mirrors the Java ApiResponse<T>):
    { success, message, statusCode, data, errors }

The FE's shared API client expects this shape for every /api/* call, so the
platform-facing agent endpoints wrap their payloads in it. Responses stay
PLAINTEXT with header `Encrypted: false` — the platform only encrypts
browser-facing responses that DON'T carry X-INT-TKN (see commons
EncryptResponseAdvice), and the agent always runs behind the gateway with one.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    success: bool
    message: str = ""
    statusCode: int = 200
    data: Any = None
    errors: list[str] = Field(default_factory=list)

    @classmethod
    def ok(cls, data: Any = None, message: str = "", status_code: int = 200) -> "ApiResponse":
        return cls(success=True, message=message, statusCode=status_code, data=data, errors=[])

    @classmethod
    def fail(
        cls, message: str, status_code: int = 500, errors: list[str] | None = None
    ) -> "ApiResponse":
        return cls(
            success=False,
            message=message,
            statusCode=status_code,
            data=None,
            errors=errors or [message],
        )
