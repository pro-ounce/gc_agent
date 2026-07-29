"""
tests/test_observability.py
───────────────────────────
Actuator-style health/info/prometheus (matching the gc Spring services) + aliases.
"""
import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("RBAC_ENABLED", "false")
os.environ.setdefault("STORE_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def test_actuator_liveness():
    r = client.get("/actuator/health/liveness")
    assert r.status_code == 200
    assert r.json()["status"] == "UP"


def test_actuator_health_spring_shape():
    r = client.get("/actuator/health")
    body = r.json()
    # Spring actuator shape: status + components{name:{status[,details]}}
    assert body["status"] in {"UP", "DOWN"}
    assert "components" in body
    for dep in ("store", "mcp", "llm"):
        assert dep in body["components"]
        assert "status" in body["components"][dep]
    # store is in-memory -> UP; mcp/llm down in tests -> aggregate DOWN + 503 (worst-of)
    assert body["components"]["store"]["status"] == "UP"


def test_actuator_readiness_503_when_mcp_down():
    r = client.get("/actuator/health/readiness")
    assert r.status_code == 503
    assert r.json()["status"] == "OUT_OF_SERVICE"


def test_actuator_info():
    r = client.get("/actuator/info")
    assert r.status_code == 200
    assert r.json()["app"]["name"]


def test_actuator_prometheus_micrometer_names():
    r = client.get("/actuator/prometheus")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # micrometer-compatible HTTP metric + agent domain metrics
    assert "http_server_requests_seconds" in body
    for family in (
        "agent_llm_calls_total",
        "agent_tool_executions_total",
        "agent_mcp_requests_total",
        "agent_chat_requests_total",
        "agent_dependency_up",
        "agent_build_info",
    ):
        assert family in body, f"missing metric family: {family}"


def test_metrics_alias():
    assert client.get("/metrics").status_code == 200


def test_api_health_alias():
    r = client.get("/api/health")
    assert "components" in r.json()
