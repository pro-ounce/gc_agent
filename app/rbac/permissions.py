"""
rbac/permissions.py
───────────────────
Flat permission constants used across the application.
"""
from __future__ import annotations


class Permissions:
    # Chat
    CHAT_SEND = "chat:send"
    CHAT_STREAM = "chat:stream"
    CHAT_HISTORY = "chat:history"

    # Sessions
    SESSION_READ = "session:read"
    SESSION_DELETE = "session:delete"
    SESSION_LIST = "session:list"

    # Tools
    TOOL_LIST = "tool:list"
    TOOL_EXECUTE = "tool:execute"        # Low risk
    TOOL_EXECUTE_ELEVATED = "tool:execute:elevated"  # Medium/High risk confirm

    # Prompts
    PROMPT_LIST = "prompt:list"
    PROMPT_EXECUTE = "prompt:execute"

    # Admin
    ADMIN_USERS = "admin:users"
    ADMIN_KEYS = "admin:keys"
    ADMIN_SESSIONS = "admin:sessions"


# Role → permissions mapping
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "viewer": [
        Permissions.CHAT_SEND,
        Permissions.CHAT_HISTORY,
        Permissions.SESSION_READ,
        Permissions.TOOL_LIST,
        Permissions.PROMPT_LIST,
    ],
    "user": [
        Permissions.CHAT_SEND,
        Permissions.CHAT_STREAM,
        Permissions.CHAT_HISTORY,
        Permissions.SESSION_READ,
        Permissions.SESSION_DELETE,
        Permissions.TOOL_LIST,
        Permissions.TOOL_EXECUTE,
        Permissions.PROMPT_LIST,
        Permissions.PROMPT_EXECUTE,
    ],
    "operator": [
        Permissions.CHAT_SEND,
        Permissions.CHAT_STREAM,
        Permissions.CHAT_HISTORY,
        Permissions.SESSION_READ,
        Permissions.SESSION_DELETE,
        Permissions.SESSION_LIST,
        Permissions.TOOL_LIST,
        Permissions.TOOL_EXECUTE,
        Permissions.TOOL_EXECUTE_ELEVATED,
        Permissions.PROMPT_LIST,
        Permissions.PROMPT_EXECUTE,
    ],
    "admin": [
        # All permissions
        Permissions.CHAT_SEND,
        Permissions.CHAT_STREAM,
        Permissions.CHAT_HISTORY,
        Permissions.SESSION_READ,
        Permissions.SESSION_DELETE,
        Permissions.SESSION_LIST,
        Permissions.TOOL_LIST,
        Permissions.TOOL_EXECUTE,
        Permissions.TOOL_EXECUTE_ELEVATED,
        Permissions.PROMPT_LIST,
        Permissions.PROMPT_EXECUTE,
        Permissions.ADMIN_USERS,
        Permissions.ADMIN_KEYS,
        Permissions.ADMIN_SESSIONS,
    ],
}


def get_permissions_for_roles(roles: list[str]) -> set[str]:
    perms: set[str] = set()
    for role in roles:
        perms.update(ROLE_PERMISSIONS.get(role, []))
    return perms
