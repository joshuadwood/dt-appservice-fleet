# Dynatrace OneAgent — Azure Linux App Service Fleet Toolkit

Automated, at-scale deployment of the Dynatrace OneAgent to Azure App Service
for **Linux (built-in image / code deploy)**, plus the assessment tooling to
decide *where* it can go.

Target Dynatrace environment: `qof78400` (`https://qof78400.live.dynatrace.com`).
Credentials (`dtApiEnvUrl`, `dtPaasToken`) are read from `creds.json`.

**→ In a hurry? See [QUICKSTART.md](QUICKSTART.md).**

---

## Contents

| File | Purpose |
|---|---|
| `dt_appservice_fleet.py` | **Main tool.** `assess` (dry run) / `deploy` / `verify` / `uninstall` across subscriptions. |
| `provision_demo_apps.sh` | Stand up 4 demo App Services (2 Python, 2 Node — distinct frameworks). |
| `generate_load.py` | Drive CRUD traffic against the apps so OneAgent has requests to trace. |
| `apps/` | Source for the 4 demo apps (Express, Fastify, Flask, FastAPI). |
| `reports/` | Generated assessment reports (`.md` / `.json` / `.csv`). |

---

## 1. Assess (dry run)

Non-mutating. Enumerate App Services, classify them, list startup commands, and
flag anything already instrumented. **Start here before deploying anything.**

```bash
# whole tenant, console table
python3 dt_appservice_fleet.py assess --all-subscriptions

# filter subscriptions by name substring, write a flat file (format by extension)
python3 dt_appservice_fleet.py assess \
    --subscription Liftr --output-file reports/liftr.md

# one subscription, include per-app startup command + DT state (slower)
python3 dt_appservice_fleet.py assess \
    --subscription "Sales-Engineering/NORAM" --detailed \
    --app-service-name dtfleet --output-file reports/noram.json
```

Scope flags: `--subscription GUID|NAME` (a subscription GUID, or a name;
repeatable), `--all-subscriptions`, `--resource-group`, `--app-service-name`
(repeatable). Output: `--output-file` (console if omitted),
`--format txt|md|csv|json`, `--detailed`.

**Wildcards:** both `--app-service-name` and `--subscription` (name form) treat a
value containing `*`, `?`, or `[` as a case-insensitive glob (anchored to the whole
name); a plain value stays a substring match. E.g. `--app-service-name
"dtfleet-node-*"`, `--subscription "*-prod"`. A `--subscription` value that is a
GUID matches by id (and is trusted even if cross-tenant). `--subscription-filter`
and `--name-filter` remain as aliases.

