# Quick Start — DT Fleet Toolkit

Deploy the Dynatrace OneAgent to Azure Linux App Services, at scale. Full detail in
[README.md](README.md); this is the 2-minute version.

## 0. Prereqs (once)

```bash
az login                                  # your own user — no Service Principal needed
cd /root/azure-linux-app-services
# creds.json already holds: credentials.dtApiEnvUrl + credentials.dtPaasToken (InstallerDownload scope)
```
Needs: `python3`, `az`, `curl`, `jq`. Add `--rest` to any command to bypass the slow `az` CLI.

## Unified tool (handles code *and* container apps)

`dt_appservice.py` auto-routes each app — **code → code path, container → sidecar path** —
so one command covers a mixed fleet:

```bash
python3 dt_appservice.py assess --all-subscriptions --rest                       # dry run, see routing
python3 dt_appservice.py deploy --subscription prod --rest --yes --verify --rollback-on-fail
# code apps, keep the startup command untouched (inject via LD_PRELOAD app setting):
python3 dt_appservice.py deploy --subscription prod --rest --yes --leave-startup-command --verify --rollback-on-fail
```
The sections below use the per-path tools directly; the unified tool takes the same flags
and routes them. (A sidecar only attaches to Publish: Container apps, so code apps always
get the startup-command / LD_PRELOAD route — never a sidecar.)

## 1. Assess first (dry run — never changes anything)

```bash
# whole tenant, fast, to console
python3 dt_appservice_fleet.py assess --all-subscriptions --rest

# scope + per-app startup/DT state + write a report (format from file extension)
python3 dt_appservice_fleet.py assess \
    --subscription prod --app-service-name "web-*" \
    --detailed --rest --output-file reports/prod.md
```

## 2. Preview the deploy

```bash
python3 dt_appservice_fleet.py deploy \
    --subscription prod --app-service-name "web-*" --rest --what-if
```

## 3. Deploy (resumable)

```bash
python3 dt_appservice_fleet.py deploy \
    --subscription prod --app-service-name "web-*" \
    --rest --resilient --state-file rollout.jsonl --yes -v
```
Interrupted? Re-run the **exact same command** — it skips the apps already done
(`--state-file`) and continues.

## 3b. Verify it actually instrumented

`deploy` success means *config applied + restarts triggered* — not that the agent
loaded (with `--resilient` fail-open, a failed download still "succeeds"). `verify`
checks **both sides**: the Azure container log (agent loaded + connected) *and*
Dynatrace (the process group actually reporting):

```bash
python3 dt_appservice_fleet.py verify \
    --subscription prod --app-service-name "web-*" --rest -v

# or confirm inline, right after deploying:
python3 dt_appservice_fleet.py deploy ... --verify
```
Per-app verdict: **CONFIRMED (Azure + Dynatrace)** ✅ · **INSTRUMENTED (connected)**
(Azure-side only; DT check skipped/absent) · **FAIL-OPEN — NOT instrumented** (agent
download failed — check the PaaS token/endpoint for that tenant) · **injected but
INACTIVATED** (tenant disabled the process group) · **no agent markers** (idle/cold —
hit the app URL, then re-verify).

The **Dynatrace-side** check needs a read credential: add `dtApiToken` (classic token
with `entities.read`) to `creds.json`, **or** have `dtctl` configured for the tenant
(the tool auto-detects the context). Skip it with `--no-dt-check`.

**It actively drives the test.** When a Dynatrace read credential is present, `verify`
(and `deploy --verify`) **generates traffic to the app and polls Dynatrace** until the
process group materializes — up to `--dt-timeout` seconds (default 180; raise it for
cold/idle apps). `--no-traffic` disables the traffic generation. This is the path to
use when SCM/Kudu is IP-restricted (the container-log check is blocked but the
Dynatrace confirmation still works):
```bash
python3 dt_appservice_fleet.py verify --auth az --subscription <sub> \
    --app-service-name "<app>" --rest --dt-timeout 300
```

## 3c. Apps with no Startup Command (Oryx implicit default)

Many Linux apps leave the **Startup Command blank** and let the platform's Oryx
build system auto-detect the launch command at boot. The wrapper has to `exec` a
real command, so you have two options — pick by what access you have:

| Situation | Use |
|---|---|
| You know the command | `--start-app-cmd "gunicorn … app:app"` (one value applies to the whole run) |
| SCM/Kudu reachable | `--discover-start-cmd` — reads the app's files and replicates Oryx |

**SCM locked down and you don't know the command?** Either copy the real launch from the
**Azure Portal → Log stream** at boot (`Generating 'gunicorn' command for '<module>:app>'`
for Python, the `pm2`/`node` line for Node) into `--start-app-cmd` — **or** use
`--preserve-oryx`, which needs no command at all:

```bash
# Node/Python, blank startup command, SCM IP-restricted — hands-off + safe:
python3 dt_appservice_fleet.py deploy --subscription prod --app-service-name "web-*" \
    --rest --preserve-oryx --rollback-on-fail --yes --verify
```
`--preserve-oryx` installs the agent to `/home`, then lets the platform's **own Oryx**
auto-detect and launch the app at boot with `LD_PRELOAD` injected — no command, no SCM
access. It handles compressed Oryx builds (`output.tar.zst`) by pointing Oryx at the
already-extracted app dir. **Always add `--rollback-on-fail`** (with `--verify`): after
deploy it probes each app's HTTP health and, if an app isn't serving, automatically
uninstalls to restore it — so the rollout can never leave an app down. Validated on
Node + Python. If an app shows `No framework detected; using default app`, it has **no
code deployed** — instrument it once your application is actually there.

