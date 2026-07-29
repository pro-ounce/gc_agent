#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# validate-opensearch.sh — end-to-end health check for the agent's OpenSearch use.
# Validates: reachability, cluster health, k-NN plugin (needed for tool-RAG),
# the agent-kv store index, a live k-NN round-trip, the agent's own store health,
# and the embedding model tool-RAG relies on.
#
#   OS_URL=http://localhost:17080 OS_USER=admin OS_PASS='xxx' ./validate-opensearch.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

OS_URL="${OS_URL:-http://localhost:17200}"
OS_USER="${OS_USER:-admin}"
OS_PASS="${OS_PASS:-}"
AGENT_URL="${AGENT_URL:-http://localhost:17024}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
EMBED_MODEL="${EMBED_MODEL:-nomic-embed-text}"
KV_INDEX="${OPENSEARCH_INDEX:-agent-kv}"
SELFTEST="knn-selftest-$$"

AUTH=(-u "${OS_USER}:${OS_PASS}")
pass=0; fail=0
ok(){   echo "  [PASS] $1"; pass=$((pass+1)); }
no(){   echo "  [FAIL] $1"; fail=$((fail+1)); }
hdr(){  echo; echo "== $1 =="; }

# ── 1. Reachability ──────────────────────────────────────────────────────────
hdr "1. OpenSearch reachable ($OS_URL)"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "${AUTH[@]}" "$OS_URL")
if [ "$code" = "200" ]; then
  ver=$(curl -s "${AUTH[@]}" "$OS_URL" | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"]["number"])' 2>/dev/null)
  ok "reachable (HTTP 200, OpenSearch $ver)"
else
  no "not reachable (HTTP $code) — check service, port, creds"; echo; echo "SUMMARY: $pass pass / $((fail)) fail"; exit 1
fi

# ── 2. Cluster health ────────────────────────────────────────────────────────
hdr "2. Cluster health"
status=$(curl -s "${AUTH[@]}" "$OS_URL/_cluster/health" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])' 2>/dev/null)
case "$status" in
  green)  ok "status=green" ;;
  yellow) ok "status=yellow (fine for single node — replicas unassigned)" ;;
  *)      no "status=$status (red or unknown — investigate)" ;;
esac

# ── 3. k-NN plugin (required for tool-RAG) ───────────────────────────────────
hdr "3. k-NN plugin"
if curl -s "${AUTH[@]}" "$OS_URL/_cat/plugins?h=component" | grep -qi 'knn'; then
  ok "opensearch-knn plugin installed"
else
  no "k-NN plugin NOT found — tool-RAG kNN index will fail (store/agent-kv still works)"
fi

# ── 4. Agent store index ─────────────────────────────────────────────────────
hdr "4. Agent store index ($KV_INDEX)"
if curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$OS_URL/$KV_INDEX" | grep -q 200; then
  cnt=$(curl -s "${AUTH[@]}" "$OS_URL/$KV_INDEX/_count" | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])' 2>/dev/null)
  ok "$KV_INDEX exists (docs=$cnt) — sessions persist here"
else
  no "$KV_INDEX missing — created lazily on first chat; run a chat then re-check"
fi

# ── 5. Live k-NN round-trip (create → index → search → delete) ───────────────
hdr "5. k-NN round-trip self-test ($SELFTEST)"
curl -s -o /dev/null "${AUTH[@]}" -XPUT "$OS_URL/$SELFTEST" -H 'Content-Type: application/json' -d '{
  "settings":{"index":{"knn":true}},
  "mappings":{"properties":{"v":{"type":"knn_vector","dimension":3,
    "method":{"name":"hnsw","space_type":"cosinesimil","engine":"lucene"}}}}}'
curl -s -o /dev/null "${AUTH[@]}" -XPOST "$OS_URL/$SELFTEST/_doc/1?refresh=true" -H 'Content-Type: application/json' -d '{"v":[0.1,0.2,0.3]}'
curl -s -o /dev/null "${AUTH[@]}" -XPOST "$OS_URL/$SELFTEST/_doc/2?refresh=true" -H 'Content-Type: application/json' -d '{"v":[0.9,0.8,0.7]}'
hits=$(curl -s "${AUTH[@]}" -XPOST "$OS_URL/$SELFTEST/_search" -H 'Content-Type: application/json' \
  -d '{"size":1,"query":{"knn":{"v":{"vector":[0.1,0.2,0.3],"k":1}}}}' \
  | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["hits"]["hits"]))' 2>/dev/null)
curl -s -o /dev/null "${AUTH[@]}" -XDELETE "$OS_URL/$SELFTEST"
if [ "$hits" = "1" ]; then ok "kNN create/index/search works — tool-RAG index is supported"; else no "kNN search failed — tool-RAG will fall back to all-tools"; fi

# ── 6. Agent's own store health ──────────────────────────────────────────────
hdr "6. Agent store health ($AGENT_URL/actuator/health)"
backend=$(curl -s -m 5 "$AGENT_URL/actuator/health" | python3 -c 'import sys,json;print(json.load(sys.stdin)["components"]["store"]["details"].get("backend",""))' 2>/dev/null)
if [ "$backend" = "opensearch" ]; then ok "agent store backend = opensearch (not memory fallback)"
elif [ -n "$backend" ]; then no "agent store backend = '$backend' (expected opensearch — check OPENSEARCH_* env)"
else no "could not read agent health (agent down? wrong port?)"; fi

# ── 7. Embedding model for tool-RAG ──────────────────────────────────────────
hdr "7. Embedding model ($EMBED_MODEL @ $OLLAMA_URL)"
if curl -s -m 5 "$OLLAMA_URL/api/tags" | grep -q "$EMBED_MODEL"; then
  dim=$(curl -s -m 60 "$OLLAMA_URL/api/embeddings" -d "{\"model\":\"$EMBED_MODEL\",\"prompt\":\"health check\"}" \
        | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("embedding",[])))' 2>/dev/null)
  [ "${dim:-0}" -gt 0 ] && ok "$EMBED_MODEL present, embed dim=$dim" || no "$EMBED_MODEL present but embed call failed"
else
  no "$EMBED_MODEL not pulled — 'ollama pull $EMBED_MODEL' (only needed if TOOL_RAG_ENABLED)"
fi

echo; echo "════════════════════════════════════"
echo "SUMMARY: $pass passed, $fail failed"
[ "$fail" -eq 0 ] && echo "OpenSearch is fully operational for the agent + tool-RAG." || echo "See [FAIL] lines above."
exit "$fail"
