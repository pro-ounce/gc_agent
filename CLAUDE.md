# GC / COMPASS — build & deploy reference

Sandbox box (sparkbee): `ssh -i ~/certs/keys/sthota_instances.pem gcusr@192.168.0.180` (PEM with `-i`;
the `sparkbee` host alias + agent key fails). `sudo` is passwordless. Prefer `ServerAliveInterval=15`
and run long jobs detached (`nohup … &`) — the VPN drops periodically.

## FE (micro-frontends) — Jenkins build + deploy
**The one command** — triggers the `pl_ui_build_deploy_modules` Jenkins job with the correct params
(mirrors build #6283), waits for it, then runs the local `deploy-fe.sh`:
```bash
ssh … 'nohup /apps/fe-build-deploy.sh sandbox > /tmp/fe-build.log 2>&1 &'   # [sandbox|qa] [--no-local-deploy]
```
It builds from the **`sandbox`** branch, discovers Jenkins creds from
`/apps/auto_jenkins/configs/supervisor/*.conf` (`JENKINS_[CLI_]USER/TOKEN`, Jenkins at
`https://apps.proounce.com/jenkins`), triggers `buildWithParameters` with a crumb, polls the queue →
build number, waits, then deploys. Build takes ~10–17 min (sometimes longer). Watch: the console URL it prints, or
`tail -f /tmp/fe-build.log`. ⚠️ The script's poll can print `result: TIMEOUT … skipping deploy` while the
Jenkins build is *still running* (its wait is shorter than a slow build). If so, check the real status —
`curl -u <jenkins creds> $JURL/job/pl_ui_build_deploy_modules/<N>/api/json?tree=building,result` — and once
it's `SUCCESS`, run `sudo /apps/deploy-fe.sh` yourself to pull the Nexus artifact to sparkbee. Jenkins builds to `vHostIP=141.31.1.8` (AWS), so the script's **local
`deploy-fe.sh` step is what serves sparkbee** (`/apps/deploy-fe.sh`; pulls the Nexus `build_<epoch>.tar`,
importmap at `/apps/build`, gcusr-owned). It replaces the whole `/apps/build` and keeps one rollback at
`/apps/build.prev`.

**Status checks + manual deploy gotchas** (learned the hard way):
- **Working Jenkins creds** are in `gunicorn_deployer.conf` (`JENKINS_USER`=`ec4aaaf6-…`/"Proounce AI").
  The `cli-ounce` token in `gunicorn_chatbot.conf` is **dead** (returns anonymous 500 + login page). Query:
  `curl -sk -u "$U:$T" "$JURL/job/pl_ui_build_deploy_modules/lastSuccessfulBuild/api/json?tree=number,result"`.
- Builds are branch-tagged in `displayName` (`<N>-PRJ241001-<sandbox|qa>-minimal-aws`) and the artifact
  epoch is in the build `description` (`Generating the build <epoch>`). `deploy-fe.sh` resolves the latest
  SUCCESS matching `PRJ241001-<branch>`; **pin a specific artifact** with `sudo /apps/deploy-fe.sh <epoch>`
  to dodge a concurrent other-branch build.
- `deploy-fe.sh` gets the Nexus password from **Vault** (`security.proounce.ai/api/vault`, secret
  `nexus/deployment`). That broker can return a persistent **500** (outage) → `ERROR: no Nexus credentials
  from Vault`; the build/artifact are fine — just retry the deploy once Vault is back.

**Surgical hot-swap (no Jenkins, for a quick single-module test)** — build locally, drop the bundle in a
fresh epoch dir, repoint the importmap (old dir + `importmap.json.bak.*` kept for rollback):
```bash
cd prj241001_fe_<module>-app && npm run build:webpack
EPOCH=$(python3 -c 'import time;print(int(time.time()*1000))')
scp -i ~/certs/keys/sthota_instances.pem dist/gc360-*-app.js dist/*.svg gcusr@192.168.0.180:/tmp/
ssh … "NEW=/apps/build/${EPOCH}_prj241001_fe_<module>-app; mkdir -p \$NEW; cp /tmp/gc360-*-app.js /tmp/*.svg \$NEW/; \
  cp /apps/build/importmap.json /apps/build/importmap.json.bak.\$(date +%s); \
  sed -i 's#\"@gc360/<module>-app\": *\"[^\"]*\"#\"@gc360/<module>-app\": \"/${EPOCH}_prj241001_fe_<module>-app/gc360-<module>-app.js\"#' /apps/build/importmap.json"
```
⚠️ A hot-swap is **not** in Nexus — run the Jenkins build afterwards or the next official deploy rolls it back.
Widget source lives in `prj241001_fe_widget-app` on the **`sandbox`** branch.

## MW (Java middleware) — one Tomcat (`gc-mw.service`), WARs from Nexus
```bash
sudo /apps/deploy-mw.sh <module> [<git-sha>]      # e.g. administration 49fc982…; 'all' for every module
sudo /apps/deploy-mw.sh --no-restart <m> <sha>    # swap only; batch several, then one restart
sudo /apps/deploy-mw.sh --check <m> <sha>         # dry run; --rollback <m> to restore previous WAR
```
Modules: gateway, authentication, administration, support, smarthub, reporting, formulation, mcp.
A restart re-deploys ALL contexts (~5 min) — and **reporting-service fails on `ORA-01017` (invalid DB
creds)** until that credential is fixed; unrelated to the deploy.

## Agent (gc_agent, FastAPI) — `ai-agent-service` :17024
```bash
ssh … 'bash /apps/gc_agent/deploy/pull-deploy.sh main'   # git reset --hard origin/main + restart (+pre-snapshot)
```
Eval gate before shipping routing/model changes: `APP_SKIP_INIT=1 PYTHONPATH=/apps/gc_agent ./bin/python eval/<f>.py`
(router_eval, classifier_eval, tool_calling_models, history_archive, …). Admin console:
`https://sparkbee.proounce.ai/gc-agent/admin`.

## TLS certs — Vault (PEM) + Nexus (JKS) → /etc/certs/<domain>
```bash
sudo /apps/deploy-certs.sh <domain>               # fetch + install + reload apache2 + restart gc-mw
sudo /apps/deploy-certs.sh <domain> --check|--no-apply|--apache-only|--rollback
```

## Snapshot recovery — recover agent-kv (history/config) from a daily OpenSearch snapshot
```bash
sudo /apps/gc_agent/bin/python /apps/gc_agent/deploy/recover-from-snapshot.py list
sudo … recover --snapshot <name> --key history:chatbot:1 [--app <code>] [--write]   # dry-run without --write
```
Snapshots: SM policy `agent-daily`, repo `agent-fs`, 14-day retention, OpenSearch `localhost:17200`.

## Conventions
- Commits under **tech-ounce**; pull `--rebase` before push. Never insert/update/delete live DB data without approval.
- Chat session id = `chatbot:<userId>:<appCode>` (per-app). FE served from `/apps/build` by apache2.
