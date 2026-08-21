"""Application package — GC Agent.

Entry points (same convention as the delivery / rag apps, so the shared deployment
process works unchanged):
- create_app: factory used by the UDS runner — `uvicorn app.main:create_app --factory`
  (see deploy/run_uvicorn_uds.py). The runner sets APP_SKIP_INIT=1 so the app is built
  once, by the factory.
- app: eager ASGI instance for legacy `uvicorn app:app` / `uvicorn app.main:app`. Skipped
  when APP_SKIP_INIT=1 (factory/UDS deploys, and unit tests that build their own app).
"""
import os

from .main import create_app  # noqa: F401 — factory is always exported

if os.environ.get("APP_SKIP_INIT") != "1":
    # main.py builds the eager instance (also guarded there); re-export it so both
    # `app:app` and `app.main:app` resolve to the same single application.
    from .main import app  # noqa: F401
