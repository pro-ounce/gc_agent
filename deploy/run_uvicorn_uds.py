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

import sys

import uvicorn


def _to_umask(mode: int) -> int:
    # umask blocks bits; it's the complement of the desired permission within 0777.
    return (~mode) & 0o777


def _detect_pkg(root: pathlib.Path) -> str:
    """Name of the app package (a sibling of this deploy/ dir). The shared deploy renames
    the repo's `app/` to `app_<name>/`, so it may be `app` here or `app_gc_agent` on the
    target. Pick the dir that is a Python package with main.py. Override with AGENT_PKG."""
    override = os.environ.get("AGENT_PKG", "").strip()
    if override:
        return override
    candidates = [
        c.name for c in sorted(root.iterdir())
        if c.is_dir() and (c / "main.py").is_file() and (c / "__init__.py").is_file()
    ]
    # Prefer the renamed package (app_<name>) over a leftover plain `app` if both are
    # present during the transition, so the deploy switches cleanly.
    for name in candidates:
        if name.startswith("app_"):
            return name
    return candidates[0] if candidates else "app"


def main() -> None:
    # Accept the AGENT_* names, or fall back to the plain HOST/PORT/WORKERS the supervisor
    # conf already sets — so switching to this runner needs no env changes.
    workers = int(os.environ.get("AGENT_WORKERS") or os.environ.get("WORKERS") or "2")
    log_level = os.environ.get("LOG_LEVEL", "info")

    # Build the app once, in the factory — not on package import.
    os.environ["APP_SKIP_INIT"] = "1"

    # Ensure the checkout root (parent of deploy/) is importable — running this file as a
    # script puts deploy/ on sys.path, not the root — so `{pkg}.main` resolves. PYTHONPATH
    # carries it to uvicorn's worker subprocesses too.
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    os.environ["PYTHONPATH"] = str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")
    target = f"{_detect_pkg(root)}.main:create_app"

    host = (os.environ.get("AGENT_HOST") or os.environ.get("HOST") or "").strip()
    port = (os.environ.get("AGENT_PORT") or os.environ.get("PORT") or "").strip()
    if host and port:
        # TCP mode — what the Spring gateway proxies to (localhost:17024).
        uvicorn.run(target, factory=True, host=host, port=int(port),
                    workers=workers, log_level=log_level, log_config=None, access_log=False,
                    proxy_headers=False)
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
            target,
            factory=True,
            uds=uds_path,
            workers=workers,
            log_level=log_level,
            log_config=None,  # use the app's structured logger
            access_log=False,
            proxy_headers=False,  # trust the direct peer (Apache/gateway hop) for _guard IP checks
        )
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    main()
