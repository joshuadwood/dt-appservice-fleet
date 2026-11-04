#!/usr/bin/env python3
"""
DT Fleet — SIDECAR path (containerized Linux App Services / BYOC).

A fork of dt_appservice_fleet.py that instruments **container** Web Apps (the apps the
code/core tool inventories and skips) with the Dynatrace OneAgent via the
`dynatrace/dynatrace-codemodules` bootstrapper sidecar — per
https://docs.dynatrace.com/docs/ingest-from/microsoft-azure-services/integrations/azure-appservice/integrate-oneagent-on-web-app-for-containers

Design: the two paths stay INDEPENDENT. This script never touches the code/core deploy
logic; it reuses only the stable scaffolding (subscription enumeration, the ARM REST
client + token cache, inventory/classification, tagging, report plumbing) by importing
dt_appservice_fleet as a library. A given app is either startup-command-instrumented
(the other tool) or sidecar-instrumented (this one), never both.

Scope (this build = Phase 1, DISCOVERY): `assess` reports per-container-app sidecar
readiness. deploy/verify/uninstall/update are defined but gated as not-yet-implemented
(Phases 2-3). Python-runtime containers are reported `unsupported-runtime` and skipped
(they belong on the OTel path, which this tool does not implement). Function Apps,
Windows, and code/core apps are out of scope and ignored.
"""
import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import dt_appservice_fleet as core
from dt_appservice_fleet import log, vlog, redact

# sitecontainers ARM schema is richest at 2024-11-01 (GA); core.ARM_API (2023-12-01)
# also supports the resource but we pin the newer version for writes in Phase 2.
SITECONTAINER_API = "2024-11-01"

# The fixed contract from the Dynatrace doc (see sidecar-arm-facts memory).
DT_LIB_PATH = "/home/dynatrace/oneagent/active/agent/lib64/liboneagentproc.so"
SIDECAR_IMAGE_BASE = "index.docker.io/dynatrace/dynatrace-codemodules"
SIDECAR_SUCCESS_LOG = "OneAgent has been successfully deployed"
DEFAULT_SIDECAR_NAME = "dynatrace-bootstrapper"
# Dynatrace --technology identifiers (reduce bootstrapper footprint).
DT_TECHNOLOGIES = {"java", "dotnet", "nodejs", "php", "apache", "nginx", "go", "sdk", "all"}


# ----------------------------------------------------------------- credentials / DT API

_CONN_INFO = {}


