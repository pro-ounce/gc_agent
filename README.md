# MCP Agent

Enterprise-grade FastAPI agent & chatbot that bridges Claude (Anthropic) or OLLAMA to a Spring Boot MCP server.

## Architecture

```
app/
├── main.py               # create_app() factory
├── connections.py        # Redis + httpx MCP client
├── commons/              # config, flags, logger, middleware
├── rbac/                 # JWT + API-key auth, permissions, decorators
├── models/               # Pydantic: chat, session, mcp tool/prompt
├── mcp/                  # Spring Boot MCP client, tool & prompt registries
├── services/             # chat_service (agentic loop), llm_service, session_service
└── routers/              # /api/chat, /tools, /prompts, /sessions, /auth, /health
```

## Quick start

```bash
# 1. Clone / open this directory in Claude Code (standalone — not inside RAG)
cd /Users/six7/Git/gc/agent

# 2. Create venv + install deps
make install

# 3. Copy env template and fill in secrets
make env          # creates .env.local
# edit .env.local — set ANTHROPIC_API_KEY and MCP_BASE_URL

# 4. Start dev server (hot-reload)
make dev          # http://localhost:8080  •  docs at /docs
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MCP_BASE_URL` | `http://localhost:8090` | Spring Boot MCP server |
| `MCP_API_KEY` | — | Optional auth header for MCP server |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `ollama` |
| `LLM_MODEL` | `claude-3-5-sonnet-20241022` | Model name |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=anthropic` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Required when `LLM_PROVIDER=ollama` |
| `REDIS_URL` | `redis://localhost:6379/0` | Session storage (in-memory fallback if unavailable) |
| `AUTH_ENABLED` | `true` | Set `false` to bypass auth in dev |
| `RBAC_ENABLED` | `true` | Role-based permission checks |
| `JWT_SECRET` | change-me | **Change in production** |
| `TOOL_RISK_CONFIRMATION` | `true` | Require confirm for MEDIUM/HIGH risk tools |
| `STREAMING_ENABLED` | `true` | SSE streaming responses |

See `.env.example` for the full list.

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health probe (includes MCP backend status) |
| `POST` | `/api/chat` | Send message, get complete response |
| `POST` | `/api/chat/stream` | SSE streaming chat |
| `POST` | `/api/chat/confirm` | Confirm / reject a pending tool action |
| `POST` | `/api/chat/prompt` | Render a server-side MCP prompt and chat |
| `GET` | `/api/tools` | List all MCP tools |
| `GET` | `/api/tools/{name}` | Tool details |
| `GET` | `/api/prompts` | List all MCP prompts |
| `POST` | `/api/prompts/{name}/execute` | Render a prompt server-side |
| `GET` | `/api/sessions/{id}` | Session history |
| `DELETE` | `/api/sessions/{id}` | Delete session |
| `POST` | `/api/auth/login` | Get JWT tokens |
| `GET` | `/api/auth/me` | Current user |
| `POST` | `/api/auth/keys` | Create API key |

## Expected Spring Boot MCP API

```
GET  /mcp/tools                      → [{name, description, inputSchema, ...}]
POST /mcp/tools/{name}/execute       → {result: ...}
GET  /mcp/prompts                    → [{name, description, arguments}]
POST /mcp/prompts/{name}/execute     → {text: "..."}
GET  /mcp/health                     → {status: "UP"}
```

## Smoke tests

```bash
# Health
curl http://localhost:8080/health

# Chat (auth disabled)
curl -X POST http://localhost:8080/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","message":"List available tools"}'

# Chat with JWT
TOKEN=$(curl -s -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"secret"}' | jq -r .access_token)

curl -X POST http://localhost:8080/api/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","message":"Hello"}'

# Confirm a pending action
curl -X POST http://localhost:8080/api/chat/confirm \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","action_id":"<id>","confirmed":true}'
```

## Development

```bash
make test       # Run test suite (17 tests)
make test-cov   # Tests + HTML coverage report
make lint       # ruff
make fmt        # black
make clean      # Remove venv, caches
```

## Docker

```bash
make docker-up    # Builds image, starts agent + redis
make docker-down
```
