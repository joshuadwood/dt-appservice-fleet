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

## 1. Assess first (dry run — never changes anything)

```bash
# whole tenant, fast, to console
python3 dt_appservice_fleet.py assess --all-subscriptions --rest

# scope + per-app startup/DT state + write a report (format from file extension)
python3 dt_appservice_fleet.py assess \
    --subscription-filter prod --name-filter "web-*" \
    --detailed --rest --output-file reports/prod.md
```

## 2. Preview the deploy

```bash
python3 dt_appservice_fleet.py deploy \
    --subscription-filter prod --name-filter "web-*" --rest --what-if
```

## 3. Deploy (resumable)

```bash
python3 dt_appservice_fleet.py deploy \
    --subscription-filter prod --name-filter "web-*" \
    --rest --resilient --state-file rollout.jsonl --yes -v
```
Interrupted? Re-run the **exact same command** — it skips the apps already done
(`--state-file`) and continues.

## 4. Uninstall (revert)

```bash
python3 dt_appservice_fleet.py uninstall \
    --subscription-filter prod --name-filter "web-*" --rest --yes
```

---

## The flags you'll actually use

| Flag | What it does |
|---|---|
| `--subscription` / `--subscription-filter` / `--all-subscriptions` | scope by subscription (filter = name substring, or glob with `* ? [`) |
| `--name-filter` / `--resource-group` | narrow to specific apps (name supports globs) |
| `--what-if` | show what *would* happen, change nothing |
| `--rest` | use ARM REST instead of `az` — **~30× faster when `az` is slow** |
| `--resilient` | persist agent to `/home` (no re-download on restart) + fail-open |
| `--state-file FILE` | resumable rollout — completed apps skipped on re-run |
| `--force` | re-apply to already-instrumented apps (default skips them) |
| `-v` / `--verbose` | echo every call + timing (tokens redacted) — **use if it looks stuck** |
| `--start-app-cmd "<cmd>"` | required only for apps with **no** existing startup command |
| `--set-host-tag K=V` | extra Dynatrace `[Environment]K:V` tag (repeatable) |
| `--detailed` | assess: fetch per-app startup cmd + DT state |

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

- **Always `assess` before `deploy`.** Deploy only touches **Linux code** apps;
  Function Apps and containers are reported and skipped.
- **Idempotent + resumable.** Re-running `deploy` skips already-instrumented apps;
  with `--state-file` it also skips apps completed in a prior run.
- **"Seems stuck"?** Add `-v`. On a starved host `az` takes 100–200s/call — use
  `--rest`. On a healthy host a warm `--rest` run is a few seconds.

## Demo apps (optional)

```bash
RG=dt-oneagent-fleet-demo ./provision_demo_apps.sh                 # 4 apps: Express/Fastify/Flask/FastAPI
python3 generate_load.py --resource-group dt-oneagent-fleet-demo --duration 600
```