## 4. Uninstall (revert)

```bash
python3 dt_appservice_fleet.py uninstall \
    --subscription prod --app-service-name "web-*" --rest --yes
```

---

## The flags you'll actually use

| Flag | What it does |
|---|---|
| `--subscription GUID\|NAME` / `--all-subscriptions` | scope by subscription — a GUID, or a name (substring or glob with `* ? [`). Repeatable. |
| `--app-service-name` / `--resource-group` | narrow to specific apps (name: substring or glob) |
| `--what-if` | show what *would* happen, change nothing |
| `--rest` | use ARM REST instead of `az` — **~30× faster when `az` is slow** |
| `--auth az\|managed-identity\|service-principal` | force the token source (default auto). MI/SP reach only their own subscriptions; `az` resolves cross-tenant. |
| `--resilient` | persist agent to `/home` (no re-download on restart) + fail-open |
| `--state-file FILE` | resumable rollout — completed apps skipped on re-run |
| `--parallel N` | deploy/uninstall N apps at once (~sequential-time/N; overlaps restart-wait). Keep low on `az`; `--rest` handles more. |
| `--force` | re-apply to already-instrumented apps (default skips them) |
| `-v` / `--verbose` | echo every call + timing (tokens redacted) — **use if it looks stuck** |
| `--start-app-cmd "<cmd>"` | launch command for apps with **no** startup command (Oryx implicit default) |
| `--discover-start-cmd` | auto-detect that launch command from the app's files (package.json / Python entrypoint) — review with `--what-if` |
| `--preserve-oryx` | **Node/Python, no startup command, SCM locked down:** inject the agent, then let the platform's own Oryx auto-detect run the app at boot — **no command or SCM access needed.** Handles compressed Oryx builds. Pair with `--rollback-on-fail`. |
| `--leave-startup-command` | **Instrument without owning the Startup Command:** install the agent to `/home`, then restore the app's original command and inject via an `LD_PRELOAD` app setting. Steady state = your exact command, untouched. |
| `--rollback-on-fail` | with `--verify`: if an app isn't serving HTTP after deploy (our change broke it), **auto-uninstall to restore it** — a deploy can never leave an app down. |
| `--set-host-tag K=V` | extra Dynatrace `[Environment]K:V` tag (repeatable) |
| `--detailed` | assess: fetch per-app startup cmd + DT state |

## Container apps? Use the sidecar path

The commands above are for **code / built-in-image** apps. **Containerized (BYOC)** Linux
apps are handled by the independent fork `dt_appservice_sidecar.py`, which attaches the
Dynatrace bootstrapper as a **sidecar container** (tenant token + `LD_PRELOAD` app
setting; no image change). Same creds.json.

```bash
# assess container readiness (never mutates)
python3 dt_appservice_sidecar.py assess --all-subscriptions --rest

# deploy to sitecontainers-mode apps, confirm + auto-rollback if an app won't serve
python3 dt_appservice_sidecar.py deploy --subscription prod --rest --yes --verify --rollback-on-fail

# convert a legacy DOCKER| app first, drop App Insights, enable storage, pick the runtime
python3 dt_appservice_sidecar.py deploy --subscription prod --rest --yes \
    --allow-convert --enable-storage --disable-app-insights --technology java --verify --rollback-on-fail

# bump the agent / remove it
python3 dt_appservice_sidecar.py update    --subscription prod --rest --yes
python3 dt_appservice_sidecar.py uninstall --subscription prod --rest --yes
```
**Gotcha #1: tenant token ≠ PaaS token** (the tool derives it for you). Python containers
are reported `unsupported-runtime` and skipped. Full detail + the two-paths comparison:
[README → Container apps (sidecar path)](README.md#container-apps-sidecar-path).

## Naming / tagging in Dynatrace

Every deploy auto-stamps `azure.app_service_name=<site>` → shows as
`[Environment]azure.app_service_name:<site>` on the process group. Add more with
`--set-host-tag team=retail --set-host-tag owner=platform`.

## Least-privilege auth (no SP, no admin account)

Run as **your own `az login`**. Grant only the custom roles in [`roles/`](roles/):

```bash
az role definition create --role-definition roles/dt-fleet-deployer.json   # edit AssignableScopes first
az role assignment create --assignee you@tenant.com --role "DT Fleet Deployer" \
    --scope /subscriptions/<sub>/resourceGroups/<rg>
```
`dt-fleet-assessor.json` = read-only (dry run); `dt-fleet-deployer.json` adds only
`config/write` + `restart/action`. For **zero `az`** (token included), run on a
**managed identity** — the token then comes from IMDS.

## Good to know

- **Always `assess` before `deploy`.** The `dt_appservice_fleet.py` deploy below touches
  **Linux code** apps (containers/Function Apps reported and skipped). For containers use
  `dt_appservice_sidecar.py`, or let `dt_appservice.py` route both automatically.
- **Idempotent + resumable.** Re-running `deploy` skips already-instrumented apps;
  with `--state-file` it also skips apps completed in a prior run.
- **"Seems stuck"?** Add `-v`. On a starved host `az` takes 100–200s/call — use
  `--rest`. On a healthy host a warm `--rest` run is a few seconds.

## Demo apps (optional)

```bash
RG=dt-oneagent-fleet-demo ./provision_demo_apps.sh                 # 4 apps: Express/Fastify/Flask/FastAPI
python3 generate_load.py --resource-group dt-oneagent-fleet-demo --duration 600
```
