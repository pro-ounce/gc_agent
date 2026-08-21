#!/usr/bin/env python3
"""
Run the GC Agent on a Unix domain socket (UDS) — the entry point the shared deployment
process invokes (same convention as the delivery / rag apps).

Resolves and prepares the socket path, removes any stale socket, sets a permissive umask
so the socket is group-readable, sets APP_SKIP_INIT=1 (so the app is built once — by the
factory, not the package import), then starts Uvicorn on the factory app
`app.main:create_app`.

Serves over a UDS by default (Apache/nginx-socket front, like delivery/rag). Because the
GC Agent is normally reached over TCP by the Spring gateway, it ALSO supports TCP: set
AGENT_HOST + AGENT_PORT and it binds those instead of the socket.

Env vars:
  AGENT_HOST       bind host for TCP mode (e.g. 0.0.0.0). If set with AGENT_PORT → TCP.
  AGENT_PORT       bind port for TCP mode (e.g. 17024). If set with AGENT_HOST → TCP.
  AGENT_UDS_PATH   absolute socket path when NOT in TCP mode (default: /tmp/agent.sock)
  AGENT_UDS_MODE   octal permissions for the socket (default: 660)
  AGENT_WORKERS    uvicorn worker count (default: 2)
  LOG_LEVEL        uvicorn log level (default: info)
"""
import os
import pathlib
import stat

import uvicorn


def _to_umask(mode: int) -> int:
    # umask blocks bits; it's the complement of the desired permission within 0777.
    return (~mode) & 0o777


def main() -> None:
    workers = int(os.environ.get("AGENT_WORKERS", "2") or "2")
    log_level = os.environ.get("LOG_LEVEL", "info")

    # Build the app once, in the factory — not on package import.
    os.environ["APP_SKIP_INIT"] = "1"

    host = os.environ.get("AGENT_HOST", "").strip()
    port = os.environ.get("AGENT_PORT", "").strip()
    if host and port:
        # TCP mode — what the Spring gateway proxies to (localhost:17024).
        uvicorn.run("app.main:create_app", factory=True, host=host, port=int(port),
                    workers=workers, log_level=log_level, log_config=None)
        return

    uds_path = os.environ.get("AGENT_UDS_PATH", "/tmp/agent.sock")
    try:
        uds_mode = int(os.environ.get("AGENT_UDS_MODE", "660").strip(), 8)
    except Exception:
        uds_mode = 0o660

    p = pathlib.Path(uds_path)
    try:
        if p.exists():
            if stat.S_ISSOCK(p.lstat().st_mode):
                p.unlink()
            else:
                raise RuntimeError(f"Path exists and is not a socket: {uds_path}")
        p.parent.mkdir(parents=True, exist_ok=True)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[run_uvicorn_uds] Failed preparing UDS path: {exc}")
        raise

    old_umask = os.umask(_to_umask(uds_mode))
    try:
        uvicorn.run(
            "app.main:create_app",
            factory=True,
            uds=uds_path,
            workers=workers,
            log_level=log_level,
            log_config=None,  # use the app's structured logger
        )
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    main()
