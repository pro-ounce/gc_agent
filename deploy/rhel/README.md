# Deploying `ai-agent-service` on RHEL with Supervisor

The GC Agent is a uvicorn/FastAPI app launched via the shared deploy runner
`deploy/run_uvicorn_uds.py` (factory entry `app.main:create_app`, same convention as the
delivery/rag apps). It serves **TCP `:17024`** by default (what the Spring gateway proxies
to); set `AGENT_UDS_PATH` instead to serve over a unix socket behind Apache. Runs as a
non-root service managed by **supervisor** (not systemd directly, not Docker).

**Pre-deploy backup (wire as the FIRST deploy step):** before rsync + restart, take an
OpenSearch snapshot so a bad push is recoverable — `make snapshot LABEL=pre-<tag>` (or
`deploy/os-snapshot.sh <label>`). It blocks and returns non-zero on failure, so the deploy
aborts if the backup fails. Requires the snapshot repo to be registered (see the Backups
section of the /admin console).

| Item | Value |
|---|---|
| App root + venv | `/apps/agent` (venv built in-place: `bin/`, `lib/` alongside `app/`) |
| Python | **3.12.8** via pyenv (`pyenv local 3.12.8`) — matches the repo's `.python-version` |
| Service user | `gcusr` — owns `/apps/agent` |
| Venv python | `/apps/agent/bin/python` |
| Port | `8080` (loopback-only unless the gateway is on another host) |
| Config | `/apps/agent/.env.local` (mode `0600`) |
| Logs | `/var/log/gc/ai-agent-service/{stdout,stderr}.log` |
| Supervisor conf | `/etc/supervisord.d/ai-agent-service.ini` |

## 1. Build the wheelhouse, copy source, build the venv (offline)

Prod is air-gapped — **no internet, no `pip.conf` mirror** (that's dev-only). Deps are
shipped as a pre-downloaded **wheelhouse** and installed with `--no-index`.

**1a. On a CONNECTED host that matches prod** (Linux **x86_64** + Python **3.12** —
compiled wheels like pydantic-core/uvloop/httptools/watchfiles are platform+abi specific):

```bash
cd agent/deploy/rhel
./build-wheelhouse.sh          # → wheelhouse/ + wheelhouse.tar.gz (app deps + supervisor)
```

> Only have a Mac? Cross-download linux wheels instead:
> ```bash
> pip download -r requirements.txt -d wheelhouse \
>   --platform manylinux2014_x86_64 --python-version 3.12 \
>   --implementation cp --abi cp312 --only-binary=:all:
> pip download supervisor -d wheelhouse      # universal wheel
> ```

**1b. Ship source + wheelhouse to `/apps/agent`:**

```bash
rsync -av --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  ~/Git/gc/agent/  DEPLOY_USER@RHEL_HOST:/apps/agent/
scp agent/deploy/rhel/wheelhouse.tar.gz DEPLOY_USER@RHEL_HOST:/tmp/
ssh DEPLOY_USER@RHEL_HOST 'tar xzf /tmp/wheelhouse.tar.gz -C /apps/agent/'   # → /apps/agent/wheelhouse
```

**1c. On the host, build the venv offline (pyenv 3.12.8, no index):**

```bash
pyenv install 3.12.8           # once, if not already installed
cd /apps/agent
pyenv local 3.12.8
python3 -m venv /apps/agent --prompt="agent"
source /apps/agent/bin/activate
pip install --no-index --find-links /apps/agent/wheelhouse --upgrade pip
pip install --no-index --find-links /apps/agent/wheelhouse -r requirements.txt
deactivate
```

> 3.12.8 matches the repo's committed `.python-version`. No `PIP_CONFIG_FILE`/`pip.conf`
> and no network are used — everything resolves from the local wheelhouse.

## 2. Wire supervisor (no EPEL — installed via pip)

This host isn't entitlement-registered, so `epel-release`/`dnf install supervisor`
won't work. Instead supervisor is installed **via pip from the offline wheelhouse**
into its own venv at `/apps/supervisor`, and **systemd runs supervisord**.

```bash
cd /apps/agent/deploy/rhel
sudo APP_ROOT=/apps/agent SVC_USER=gcusr ./install.sh
```

The installer (assumes the app venv + wheelhouse from §1 exist):
1. `python3 -m venv /apps/supervisor` + `pip install --no-index --find-links <wheelhouse> supervisor`
   — supervisor runs on the **system `python3`** (which still has `asyncore`/`asynchat`,
   removed in 3.12); the supervisor wheel is universal so it installs regardless of version.
2. Installs `/etc/supervisord.conf` (includes `/etc/supervisord.d/*.ini`) and the
   `supervisord.service` systemd unit → `systemctl enable --now supervisord`.
3. Ensures `gcusr`, seeds `.env.local` (0600), sets ownership + log dirs, drops the
   `ai-agent-service.ini` program config, and starts the service.

> If no wheelhouse is present, the installer falls back to networked pip (no custom
> `pip.conf`) — only works if the host can reach an index.

`supervisorctl` lives at `/apps/supervisor/bin/supervisorctl` — it reads
`/etc/supervisord.conf` automatically, or pass `-c /etc/supervisord.conf` explicitly.

## 3. Edit `.env.local` before it's useful

Minimum for the integrated (gateway-vouched) mode:

```ini
PLATFORM_AUTH_MODE=gateway
GC_JWT_SECRET=<sealed copy of the DB HMAC signing key (HS512)>
JAVA_MCP_BASE_URL=http://MCP_HOST:19170/mcp-service/mcp
OLLAMA_BASE_URL=http://OLLAMA_HOST:11434
OPENSEARCH_URL=https://OPENSEARCH_HOST:9200
OPENSEARCH_USERNAME=...
OPENSEARCH_PASSWORD=...
# ROOT_PATH=/ai-agent-service   # ONLY if you add the dedicated /api/agent gateway route.
#                                Leave empty for the administration-BFF wiring (chat-agent/reply).
```

After editing: `sudo supervisorctl restart ai-agent-service`

## 4. SELinux / firewall (RHEL gotchas)

- **SELinux**: supervisord (run by systemd) spawning a venv python in `/apps` is usually
  fine. If AVC denials appear (`ausearch -m avc -ts recent`), the pragmatic fix is a
  targeted policy, not `setenforce 0`.
- **Firewall**: keep 8080 **loopback-only** if the gateway runs on the same host. If the
  gateway is remote: `firewall-cmd --add-port=8080/tcp --permanent && firewall-cmd --reload`
  (and prefer mTLS or a source-restricted rule — the agent trusts the gateway's `X-INT-TKN`).

## 5. Operate

```bash
# supervisorctl = /apps/supervisor/bin/supervisorctl (add to PATH or alias it)
supervisorctl status ai-agent-service
supervisorctl restart ai-agent-service
supervisorctl tail -f ai-agent-service stderr
systemctl status supervisord                           # supervisord itself (systemd)
curl -s http://localhost:8080/actuator/health          # {status: UP|DEGRADED|DOWN, components:{...}}
curl -s http://localhost:8080/actuator/prometheus | head
```

`autorestart=true` brings the app back on crash; `supervisord` is `systemctl enable`d so
it survives reboot. To pick up new code: rsync the source, `/apps/agent/bin/pip install -r
requirements.txt` if deps changed, then `supervisorctl restart ai-agent-service`.

## Notes

- `--workers 2` is safe because the default store is **OpenSearch** (shared across workers).
  If you switch `STORE_BACKEND=memory`, drop to `--workers 1` — the in-memory store is
  per-process and sessions won't be shared.
- The service does **not** need to be internet-facing. Only the gateway talks to it.
