# Dynatrace OneAgent — Azure Linux App Service Fleet Toolkit

> ⚠️ **Disclaimer:** These are field-developed community scripts. They are **not** official
> Dynatrace tools and are **not** supported by Dynatrace. Use at your own discretion.

Automated, at-scale deployment of the Dynatrace OneAgent to **any Linux Azure App
Service** — both **Publish: Code** (built-in image) and **Publish: Container** (BYOC) —
plus the assessment tooling to decide *where* it can go and *how*.

One unified entry point (`dt_appservice.py`) auto-routes each app to the mechanism Azure
actually allows for it:

- **Publish: Code** → startup-command wrapper, **or** `--leave-startup-command` (inject via
  an `LD_PRELOAD` app setting, Startup Command untouched), **or** `--preserve-oryx`.
- **Publish: Container** → the `dynatrace-codemodules` **bootstrapper sidecar** (a sidecar
  can only attach to a container app — Azure requires it — so code apps never get one).

Target Dynatrace environment: `qof78400` (`https://qof78400.live.dynatrace.com`).
Credentials (`dtApiEnvUrl`, `dtPaasToken`) are read from `creds.json`.

**→ In a hurry? See [QUICKSTART.md](QUICKSTART.md).**

---

## Contents

| File | Purpose |
|---|---|
| `dt_appservice.py` | **Unified entry (recommended).** One `assess`/`deploy`/`verify`/`uninstall`/`update` that **auto-routes each app**: Publish: Code → code path, Publish: Container → sidecar path. See [Unified tool](#unified-tool-code--container). |
| `dt_appservice_fleet.py` | Code path (built-in image) — used directly or via the unified tool. `assess`/`deploy`/`verify`/`uninstall`. |
| `dt_appservice_sidecar.py` | Sidecar path (containers/BYOC) — used directly or via the unified tool. `assess`/`deploy`/`verify`/`uninstall`/`update`. See [Container apps (sidecar path)](#container-apps-sidecar-path). |
| `provision_demo_apps.sh` | Stand up 4 demo App Services (2 Python, 2 Node — distinct frameworks). |
| `provision_demo_containers.sh` | Stand up demo **container** Web Apps (sitecontainers + legacy DOCKER\| modes) for the sidecar path. |
| `generate_load.py` | Drive CRUD traffic against the apps so OneAgent has requests to trace. |
| `apps/` | Source for the 4 demo apps (Express, Fastify, Flask, FastAPI). |
| `reports/` | Generated assessment reports (`.md` / `.json` / `.csv`). |

---

## Unified tool (code + container)

`dt_appservice.py` is the recommended entry point: **one command over a mixed fleet** that
inventories once, classifies each app by its Azure **Publish** model, and dispatches to the
mechanism that actually works for it (a sidecar can only attach to a Publish: Container app
— Azure requires it — so code apps never go down the sidecar path):

| Publish model | Route | How the agent loads |
|---|---|---|
| **Code** (built-in image) | code path | startup-command wrapper, **or** `--leave-startup-command` (LD_PRELOAD app setting, command untouched), **or** `--preserve-oryx` |
| **Container** (BYOC) | sidecar path | `dynatrace-codemodules` bootstrapper sidecar + LD_PRELOAD app setting |

```bash
# one dry run across everything — see how each app would be routed
python3 dt_appservice.py assess --all-subscriptions --rest

# deploy the whole mixed fleet; each app auto-routed, with inline verify + auto-rollback
python3 dt_appservice.py deploy --subscription prod --rest --yes --verify --rollback-on-fail

# code apps: keep their startup command, inject via LD_PRELOAD (containers ignore this flag)
python3 dt_appservice.py deploy --subscription prod --rest --yes --leave-startup-command \
    --verify --rollback-on-fail
```

Every flag of both paths is accepted; each app consumes only the ones relevant to its route
(`--leave-startup-command`/`--preserve-oryx`/`--start-app-cmd` for code; `--allow-convert`/
`--enable-storage`/`--technology`/`--oneagent-version`/… for containers). `update` targets
the sidecar (container) apps. The two underlying tools remain usable standalone.

**Instrumenting code apps without touching the Startup Command.** `--leave-startup-command`
installs the agent to `/home` in one install boot, then **restores the app's original
startup command** and injects via an `LD_PRELOAD` app setting (same model the Dynatrace
Kubernetes Operator uses). Steady state = your exact command + `LD_PRELOAD` + agent on
`/home`; the wrapper is only transient. Trade-off: `LD_PRELOAD` is container-wide and
updates are manual — vs the default wrapper which owns the command but is self-installing.

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
`--restart-wait` sleep dominates the `--rest` path, so the two levers are
**`--parallel N`** (deploy N apps at once — wall-clock ≈ sequential-time ÷ N, since
the restart-waits overlap) and a lower `--restart-wait`. ARM 429 throttling is retried
automatically and the `--state-file` checkpoint is thread-safe; keep `--parallel` low
on the `az` path. The JSON report includes `estimated_deploy_seconds` for scripting.

## 2. Deploy OneAgent

Mutating. This section covers the **code path** (`dt_appservice_fleet.py`) — Linux
**Publish: Code** Web Apps. Container apps go through the [sidecar path](#container-apps-sidecar-path),
and the [unified tool](#unified-tool-code--container) routes either automatically. Deploy
honors every gotcha in `dynatrace-appservice-linux-gotchas.md` (see "How instrumentation
works" below).

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
*no* existing Startup Command — see Node gotcha), `--resilient` (persist agent to
`/home`, fail-open), `--leave-startup-command` (instrument via an `LD_PRELOAD` app
setting and leave the Startup Command untouched — see [Unified tool](#unified-tool-code--container)),
`--rollback-on-fail` (auto-revert an app left not-serving after `--verify`),
`--env-tag`, `--restart-wait`, `-v/--verbose`, `--rest`.

**Apps with no Startup Command (Oryx implicit default).** The wrapper must `exec` a
real launch command. Two ways to supply it:

- `--start-app-cmd "<cmd>"` — you provide it (one value applies to the whole run).
- `--discover-start-cmd` — read the app's files via Kudu and replicate Oryx's logic
  (needs SCM/Kudu reachable).

When SCM is IP-restricted and the command is unknown, either copy the real launch from
the **Azure Portal → Log stream** (`Generating 'gunicorn' command for '<module>:app'`)
into `--start-app-cmd`, **or** use `--preserve-oryx` (no command needed):

- `--preserve-oryx` — **Node/Python, no startup command, locked-down SCM.** Installs the
  agent to persistent `/home`, then invokes the platform's **own Oryx** at boot to
  regenerate and run the app with `LD_PRELOAD` injected. Needs no launch command or file
  access. Handles compressed Oryx builds (`output.tar.zst`) by pointing Oryx at the
  platform's already-extracted app dir (not `wwwroot`, which would re-extract and
  collide). The `AppCommandLine` stays within Azure's 1024-char limit.
- `--rollback-on-fail` — **the safety net; always pair it with `--preserve-oryx` +
  `--verify`.** After deploy it probes each app's public URL and, if an app is not
  serving HTTP (our startup change broke it), automatically `uninstall`s to restore its
  prior state. This is independent of the Dynatrace/agent verdict — the OneAgent can load
  and connect while the app command is broken and the app is down, so health is judged by
  an HTTP probe, not by "did the agent attach". A rollout can never leave an app down.

An app logging `No framework detected; using default app` has *no code deployed*;
instrument it once the application is actually there.

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
3. **`LD_PRELOAD` is inline by default, never set before the agent exists.** A
   container-wide `LD_PRELOAD` app setting that fires before the `.so` is present
   spams `cannot be preloaded` errors on oryx/wget/sh, so the default wrapper sets
   it inline for the app process only. **Exception — `--leave-startup-command`**
   *deliberately* uses an `LD_PRELOAD` app setting, but only **after** staging the
   agent on `/home`, so there is no missing-`.so` window (and the sidecar path uses
   it too, since the customer image's entrypoint can't be touched).
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

## Container apps (sidecar path)

`dt_appservice_fleet.py` instruments Linux **code/built-in-image** apps. The apps it
inventories and *skips* — **containerized (bring-your-own-container) Linux Web Apps** —
are handled by a separate, independent fork, **`dt_appservice_sidecar.py`**, which
attaches the Dynatrace **`dynatrace-codemodules` bootstrapper as a sidecar container**
(per the [Dynatrace doc](https://docs.dynatrace.com/docs/ingest-from/microsoft-azure-services/integrations/azure-appservice/integrate-oneagent-on-web-app-for-containers)).
A given app is instrumented by exactly one path, never both.

### The two paths at a glance

| | Code path (`dt_appservice_fleet.py`) | Sidecar path (`dt_appservice_sidecar.py`) |
|---|---|---|
| Targets | Linux built-in image / code apps | Linux container (BYOC) apps (`sitecontainers`, legacy `DOCKER\|`) |
| Agent delivery | PaaS installer downloaded in a startup-command wrapper | `dynatrace-codemodules` **sidecar** writes the agent to `/home` |
| `LD_PRELOAD` | set **inline** in the startup command (not an app setting) | set as an **app setting** (can't touch the customer image's entrypoint) |
| Token | PaaS token (installer) | **tenant token** (NOT PaaS — PaaS token here fails) + connection endpoints |
| Restarts | 2 (install, instrument) | 2 (bootstrap-settle, instrument) |
| Updates | re-run `deploy` (re-downloads) | `update` → new sidecar image tag |
| Uninstall | restores original startup command | deletes sidecar + settings; agent files remain under `/home` |

### Usage

```bash
# 1) assess — per-container-app readiness (never mutates)
python3 dt_appservice_sidecar.py assess --all-subscriptions --rest

# 2) deploy — sitecontainers-mode apps, with inline verify + auto-rollback
python3 dt_appservice_sidecar.py deploy --subscription prod \
    --rest --yes --verify --rollback-on-fail

# 3) verify / uninstall / update
python3 dt_appservice_sidecar.py verify    --subscription prod --rest
python3 dt_appservice_sidecar.py uninstall --subscription prod --rest --yes
python3 dt_appservice_sidecar.py update    --subscription prod --rest --yes   # newest codemodules tag
```

Readiness statuses: **sidecar-ready** · **needs-conversion** (legacy `DOCKER|` → pass
`--allow-convert`) · **needs-storage** (pass `--enable-storage`) · **needs-review**
(Alpine/musl — pass `--allow-alpine`) · **needs-input** (foreign `LD_PRELOAD`) ·
**unsupported-runtime** (Python → OTel path, skipped) / **-compose** / **-sku** ·
**instrumented**.

### Key flags

| Flag | Effect |
|---|---|
| `--oneagent-version <tag>` | pin the codemodules image (default: newest from Docker Hub; `latest` refused) |
| `--technology <t>` / `--technology-map <file>` | bootstrapper `--technology=` (java/dotnet/nodejs/php/go/apache/nginx). A specific value is **much faster** than the `all` default |
| `--image-registry <host/path>` / `--registry-auth` | pull the sidecar from a mirror (ACR) — recommended for fleets (Docker Hub anon pull is 100/6h/IP) |
| `--enable-storage` | flip `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` (required; bootstrapper skips silently without it) |
| `--allow-convert` / `--revert-conversion` | convert legacy `DOCKER|` → `sitecontainers` (forces `--parallel 1`) / restore it on uninstall |
| `--disable-app-insights` | remove conflicting App Insights auto-instrumentation (stashed for uninstall) |
| `--rollback-on-fail` | with `--verify`: uninstall any app not serving HTTP after deploy (independent of the agent verdict) |
| `--sidecar-wait <s>` | max wait for the agent to land on `/home` (the tool **polls** and proceeds as soon as it appears) |
| `--include-slots` · `--keyvault-ref <vault>` · `--prune-old-versions` | apply to slots · store the tenant token as a Key Vault reference · delete stale agent versions |

### Hard edges (customer-facing)

- **Tenant token ≠ PaaS token** — the #1 silent failure. The tool derives the tenant
  token + endpoints from the PaaS token via the connectioninfo API automatically.
- **Shared storage is load-bearing**: `LD_PRELOAD` loads the `.so` from an SMB mount on
  every spawn; cold-start latency and Azure Files availability become dependencies.
- **Supported runtime matters**: the agent injects + connects but will *decline* an
  unsupported runtime (e.g. it refuses .NET 11), so instrument images on a supported
  stack. The agent's own log (`/home/dynatrace/oneagent/active/log/<tech>/ruxitagent_*`)
  tells "declined" from "never loaded".
- **No auto agent updates** on this path — every bump is an `update` (image-tag change);
  old versions accumulate under `/home/dynatrace` (`--prune-old-versions`).
- **App Insights** auto-instrumentation must be *off*, not merely unconfigured.
- **VNet-integrated apps** need egress to the image registry and `DT_CONNECTION_POINT`.
- **Python containers** are out of scope (OTel path, not implemented here).

### Least privilege

`roles/dt-fleet-assessor.json` already includes the read permissions the sidecar `assess`
needs (sitecontainers / slots / serverfarms read). The write path additionally needs
`Microsoft.Web/sites/sitecontainers/write` + `.../delete` (deploy role).

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
