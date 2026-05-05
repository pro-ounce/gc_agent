# Re-export the ASGI app so `uvicorn app:app` works alongside `uvicorn app.main:app`.
from app.main import app  # noqa: F401