def dt_connection_info(creds, network_zone=None):
    """Fetch tenant token + communication endpoints from the connectioninfo API using
    the PaaS token creds.json already holds. The sidecar path needs the TENANT token
    (not the PaaS token) and the connection endpoints. Cached per run."""
    key = network_zone or "_"
    if key in _CONN_INFO:
        return _CONN_INFO[key]
    base = creds["env_url"].rstrip("/")
    url = f"{base}/api/v1/deployment/installer/agent/connectioninfo"
    if network_zone:
        url += "?" + urllib.parse.urlencode({"networkZone": network_zone})
    req = urllib.request.Request(url, headers={"Authorization": f"Api-Token {creds['paas_token']}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SystemExit("connectioninfo API rejected the PaaS token — it needs the "
                             "InstallerDownload scope. (HTTP %d)" % e.code)
        raise SystemExit(f"connectioninfo API error: HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"connectioninfo API unreachable: {e}")
    endpoints = data.get("communicationEndpoints") or []
    info = {
        "tenant": data.get("tenantUUID") or "",
        "tenant_token": data.get("tenantToken") or "",
        "connection_point": ";".join(endpoints),
        "network_zone": network_zone or "",
    }
    if not info["tenant"] or not info["tenant_token"] or not info["connection_point"]:
        raise SystemExit("connectioninfo API response missing tenantUUID/tenantToken/"
                         "communicationEndpoints — check the environment URL + token.")
    vlog(f"[dt] connectioninfo: tenant={info['tenant']} "
         f"token={redact('DT_TENANTTOKEN=' + info['tenant_token'])} "
         f"endpoints={len(endpoints)}")
    _CONN_INFO[key] = info
    return info


# -------------------------------------------------------------------- ARM: sitecontainers

def _sc_call(method, app, sub_path="", body=None, api=SITECONTAINER_API):
    """ARM call against the site (or a sitecontainers sub-path) with the sitecontainers
    api-version. Returns parsed JSON or None."""
    url = f"{core.ARM}{app['id']}{sub_path}?api-version={api}"
    return core._arm_call(method, url, body, 60, app.get("subscription_id"))


def list_sitecontainers(app):
    out = _sc_call("GET", app, "/sitecontainers")
    if isinstance(out, dict):
        return out.get("value", []) or []
    return []


# --------------------------------------------------------------------------- discovery

# image-name heuristics -> Dynatrace --technology
_TECH_PATTERNS = [
    ("python", ("python", "fastapi", "flask", "django", "gunicorn", "uvicorn")),
    ("java", ("openjdk", "temurin", "eclipse-temurin", "jre", "jdk", "jetty", "tomcat", "spring")),
    ("dotnet", ("dotnet", "aspnet", "/dotnet/")),
    ("nodejs", ("node", "nodejs")),
    ("php", ("php", "wordpress")),
    ("go", ("golang", "/go:")),
    ("nginx", ("nginx",)),
    ("apache", ("httpd", "apache")),
]


def technology_hint(image, app_settings, tech_map=None, name=""):
    """Best-effort runtime guess from the image name/tag (+ optional --technology-map).
    Returns a Dynatrace technology id or "" if unsure. 'python' here is meaningful: it
    marks the app unsupported on this path."""
    if tech_map:
        for pat, tech in tech_map.items():
            if core._matches_any([pat], name):
                return tech
    img = (image or "").lower()
    for tech, needles in _TECH_PATTERNS:
        if any(n in img for n in needles):
            return tech
    return ""


def _is_container_candidate(r):
    """True for Linux (non-Function) apps the sidecar path targets. core.classify()
    predates sitecontainers mode: it marks legacy DOCKER| apps as deploy_model=container,
    but a converted `linuxFxVersion=sitecontainers` app comes back kind 'app,linux'
    family 'SITECONTAINERS' (and Compose as 'COMPOSE') — so catch those explicitly."""
    if not r.get("is_linux") or r.get("is_function"):
        return False
    return (r.get("deploy_model") == "container"
            or r.get("runtime_family") in ("SITECONTAINERS", "COMPOSE", "CONTAINER"))


def _container_mode(linux_fx):
    fx = (linux_fx or "")
    up = fx.upper()
    if up == "SITECONTAINERS":
        return "multi"
    if up.startswith("DOCKER|"):
        return "legacy-single"
    if up.startswith("COMPOSE|"):
        return "compose"
    return "none"


def sidecar_readiness(app, sidecar_name=DEFAULT_SIDECAR_NAME, tech_map=None):
    """Populate app['sidecar'] with the readiness block (plan §3). Independent ARM reads;
    ~5 calls per app. Also sets app['dt_instrumented'] / app['sidecar_status']."""
    s = app.get("subscription_id")
    r = {
        "container_mode": "none", "main_image": "", "app_service_storage": "absent",
        "existing_sitecontainers": [], "dt_instrumented": False, "dt_version": "",
        "dt_settings_present": False, "foreign_ld_preload": "", "app_insights": False,
        "vnet": {}, "egress_review": False, "slots": [], "technology_hint": "",
        "alpine_hint": False, "always_on": None, "sku_tier": "", "sku_capacity": None,
        "sku_unsupported": False,
    }

    # 1) full site GET: siteConfig (linuxFxVersion, vnet, alwaysOn), serverFarmId
    site = core.arm_resource("GET", app["id"], subscription=s)
    props = site.get("properties", {}) if isinstance(site, dict) else {}
    cfg = props.get("siteConfig", {}) or {}
    linux_fx = cfg.get("linuxFxVersion") or app.get("linuxFx") or ""
    r["container_mode"] = _container_mode(linux_fx)
    r["always_on"] = cfg.get("alwaysOn")
    r["vnet"] = {
        "route_all": cfg.get("vnetRouteAllEnabled"),
        "subnet": props.get("virtualNetworkSubnetId") or "",
        "public_access": props.get("publicNetworkAccess") or "",
    }
    r["egress_review"] = bool(r["vnet"]["route_all"] or r["vnet"]["subnet"])
    server_farm_id = props.get("serverFarmId") or ""

    # legacy single image comes straight from linuxFxVersion
    if r["container_mode"] == "legacy-single":
        r["main_image"] = linux_fx.split("|", 1)[1] if "|" in linux_fx else ""

    # 2) app settings: storage, DT_*, App Insights, foreign LD_PRELOAD
    setres = core.arm_resource("POST", app["id"], "/config/appsettings/list", subscription=s)
    settings = setres.get("properties", {}) if isinstance(setres, dict) else {}
    app["app_settings"] = settings
    r["app_service_storage"] = (settings.get("WEBSITES_ENABLE_APP_SERVICE_STORAGE")
                                or "absent").lower() if "WEBSITES_ENABLE_APP_SERVICE_STORAGE" in settings else "absent"
    dt_keys = ("DT_CONNECTION_POINT", "DT_TENANT", "DT_TENANTTOKEN")
    r["dt_settings_present"] = any(k in settings for k in dt_keys) or ("LD_PRELOAD" in settings)
    ldp = settings.get("LD_PRELOAD", "")
    if ldp and ldp.strip() != DT_LIB_PATH:
        r["foreign_ld_preload"] = ldp
    r["app_insights"] = any(k in settings for k in (
        "APPLICATIONINSIGHTS_CONNECTION_STRING", "APPINSIGHTS_INSTRUMENTATIONKEY",
        "ApplicationInsightsAgent_EXTENSION_VERSION"))

    # 3) existing sitecontainers (multi mode): main image + prior DT sidecar
    if r["container_mode"] == "multi":
        for sc in list_sitecontainers(app):
            scp = sc.get("properties", {}) or {}
            img = scp.get("image", "")
            nm = sc.get("name", "")
            r["existing_sitecontainers"].append({"name": nm, "image": img,
                                                 "is_main": scp.get("isMain")})
            if scp.get("isMain"):
                r["main_image"] = img
            if "dynatrace-codemodules" in img.lower() or nm == sidecar_name:
                r["dt_instrumented"] = True
                r["dt_version"] = img.rsplit(":", 1)[-1] if ":" in img else ""

    # 4) slots
    slotres = core.arm_resource("GET", app["id"], "/slots", subscription=s)
    if isinstance(slotres, dict):
        r["slots"] = [v.get("name", "").split("/")[-1] for v in slotres.get("value", [])]

    # 5) serverfarm SKU
    if server_farm_id:
        farm = core.arm_resource("GET", server_farm_id, subscription=s)
        sku = (farm.get("sku", {}) if isinstance(farm, dict) else {}) or {}
        r["sku_tier"] = sku.get("tier", "") or sku.get("name", "")
        r["sku_capacity"] = sku.get("capacity")
        r["sku_unsupported"] = r["sku_tier"].lower() in ("free", "shared", "dynamic")

    # derived: technology + alpine
    r["technology_hint"] = technology_hint(r["main_image"], settings, tech_map, app["name"])
    r["alpine_hint"] = any(t in (r["main_image"] or "").lower() for t in ("alpine", "musl"))

    app["sidecar"] = r
    app["dt_instrumented"] = r["dt_instrumented"]
    app["sidecar_status"], app["sidecar_reason"], app["deployable_sidecar"] = \
        classify_sidecar_status(app, r)
    return r


def classify_sidecar_status(app, r):
    """Single primary status + reason + deployable_sidecar bool. Precedence matters:
    report the most important blocker first."""
    if r["dt_instrumented"]:
        return "instrumented", f"Dynatrace sidecar present ({r['dt_version'] or 'unknown tag'})", False
    if r["container_mode"] == "none":
        return "not-container", "not a container app (out of scope for the sidecar path)", False
    if r["container_mode"] == "compose":
        return "unsupported-compose", "Docker Compose multi-container — unsupported by sidecars", False
    if r["technology_hint"] == "python":
        return "unsupported-runtime", "Python container — use the OTel path (not this tool)", False
    if r["sku_unsupported"]:
        return "unsupported-sku", f"SKU '{r['sku_tier']}' does not support sidecars (needs Basic+)", False
    if r["foreign_ld_preload"]:
        return "needs-input", f"foreign LD_PRELOAD set ({r['foreign_ld_preload']}) — resolve manually", False
    # From here the app is fundamentally eligible; remaining items are approved config changes.
    deployable = True
    if r["container_mode"] == "legacy-single":
        status, reason = "needs-conversion", "legacy single-container (DOCKER|) — needs --allow-convert to sitecontainers"
    elif r["app_service_storage"] != "true":
        status, reason = "needs-storage", f"WEBSITES_ENABLE_APP_SERVICE_STORAGE={r['app_service_storage']} — needs --enable-storage"
    elif r["alpine_hint"]:
        status, reason = "needs-review", "Alpine/musl image — sidecar musl support unverified (use --allow-alpine)"
    else:
        status, reason = "sidecar-ready", "multi-container, storage on, supported runtime"
    extra = []
    if r["app_insights"]:
        extra.append("App Insights present (conflict — use --disable-app-insights)")
    if r["egress_review"]:
        extra.append("VNet egress — verify reachability of Docker Hub + DT_CONNECTION_POINT")
    if r["slots"]:
        extra.append(f"{len(r['slots'])} slot(s): {','.join(r['slots'])}")
    if extra:
        reason += "  [" + "; ".join(extra) + "]"
    return status, reason, deployable


# ------------------------------------------------------------------------------ report

_STATUS_BUCKET = {
    "sidecar-ready": "sidecar-ready",
    "needs-conversion": "needs-conversion",
    "needs-storage": "needs-conversion",
    "needs-review": "needs-review",
    "instrumented": "instrumented",
    "unsupported-runtime": "unsupported",
    "unsupported-compose": "unsupported",
    "unsupported-sku": "unsupported",
    "needs-input": "needs-review",
}


def render_sidecar_report(records, fmt="txt"):
    containers = [r for r in records if r.get("sidecar_status") not in (None, "not-container")]
    if fmt == "json":
        return json.dumps([{
            "name": r["name"], "subscription": r.get("subscription_name"),
            "resource_group": r["resource_group"], "status": r["sidecar_status"],
            "deployable_sidecar": r.get("deployable_sidecar"),
            "reason": r.get("sidecar_reason"), "readiness": r.get("sidecar"),
        } for r in containers], indent=2)

    buckets = {}
    for r in containers:
        buckets.setdefault(_STATUS_BUCKET.get(r["sidecar_status"], "other"), []).append(r)
    lines = []
    lines.append("DT Fleet — Sidecar (container) readiness")
    lines.append("=" * 72)
    total = len(containers)
    lines.append(f"{total} container Linux App Service(s) in scope")
    order = ["sidecar-ready", "needs-conversion", "needs-review", "instrumented", "unsupported", "other"]
    for b in order:
        if buckets.get(b):
            lines.append(f"  {b:16} {len(buckets[b])}")
    lines.append("")
    hdr = f"{'STATUS':17} {'MODE':13} {'TECH':8} {'SKU':10} {'APP'}"
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for r in sorted(containers, key=lambda x: (x["sidecar_status"], x["name"])):
        sc = r.get("sidecar", {})
        lines.append(f"{r['sidecar_status']:17} {sc.get('container_mode',''):13} "
                     f"{sc.get('technology_hint','') or '?':8} {sc.get('sku_tier','') or '?':10} "
                     f"{r['subscription_name']}/{r['name']}")
        lines.append(f"{'':17} -> {r.get('sidecar_reason','')}")
    return "\n".join(lines)


# -------------------------------------------------------------------------------- main

# =========================================================================== Phase 2
# Write path for sitecontainers-mode apps: connectioninfo -> DT_* settings -> sidecar
# PUT -> wait -> LD_PRELOAD + restart -> verify. Ordered so a kill between any two steps
# leaves the app recoverable by re-run. Conversion of legacy DOCKER| apps and slot
# fan-out remain Phase 3 (gated).

AI_KEYS = ["APPLICATIONINSIGHTS_CONNECTION_STRING", "APPINSIGHTS_INSTRUMENTATIONKEY",
           "ApplicationInsightsAgent_EXTENSION_VERSION",
           "XDT_MicrosoftApplicationInsights_Mode", "XDT_MicrosoftApplicationInsights_Java",
           "XDT_MicrosoftApplicationInsights_NodeJS"]

# settings this path writes; removed on uninstall
DT_SETTING_KEYS = ["DT_CONNECTION_POINT", "DT_TENANT", "DT_TENANTTOKEN", "DT_NETWORK_ZONE",
                   "DT_TAGS", "DT_LOGSTREAM", "DT_LOGLEVELCON", "DT_FLEET_SIDECAR_VERSION",
                   "DT_FLEET_PATH"]


def resolve_oneagent_version(explicit=None):
    """Full image tag. Refuse 'latest'. Default: highest concrete tag from Docker Hub."""
    if explicit == "latest":
        raise SystemExit("refusing 'latest' — pass a concrete --oneagent-version or omit to auto-resolve")
    if explicit:
        return explicit
    url = ("https://hub.docker.com/v2/repositories/dynatrace/dynatrace-codemodules/"
           "tags?page_size=100")
    pat = re.compile(r"^\d+\.\d+\.\d+\.\d{8}-\d{6}$")
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"could not resolve latest codemodules tag ({e}); pass --oneagent-version")
    tags = [t["name"] for t in data.get("results", []) if pat.match(t.get("name", ""))]
    if not tags:
        raise SystemExit("no concrete codemodules version tags found; pass --oneagent-version")

    def tk(t):
        head, tail = t.rsplit(".", 1)
        a, b, c = head.split(".")
        d, e = tail.split("-")
        return (int(a), int(b), int(c), int(d), int(e))
    return max(tags, key=tk)


def _sidecar_image(version, registry):
    return f"{registry or SIDECAR_IMAGE_BASE}:{version}"


def _sidecar_startup(tech):
    cmd = "serverless --keep-alive --target /home/dynatrace/"
    if tech and tech != "all":
        cmd += f" --technology={tech}"
    return cmd


def _effective_tech(app, args):
    if args.technology:
        return args.technology
    hint = app.get("sidecar", {}).get("technology_hint")
    return hint if hint and hint != "python" else "all"


def _registry_auth_props(args):
    """Map --registry-auth to sitecontainer authType + identity fields."""
    ra = (args.registry_auth or "anonymous")
    if ra == "anonymous":
        return {"authType": "Anonymous"}
    if ra == "system-identity":
        return {"authType": "SystemIdentity"}
    if ra.startswith("user-assigned:"):
        return {"authType": "UserAssigned", "userManagedIdentityClientId": ra.split(":", 1)[1]}
    raise SystemExit(f"--registry-auth must be anonymous | system-identity | user-assigned:<clientId> (got {ra})")


def _dt_settings(app, conn, args, version):
    name = app["name"]
    tags = [f"azure.app_service_name={name}", "managed-by=dt-fleet",
            f"env={args.env_tag}", "dt-path=sidecar"]
    for t in (args.set_host_tag or []):
        tags.append(t.format(name=name, rg=app["resource_group"], sub=app["subscription_id"]))
    s = {
        "DT_CONNECTION_POINT": conn["connection_point"],
        "DT_TENANT": conn["tenant"],
        "DT_TENANTTOKEN": conn["tenant_token"],
        "DT_TAGS": " ".join(tags),
        "DT_LOGSTREAM": "stdout",
        "DT_LOGLEVELCON": "INFO",
        "DT_FLEET_SIDECAR_VERSION": version,
        "DT_FLEET_PATH": "sidecar",
    }
    if conn.get("network_zone"):
        s["DT_NETWORK_ZONE"] = conn["network_zone"]
    return s


def _gate(app, args):
    """Return (blocked_status, detail) if the app should not proceed, else (None, None)."""
    status = app.get("sidecar_status")
    reason = app.get("sidecar_reason", "")
    if status in ("unsupported-runtime", "unsupported-compose", "unsupported-sku", "not-container"):
        return "skipped", reason
    if status == "needs-input":
        return "needs-input", reason
    if status == "instrumented" and not args.force:
        return "skipped", "already has a Dynatrace sidecar (use --force to re-apply)"
    if status == "needs-conversion":
        return "needs-conversion", "legacy DOCKER| container — conversion to sitecontainers is Phase 3 (--allow-convert)"
    if status == "needs-storage" and not args.enable_storage:
        return "needs-storage", "WEBSITES_ENABLE_APP_SERVICE_STORAGE is not true — pass --enable-storage"
    if status == "needs-review" and not args.allow_alpine:
        return "needs-review", "Alpine/musl image — sidecar musl support unverified; pass --allow-alpine to proceed"
    return None, None


def _wait_sidecar(app, args):
    """Wait for the bootstrapper to finish. v1: fixed wait (SCM/Kudu log polling is a
    follow-on); verify() is the real confirmation. Returns None (unconfirmed)."""
    if args.parallel > 1:
        time.sleep(args.sidecar_wait)
    else:
        core.sleep_with_heartbeat(args.sidecar_wait, f"[{app['name']}] sidecar bootstrap")
    return None


def sidecar_deploy_one(app, creds, conn, args):
    name, rg = app["name"], app["resource_group"]
    r = app.get("sidecar", {})
    result = {"name": name, "rg": rg, "status": "", "detail": ""}

    blocked, detail = _gate(app, args)
    if blocked:
        result["status"], result["detail"] = blocked, detail
        return result

    version = args._resolved_version
    tech = _effective_tech(app, args)
    image = _sidecar_image(version, args.image_registry)
    startup = _sidecar_startup(tech)
    status = app.get("sidecar_status")

    if args.what_if:
        acts = []
        if status == "needs-storage":
            acts.append("set WEBSITES_ENABLE_APP_SERVICE_STORAGE=true")
        if args.disable_app_insights and r.get("app_insights"):
            acts.append("remove App Insights settings")
        acts.append(f"write DT_* (tenant {conn['tenant']})")
        acts.append(f"PUT sidecar '{args.sidecar_name}' <- {image}  startup='{startup}'")
        acts.append("set LD_PRELOAD + restart")
        result["status"] = "would-deploy"
        result["detail"] = f"tech={tech} | " + " | ".join(acts)
        return result

    try:
        extra = {}
        if status == "needs-storage" and args.enable_storage:
            orig = r.get("app_service_storage")
            extra["DT_FLEET_ORIG_STORAGE"] = "" if orig == "absent" else orig
            extra["WEBSITES_ENABLE_APP_SERVICE_STORAGE"] = "true"
        if args.disable_app_insights and r.get("app_insights"):
            stash = {k: app["app_settings"][k] for k in AI_KEYS if k in app.get("app_settings", {})}
            if stash:
                extra["DT_FLEET_ORIG_APPINSIGHTS"] = json.dumps(stash)
                log(f"    [{name}] removing {len(stash)} App Insights setting(s)...")
                core.arm_delete_appsettings(app, list(stash.keys()))

        settings = _dt_settings(app, conn, args, version)
        settings.update(extra)
        log(f"    [{name}] 1/5 writing {len(settings)} DT_* app setting(s) (no LD_PRELOAD yet)...")
        core.arm_set_appsettings(app, settings)

        log(f"    [{name}] 2/5 PUT sidecar '{args.sidecar_name}' <- {image}...")
        props = {"image": image, "isMain": False, "startUpCommand": startup}
        props.update(_registry_auth_props(args))
        if _sc_call("PUT", app, f"/sitecontainers/{args.sidecar_name}",
                    {"properties": props}) is None:
            raise RuntimeError("sitecontainers PUT failed")

        log(f"    [{name}] 3/6 waiting for bootstrapper to lay down the agent (up to {args.sidecar_wait}s)...")
        _wait_sidecar(app, args)

        log(f"    [{name}] 4/6 set LD_PRELOAD...")
        core.arm_set_appsettings(app, {"LD_PRELOAD": DT_LIB_PATH})
        # Two restarts (per the Dynatrace doc). The agent is written to the SMB-mounted
        # /home by the bootstrapper, which can lag; restart #1 may race it. Restart #2,
        # after a settle window, guarantees the app process preloads the now-present .so.
        log(f"    [{name}] 5/6 restart #1 + settle {args.restart_wait}s...")
        core.arm_restart(app)
        if args.parallel > 1:
            time.sleep(args.restart_wait)
        else:
            core.sleep_with_heartbeat(args.restart_wait, f"[{name}] restart #1")
        log(f"    [{name}] 6/6 restart #2 (instrument)...")
        core.arm_restart(app)

        result["status"] = "deployed"
        result["detail"] = f"sidecar {version} tech={tech}"
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["detail"] = redact(str(e))
    return result


def sidecar_verify_one(app, creds, args):
    """ARM-side (sidecar present + LD_PRELOAD + tenant) AND Dynatrace-side (process group
    by azure.app_service_name tag) + HTTP health."""
    name = app["name"]
    res = {"name": name, "verdict": "", "detail": ""}
    # re-read current state
    sidecar_readiness(app, args.sidecar_name)
    r = app["sidecar"]
    settings = app.get("app_settings", {})
    az_ok = r.get("dt_instrumented") and settings.get("LD_PRELOAD") == DT_LIB_PATH
    healthy = core.app_http_ok(app)
    az_part = ("sidecar present + LD_PRELOAD set" if az_ok else
               "sidecar/LD_PRELOAD NOT fully set")
    # Dynatrace side
    dt_state, dt_detail = "skipped", "no dtApiToken/dtctl"
    if not args.no_dt_check and creds:
        dt_state, dt_detail = core.dt_confirm(app, creds, args.dt_timeout, not args.no_traffic)
    if dt_state == "present":
        res["verdict"] = "CONFIRMED (Azure + Dynatrace)" if az_ok else "CONFIRMED (Dynatrace)"
    elif az_ok and healthy:
        res["verdict"] = "INSTRUMENTED (sidecar up)"
    elif az_ok and not healthy:
        res["verdict"] = "sidecar set but app NOT serving HTTP"
    else:
        res["verdict"] = "NOT instrumented"
    res["detail"] = f"{az_part}; app {'up' if healthy else 'DOWN'} | Dynatrace: {dt_detail}"
    return res


def sidecar_uninstall_one(app, args):
    name, rg = app["name"], app["resource_group"]
    result = {"name": name, "rg": rg, "status": "", "detail": ""}
    sidecar_readiness(app, args.sidecar_name)
    settings = app.get("app_settings", {})
    if args.what_if:
        result["status"] = "would-uninstall"
        result["detail"] = f"delete sidecar '{args.sidecar_name}', remove DT_* + LD_PRELOAD"
        return result
    try:
        # 1. delete the sidecar container
        log(f"    [{name}] 1/4 deleting sidecar '{args.sidecar_name}'...")
        _sc_call("DELETE", app, f"/sitecontainers/{args.sidecar_name}")
        # 2. settings to remove: DT_* + LD_PRELOAD (only if it's ours)
        remove = list(DT_SETTING_KEYS)
        if settings.get("LD_PRELOAD") == DT_LIB_PATH:
            remove.append("LD_PRELOAD")
        # 3. restore stashed originals
        restore = {}
        if "DT_FLEET_ORIG_STORAGE" in settings:
            if settings["DT_FLEET_ORIG_STORAGE"]:
                restore["WEBSITES_ENABLE_APP_SERVICE_STORAGE"] = settings["DT_FLEET_ORIG_STORAGE"]
            else:
                remove.append("WEBSITES_ENABLE_APP_SERVICE_STORAGE")
            remove.append("DT_FLEET_ORIG_STORAGE")
        if "DT_FLEET_ORIG_APPINSIGHTS" in settings:
            try:
                restore.update(json.loads(settings["DT_FLEET_ORIG_APPINSIGHTS"]))
            except Exception:  # noqa: BLE001
                pass
            remove.append("DT_FLEET_ORIG_APPINSIGHTS")
        log(f"    [{name}] 2/4 removing {len(remove)} setting(s)"
            + (f", restoring {len(restore)}" if restore else "") + "...")
        core.arm_delete_appsettings(app, remove)
        if restore:
            core.arm_set_appsettings(app, restore)
        log(f"    [{name}] 3/4 restart...")
        core.arm_restart(app)
        log(f"    [{name}] 4/4 done (note: /home/dynatrace agent files remain on shared storage)")
        result["status"] = "uninstalled"
        result["detail"] = f"removed sidecar + {len(remove)} setting(s)"
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["detail"] = redact(str(e))
    return result


def build_parser():
    p = argparse.ArgumentParser(
        prog="dt_appservice_sidecar.py",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", nargs="?", default="assess",
                   choices=["assess", "deploy", "verify", "uninstall", "update"],
                   help="assess (container sidecar-readiness, DEFAULT) | deploy/verify/uninstall/update (Phase 2/3)")
    # scope (reused from the core tool's semantics)
    p.add_argument("--subscription", "--subscription-filter", dest="subscription",
                   action="append", metavar="GUID|NAME",
                   help="subscription GUID or name (substring/glob), repeatable")
    p.add_argument("--all-subscriptions", action="store_true")
    p.add_argument("--resource-group")
    p.add_argument("--app-service-name", "--name-filter", dest="app_service_name",
                   action="append", metavar="NAME", help="app name (substring/glob), repeatable")
    # output / client
    p.add_argument("--output-file")
    p.add_argument("--format", choices=["txt", "md", "csv", "json"])
    p.add_argument("--detailed", action="store_true", help="(reserved) deeper per-app probing")
    p.add_argument("--use-arm", action="store_true", help="inventory via ARM instead of Resource Graph")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--rest", action="store_true", help="ARM REST instead of az CLI (recommended)")
    p.add_argument("--no-token-cache", action="store_true")
    p.add_argument("--auth", choices=["auto", "az", "managed-identity", "service-principal"], default="auto")
    p.add_argument("--creds", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "creds.json"))
    # sidecar knobs (most consumed in Phase 2; parsed now so the CLI is stable)
    p.add_argument("--sidecar-name", default=DEFAULT_SIDECAR_NAME)
    p.add_argument("--technology", help="override the Dynatrace --technology for the bootstrapper")
    p.add_argument("--technology-map", help="file of 'name-glob: tech' lines overriding the hint")
    p.add_argument("--network-zone", help="Dynatrace network zone (connectioninfo + DT_NETWORK_ZONE)")
    p.add_argument("--oneagent-version", help="full codemodules image tag (default: resolve latest from Docker Hub; 'latest' refused)")
    p.add_argument("--image-registry", help="override index.docker.io/dynatrace/dynatrace-codemodules with a mirror (e.g. your ACR path)")
    p.add_argument("--registry-auth", default="anonymous",
                   help="anonymous | system-identity | user-assigned:<clientId> for the sidecar image pull")
    p.add_argument("--sidecar-wait", type=int, default=180, help="seconds to wait for the bootstrapper before LD_PRELOAD+restart")
    p.add_argument("--no-sidecar-wait", action="store_true", help="skip the bootstrap wait and rely on verify")
    p.add_argument("--restart-wait", type=int, default=45, help="seconds to wait after the instrument restart")
    # approved config changes (behavioral — off by default)
    p.add_argument("--enable-storage", action="store_true", help="permit flipping WEBSITES_ENABLE_APP_SERVICE_STORAGE=true")
    p.add_argument("--allow-alpine", action="store_true", help="proceed on Alpine/musl images (musl support unverified)")
    p.add_argument("--disable-app-insights", action="store_true", help="remove conflicting App Insights settings (stashed for uninstall)")
    p.add_argument("--allow-convert", action="store_true", help="(Phase 3) permit converting legacy DOCKER| apps to sitecontainers")
    p.add_argument("--include-slots", action="store_true", help="(Phase 3) also apply to deployment slots")
    # deploy/verify controls
    p.add_argument("--force", action="store_true", help="re-apply to apps already carrying a Dynatrace sidecar")
    p.add_argument("--verify", action="store_true", help="deploy: confirm injection inline after deploying")
    p.add_argument("--rollback-on-fail", action="store_true", help="deploy --verify: uninstall any app not serving HTTP after deploy")
    p.add_argument("--dt-timeout", type=int, default=180, help="verify: seconds to poll Dynatrace (driving traffic)")
    p.add_argument("--no-dt-check", action="store_true", help="verify: skip the Dynatrace-side entity check")
    p.add_argument("--no-traffic", action="store_true", help="verify: do not generate traffic while polling")
    p.add_argument("--env-tag", default="demo", help="value for DT_TAGS env=")
    p.add_argument("--set-host-tag", action="append", metavar="KEY=VALUE", help="extra DT_TAGS entry (repeatable)")
    p.add_argument("--state-file", help="JSONL checkpoint for resumable rollouts")
    p.add_argument("--ntfy-topic", help="ntfy.sh topic for progress notifications")
    p.add_argument("--parallel", type=int, default=1, metavar="N")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--what-if", action="store_true")
    return p


