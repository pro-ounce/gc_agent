"""
tests/test_opensearch_store.py
──────────────────────────────
Verify the OpenSearch-backed store implements the Redis-compatible interface
(get/set/delete/exists/expire/keys + list ops) using a fake OpenSearch client,
so no live cluster is needed in CI.
"""
import json
import time

import pytest
from opensearchpy.exceptions import NotFoundError

from app.connections import _OpenSearchStore


class FakeOS:
    """Minimal in-memory stand-in for the opensearch-py client."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self._indices: set[str] = set()

    class _Indices:
        def __init__(self, outer):
            self._o = outer

        def exists(self, index):
            return index in self._o._indices

        def create(self, index, body=None):
            self._o._indices.add(index)

    @property
    def indices(self):
        return FakeOS._Indices(self)

    def index(self, index, id, body):
        self.docs[id] = dict(body)

    def get(self, index, id):
        if id not in self.docs:
            raise NotFoundError(404, "not_found", {})
        return {"_source": self.docs[id]}

    def delete(self, index, id):
        if id not in self.docs:
            raise NotFoundError(404, "not_found", {})
        del self.docs[id]

    def update(self, index, id, body):
        if id not in self.docs:
            raise NotFoundError(404, "not_found", {})
        self.docs[id].update(body.get("doc", {}))

    def search(self, index, body):
        return {"hits": {"hits": [
            {"_id": k, "_source": {"expires_at": v.get("expires_at")}}
            for k, v in self.docs.items()
        ]}}

    def ping(self):
        return True

    def close(self):
        pass


@pytest.fixture
def store():
    return _OpenSearchStore(FakeOS(), "agent-kv-test")


def test_set_get_json_roundtrip(store):
    store.set("session:1", json.dumps({"a": 1}), ex=3600)
    raw = store.get("session:1")
    assert isinstance(raw, bytes)
    assert json.loads(raw) == {"a": 1}


def test_plain_string(store):
    store.set("user:email:x@y.com", "uid-1")
    assert store.get("user:email:x@y.com").decode() == "uid-1"


def test_exists_and_delete(store):
    store.set("k", "v")
    assert store.exists("k") == 1
    assert store.delete("k") == 1
    assert store.get("k") is None
    assert store.exists("k") == 0


def test_ttl_lazy_expiry(store):
    store.set("temp", "v", ex=1)
    # force-expire the underlying doc
    store._c.docs["temp"]["expires_at"] = time.time() - 5
    assert store.get("temp") is None


def test_keys_pattern(store):
    store.set("session:a", "1")
    store.set("session:b", "2")
    store.set("other:c", "3")
    assert sorted(k.decode() for k in store.keys("session:*")) == ["session:a", "session:b"]


def test_expire(store):
    store.set("ex1", "v")
    assert store.expire("ex1", 100) is True
    assert store.expire("missing", 100) is False


def test_list_ops(store):
    store.lpush("audit:log", "e1")
    store.lpush("audit:log", "e2")
    store.lpush("audit:log", "e3")
    assert json.loads(store.get("audit:log")) == ["e3", "e2", "e1"]  # newest first
    store.ltrim("audit:log", 0, 1)
    assert json.loads(store.get("audit:log")) == ["e3", "e2"]


def test_ping(store):
    assert store.ping() is True