**Speed (`--rest`):** add `--rest` to run inventory + per-app enrich through the
**ARM REST API** instead of the `az` CLI. See [Performance](#performance-the-az-cli-tax)
— on hosts where `az` is slow this is ~30× faster.

The report identifies every app by **name**, **OS** (`linux`/`windows`), **runtime**
(`NODE|20-lts`, `PYTHON|3.12`, `JAVA|17`, …), **deployment model**, **registry**
(for containers), and an explicit **`deployable`** flag. The summary partitions the
fleet cleanly (Linux code / container / function / Windows):

- `code/core` — bring-your-own-code on the platform built-in image → **deployable**
  (if Linux **and** a OneAgent-supported runtime).
- `container:<ACR|JFrog|MCR|GHCR|DockerHub|Other>` — standalone container →
  needs the custom-image/sidecar path, **skipped** by deploy.
- `function` — Function App → the Web-App injection path is inert, **skipped**.
- **Windows** — **skipped**; this tool is **Linux-only** (deploy never touches a
  non-Linux app). Pass `--deployable-only` to limit the inventory to just the
  relevant Linux code/core apps.

`deployable = Linux AND code/core AND OneAgent-supported runtime` — so e.g. a Linux
**WordPress** app shows as `code` but `deployable=False` (unsupported runtime).

The assessment also prints an **estimated deploy time** for the deployable apps
(sequential), calibrated to the call latency observed during the run — e.g.
`via --rest: ~27 min (~48s/app = 45s restart-wait + ~5 ARM calls @ 0.5s)`. The
`--restart-wait` sleep dominates the `--rest` path, so lowering it (e.g.
`--restart-wait 20`) is the main lever. The JSON report includes
`estimated_deploy_seconds` for scripting.

## 2. Deploy OneAgent

Mutating. Instruments **Linux code** Web Apps only. Deploy honors every gotcha in
`dynatrace-appservice-linux-gotchas.md` (see "How instrumentation works" below).

```bash
# instrument the 4 demo apps
python3 dt_appservice_fleet.py deploy \
    --resource-group dt-oneagent-fleet-demo --yes \
    --ntfy-topic the-score-of-the-hms-pinafore-1

# preview without applying
python3 dt_appservice_fleet.py deploy --app-service-name dtfleet --what-if
```

Useful knobs: `--dt-include` (override the per-runtime tech), `--dt-flavor`
(`default` glibc / `musl` Alpine), `--start-app-cmd` (required for apps that had
*no* existing Startup Command — see Node gotcha), `--env-tag`, `--restart-wait`,
`-v/--verbose` (echo every `az` call with timing + applied values, tokens redacted),
`--rest` (ARM REST fast path for reads).

### Naming & tagging in Dynatrace

Every deploy stamps a reliable identity **tag** via `DT_TAGS`, which surfaces in
Dynatrace as `[Environment]<key>:<value>` on the process group (the App Service's
identity on the PaaS path):

| Flag / default | Env var set | Result in Dynatrace |
|---|---|---|
| *(always on)* | `DT_TAGS: azure.app_service_name=<site>` | `[Environment]azure.app_service_name:<site>` ✅ verified |
| `--set-host-tag KEY=VALUE` (repeatable) | appends to `DT_TAGS` | `[Environment]KEY:VALUE` |
| `--env-tag <v>` | `DT_TAGS: env=<v>` | `[Environment]env:<v>` |
| `--service-name <tmpl>` | `OTEL_SERVICE_NAME` | service-entity name *(OTel-aware; best-effort, not guaranteed)* |
| `--otel-resource-attributes <kv>` | `OTEL_RESOURCE_ATTRIBUTES` | e.g. `service.namespace`, `deployment.environment` |
| `--host-name <tmpl>` | `DT_LOCALTOVIRTUALHOSTNAME` | names the host entity |

`--set-host-tag`, `--service-name`, and `--host-name` support `{name}`/`{rg}`/`{sub}`
placeholders. Prefer **`DT_TAGS`** (deterministic, env-sourced) over
`OTEL_SERVICE_NAME` for identity — the latter is read by the OTel-aware OneAgent but
does not reliably rename the classic service entity.

### Idempotency & re-runs

Deploy is **idempotent and interruption-safe**. All recovery state lives on the
App Service itself (app settings), so there is no local checkpoint — re-running the
same command is always safe:

- **Already-instrumented apps are skipped by default.** The dry-run/assess
  `dt_instrumented` flag drives this. Pass `--force` to re-apply.
- **Re-applying never corrupts the saved original.** `DT_FLEET_ORIG_STARTUP` is
  written once and preserved on every subsequent run (it is never overwritten with
  the wrapper), so `uninstall` always restores the true original command.
- **The real launch command is recovered**, not re-derived: on a re-run the tool
  reads `START_APP_CMD` from the prior deploy rather than treating the wrapper as
  the command.
- **Foreign instrumentation is protected.** If an app already has a *different*
  installer wrapper as its startup command (e.g. a hand-rolled paas-sh snippet) and
  no captured `START_APP_CMD`, deploy returns `needs-input` instead of nesting a
  wrapper — pass `--start-app-cmd "<real launch command>"` to adopt it safely.

Interruption: apps are processed sequentially; writes are ordered settings-first
then startup-swap then restart, so a kill mid-run leaves each app either untouched
or fully done (never half-broken), and a re-run completes the rest.

### Resumable rollouts (`--state-file`)

For large campaigns, pass `--state-file rollout.jsonl`. Each app's outcome is
appended (and flushed) the instant it completes, so an interruption loses at most
one app. Re-running the **same command** with the **same state file** skips every
app already recorded `deployed`/`uninstalled` — **before** the enrich step, so a
resume is cheap even across hundreds of apps. Errors / `needs-input` / `unknown`
are *retried* on the next run.

```bash
# start a rollout
python3 dt_appservice_fleet.py deploy --subscription prod --rest \
    --state-file rollout.jsonl --yes
# ...interrupted (Ctrl-C, token expiry, network)... just run it again:
python3 dt_appservice_fleet.py deploy --subscription prod --rest \
    --state-file rollout.jsonl --yes      # resumes where it left off
```

The file doubles as an audit log (`ts`, `id`, `name`, `status`, `detail` per line).

## 3. Uninstall

```bash
python3 dt_appservice_fleet.py uninstall --resource-group dt-oneagent-fleet-demo --yes
```

Restores the original Startup Command (stashed in `DT_FLEET_ORIG_STARTUP`) and
removes the `DT_*` / `START_APP_CMD` settings.

---

## Least privilege & auth (no Service Principal, no admin account)

The tool is designed to run as **you** — your own `az login` (delegated user) — with
only the minimum RBAC needed. No Service Principal secret and no highly-privileged
"management" account are required.

**Token source, cheapest first** (`arm_token()`): disk cache → service principal
*(optional,* `AZURE_*` *env)* → managed identity (IMDS) → `az` (your delegated login).
Force one with `--auth {auto,az,managed-identity,service-principal}`. Nothing is
stored but a short-lived, identity-scoped token (0600 temp cache;
`--no-token-cache` to disable). A long rollout that outlives the token fails
cleanly — re-auth and re-run with the same `--state-file` to resume.

> **Single-identity vs. delegated.** A **managed identity** or **service principal**
> is *one* identity: its token only reaches subscriptions it was explicitly granted,
> and it **cannot** be swapped per tenant. So under `--auth managed-identity`/`-sp`
> the tool enumerates and operates **only** on that identity's subscriptions — it
> will not pull in cross-tenant subscriptions from `az` (which is a *different*
> identity; mixing them produces `InvalidAuthenticationTokenTenant` 401s). If your
> `--subscription` scope matches none of the identity's subscriptions, the tool
> lists what it *can* reach and tells you to grant access or use `--auth az`.
> **Delegated `az`** (a user logged into multiple tenants) *does* resolve
> cross-tenant subscriptions, per-subscription, incl. guest / Azure Lighthouse.

**Minimum RBAC** — two custom roles are provided in [`roles/`](roles/), scoped to the
*exact* actions the tool calls (nothing else — no site create/delete, scaling,
networking, slots, or publish-credential access):

| Role | Use | Adds over read |
|---|---|---|
| `dt-fleet-assessor.json` | `assess` (dry run) | — (read-only) |
| `dt-fleet-deployer.json` | `deploy` / `uninstall` | `config/write`, `restart/action` |

```bash
# create the role (edit AssignableScopes first — prefer an RG scope), then assign to the user
az role definition create --role-definition roles/dt-fleet-deployer.json
az role assignment create --assignee you@tenant.com \
    --role "DT Fleet Deployer" \
    --scope /subscriptions/<sub>/resourceGroups/<rg>
```

Assess needs only `*/read` + `config/list/action` + Resource Graph; deploy adds just
`Microsoft.Web/sites/config/write` and `Microsoft.Web/sites/restart/action`. Scope the
assignment to the target resource group(s) for tightest blast radius.

## Performance (the `az` CLI tax)

On resource-starved hosts, **each `az` invocation can take 60–200s** — not from the
API (that's ~3s) but from the `az` CLI loading its command modules on every call
(you'll see `Module 'appservice' load timeout` warnings). At fleet scale this reads
as "stuck." Remedies, in order of impact:

1. **`--rest` (biggest win).** Runs inventory, enrich, **and deploy/uninstall
   writes** through the ARM REST API — a warm `--rest` run makes **zero `az` calls**.
   Measured here: config read **0.7s** vs `az` **113s**; a 4-app `--detailed` enrich
   dropped from **~5.5 min to ~2s**; a single-app deploy ~100s vs ~14 min. Reads
   parallelize to 16 workers. Use it whenever `az` is slow:
   ```bash
   python3 dt_appservice_fleet.py assess --all-subscriptions --detailed --rest
   python3 dt_appservice_fleet.py deploy --resource-group <rg> --rest --yes
   ```
   **Token acquisition** (cheapest source first): disk cache (0600 temp file, ~60-min
   reuse) → service principal (`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET`/`AZURE_TENANT_ID`
   env) → managed identity (IMDS) → `az account get-access-token` (slow fallback,
   also cached). So only the *first* run per ~hour (without SP/MI) pays any `az` cost.
   `--no-token-cache` disables the disk cache; a 401 auto-retries with a fresh token.

   > On a resource-starved host even `--rest` can look slow — but that's the host
   > importing Python's own stdlib modules (same module-load pathology as `az`),
   > not the tool. On a healthy runner a warm `--rest` run is ~3–5s.
2. **Verbose to confirm it's `az`, not the script.** `-v` prints each call's
   duration; `(slow: az … took 120s)` is surfaced even without `-v`.
3. **Fix the host.** The `user` CPU time is ~3s, so it's I/O/CPU starvation — run on
   a box with a fast local disk and adequate CPU; avoid many concurrent `az`
   processes competing (the tool already caps enrich at 3 workers on the `az` path).
4. **Trim per-call `az` overhead.** `az config set core.collect_telemetry=false`,
   `az config set core.only_show_errors=true`, and pre-install the `resource-graph`
   extension so there's no dynamic-install check.

## How instrumentation works (and the gotchas it encodes)

Deploy sets a **self-contained Startup Command** and supporting app settings.

Startup Command (stored on the app; `$VARS` expand from app settings at boot):

```sh
wget --no-check-certificate -O /tmp/dt-oneagent-install.sh \
  "$DT_ENDPOINT/api/v1/deployment/installer/agent/unix/paas-sh/latest?Api-Token=$DT_API_TOKEN&flavor=$DT_FLAVOR&include=$DT_INCLUDE&arch=x86" \
  && sh /tmp/dt-oneagent-install.sh \
  && LD_PRELOAD=/opt/dynatrace/oneagent/agent/lib64/liboneagentproc.so $START_APP_CMD
```

App settings written:

| Setting | Value / purpose |
|---|---|
| `DT_ENDPOINT` | `https://qof78400.live.dynatrace.com` |
| `DT_API_TOKEN` | PaaS token with **InstallerDownload** scope |
| `DT_INCLUDE` | `nodejs` / `python` / `dotnet` / `java` — inferred from runtime |
| `DT_FLAVOR` | `default` (glibc; App Service built-in images are Debian) |
| `START_APP_CMD` | the app's **original** launch command (captured on deploy) |
| `DT_LOGSTREAM=stdout`, `DT_LOGLEVELCON=INFO` | push agent logs to the App Service log stream (container FS is ephemeral) |
| `OTEL_SERVICE_NAME` | service entity name, distinct from the process group |
| `DT_TAGS` | `managed-by=dt-fleet env=<tag>` |
| `DT_FLEET_ORIG_STARTUP` | original Startup Command, for clean uninstall |

Then the app is **restarted twice** (once to install, once to instrument).

### Gotchas honored

1. **Web App vs Function App.** Classified by `kind`; `functionapp*` is never
   touched — the startup/LD_PRELOAD path does nothing there.
2. **Startup command *swap*, not chain.** The real launch command moves into
   `START_APP_CMD`; the wrapper execs it as its final step with `LD_PRELOAD` set.
3. **`LD_PRELOAD` is inline, never an app setting.** A container-wide
   `LD_PRELOAD` app setting fires before `/opt/dynatrace` exists and spams
   `cannot be preloaded` errors on oryx/wget/sh. We set it inline for the app
   process only. *(This is the one correction vs. the attached screenshot, which
   used an `LD_PRELOAD` app setting — the sidecar-tab pattern.)*
4. **`START_APP_CMD` must be explicit.** An app that relied on the image's
   implicit default start will not start once a Startup Command is present, so we
   capture the existing command (and refuse, with `needs-input`, when there is
   none and none is supplied).
5. **Fail loud.** `&&`-chained: if download or install fails the app does not
   launch, rather than silently running un-instrumented.
6. **Ephemeral `/opt/dynatrace`.** The agent lives in the container filesystem and
   is re-downloaded on every cold start — acceptable for the built-in path; noted
   as the known coupling (gotchas §10).

---

## Demo apps

`provision_demo_apps.sh` creates (names suffixed with a subscription fragment):

| App | Runtime | Framework | Startup command |
|---|---|---|---|
| `dtfleet-node-express-*` | NODE:22-lts | Express | `node server.js` |
| `dtfleet-node-fastify-*` | NODE:24-lts | Fastify | `node server.js` |
| `dtfleet-py-flask-*` | PYTHON:3.11 | Flask | `gunicorn --bind=0.0.0.0:8000 app:app` |
| `dtfleet-py-fastapi-*` | PYTHON:3.12 | FastAPI | `gunicorn -k uvicorn.workers.UvicornWorker --bind=0.0.0.0:8000 main:app` |

All four expose the same CRUD surface: `GET /`, `GET /health`, `GET/POST /items`,
`GET/PUT/DELETE /items/{id}`.

```bash
RG=dt-oneagent-fleet-demo NTFY_TOPIC=the-score-of-the-hms-pinafore-1 ./provision_demo_apps.sh
python3 generate_load.py --resource-group dt-oneagent-fleet-demo --duration 600 --rate 2
```

---

## Prerequisites

- `az` CLI, authenticated (`az login`) with reader+contributor on the targets.
- `creds.json` with `credentials.dtApiEnvUrl` and `credentials.dtPaasToken`
  (PaaS token, InstallerDownload scope).
- `python3`, `zip`, `curl`, `jq`.