def _load_tech_map(path):
    if not path:
        return None
    m = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            m[k.strip()] = v.strip()
    return m


def main():
    args = build_parser().parse_args()
    core.VERBOSE = args.verbose
    core.USE_REST = args.rest
    core.NO_TOKEN_CACHE = args.no_token_cache
    core.AUTH_PREF = args.auth

    if args.action == "update":
        _phase2("update")  # Phase 3

    if args.technology and args.technology not in DT_TECHNOLOGIES:
        raise SystemExit(f"--technology must be one of: {', '.join(sorted(DT_TECHNOLOGIES))}")
    tech_map = _load_tech_map(args.technology_map)
    mutate = args.action in ("deploy", "uninstall")

    fmt = args.format
    if not fmt and args.output_file:
        ext = os.path.splitext(args.output_file)[1].lower().lstrip(".")
        fmt = {"md": "md", "csv": "csv", "json": "json"}.get(ext, "txt")
    fmt = fmt or "txt"

    # creds + connection info only for the write/verify paths
    creds = conn = None
    if args.action in ("deploy", "verify"):
        creds = core.load_creds(args.creds, require_paas=(args.action == "deploy"))
    if args.action == "deploy":
        conn = dt_connection_info(creds, args.network_zone)
        args._resolved_version = resolve_oneagent_version(args.oneagent_version)
        log(f"[sidecar] OneAgent codemodules tag: {args._resolved_version}")

    subs = core.resolve_subscriptions(args)
    records = core.build_app_records(args, subs)
    containers = [r for r in records if _is_container_candidate(r)]
    log(f"[sidecar] {len(records)} app(s) in scope; {len(containers)} container Linux Web App(s)")
    if not containers:
        log("[sidecar] no container apps matched — nothing to do.")
        return

    workers = max(1, args.parallel) if core.USE_REST else 1
    log(f"[sidecar] probing readiness for {len(containers)} app(s)"
        + (f" across {workers} threads..." if workers > 1 else "..."))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda a: sidecar_readiness(a, args.sidecar_name, tech_map), containers))

    # ---- assess: report and stop
    if args.action == "assess":
        report = render_sidecar_report(containers, fmt)
        if args.output_file:
            with open(args.output_file, "w") as f:
                f.write(report + "\n")
            log(f"[sidecar] wrote {args.output_file}")
        else:
            print("\n" + report)
        return

    # ---- verify: confirm existing instrumentation
    if args.action == "verify":
        targets = [a for a in containers if a.get("dt_instrumented")] or containers
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, workers if workers > 1 else 8)) as ex:
            for v in ex.map(lambda a: sidecar_verify_one(a, creds, args), targets):
                log(f"    {v['verdict']:34}  {v['name']}: {v['detail']}")
        return

    # ---- deploy / uninstall
    state = core.load_state(args.state_file) if args.state_file else {}
    done_statuses = {"deployed", "uninstalled"}
    actionable = [a for a in containers if state.get(a["id"]) not in done_statuses]
    skipped_state = len(containers) - len(actionable)
    if skipped_state:
        log(f"[sidecar] {skipped_state} app(s) already completed per state file — skipped")

    # Preview what each app will do (gating reasons) before confirming.
    log(f"[sidecar] {args.action} targets ({len(actionable)}):")
    for a in actionable:
        log(f"    - {a['subscription_name']}/{a['name']}  [{a['sidecar_status']}]  {a.get('sidecar_reason','')}")

    if mutate and not args.yes and not args.what_if:
        if input(f"\nProceed to {args.action} {len(actionable)} app(s)? [y/N] ").strip().lower() != "y":
            raise SystemExit("aborted")

    def run_one(a):
        if args.action == "deploy":
            res = sidecar_deploy_one(a, creds, conn, args)
        else:
            res = sidecar_uninstall_one(a, args)
        if args.state_file:
            core.append_state(args.state_file, a, res)
        return a, res

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for a, res in ex.map(run_one, actionable):
            log(f"[sidecar] {res['name']} -> {res['status']}: {res['detail']}")
            results.append((a, res))

    ok = sum(1 for _, x in results if x["status"] in ("deployed", "uninstalled", "would-deploy", "would-uninstall"))
    log(f"\n[sidecar] {args.action} done: {ok}/{len(results)} ok")
    for _, x in results:
        if x["status"] in ("error", "needs-input", "needs-conversion", "needs-storage", "needs-review"):
            log(f"    !! {x['name']}: {x['status']} — {x['detail']}", err=True)

    # inline verify (+ optional rollback) after a real deploy
    if args.action == "deploy" and args.verify and not args.what_if:
        deployed = [a for a, x in results if x["status"] == "deployed"]
        if deployed:
            log(f"\n[verify] confirming {len(deployed)} app(s) (ARM + Dynatrace + HTTP)...")
            verdicts = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(deployed))) as ex:
                for a, v in zip(deployed, ex.map(lambda a: sidecar_verify_one(a, creds, args), deployed)):
                    log(f"    {v['verdict']:34}  {v['name']}: {v['detail']}")
                    verdicts[a["name"]] = (a, v)
            if args.rollback_on_fail:
                log(f"\n[rollback] probing HTTP health of {len(verdicts)} app(s) (independent of agent verdict)...")
                rolled = 0
                for nm, (a, v) in verdicts.items():
                    if core.app_http_ok(a):
                        continue
                    log(f"    [{nm}] NOT serving HTTP (verdict '{v['verdict']}') — rolling back...", err=True)
                    rb = sidecar_uninstall_one(a, args)
                    rolled += 1
                    log(f"    [{nm}] rollback -> {rb['status']}: {rb['detail']}", err=True)
                log(f"[rollback] {rolled}/{len(verdicts)} restored (were not serving HTTP); rest healthy.")

    if args.ntfy_topic:
        core.notify(args.ntfy_topic, f"DT Fleet sidecar — {args.action} complete",
                    f"{ok}/{len(results)} ok.", tags="white_check_mark")


if __name__ == "__main__":
    main()
