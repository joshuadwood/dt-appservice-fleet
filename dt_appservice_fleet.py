#!/usr/bin/env python3
"""
dt_appservice_fleet.py — Dynatrace OneAgent fleet tool for Azure Linux App Services.

Two primary modes:

  assess  (a.k.a. "dry run", default)
      Non-mutating. Enumerates App Services across one or more subscriptions
      (optionally filtered), classifies each by runtime and by deployment model
      (code/"core" vs. standalone container via ACR/JFrog/other), lists the
      startup command, and reports whether Dynatrace instrumentation is already
      present. Emits a distribution table + per-app detail to the console or to a
      flat file (.txt/.md/.csv/.json chosen by extension).

  deploy
      Mutating. Instruments targeted *Linux code* Web Apps with the Dynatrace
      OneAgent using the built-in-image / startup-command path:
        * swaps the Startup Command to a self-contained installer wrapper that
          downloads the PaaS OneAgent, installs it, and execs the real app with
          LD_PRELOAD set INLINE (not as a container-wide app setting);
        * preserves the previous startup command as START_APP_CMD;
        * sets DT_ENDPOINT / DT_API_TOKEN / DT_INCLUDE / DT_FLAVOR and useful
          naming + logging app settings;
        * restarts twice (install, then instrument);
        * verifies injection.

  uninstall
      Reverts deploy: restores the original Startup Command and removes DT_* /
      START_APP_CMD settings.

Design notes / field gotchas honored (see dynatrace-appservice-linux-gotchas.md):
  * Function Apps (kind contains "functionapp") are NEVER deployed to — the
    Web-App startup/LD_PRELOAD path is inert there. They are reported and skipped.
  * Standalone-container apps (kind contains "container" / linuxFxVersion starts
    "DOCKER|") use the custom/sidecar path, not the built-in path — reported and
    skipped by deploy.
  * LD_PRELOAD is set INLINE by the startup command for the app process only,
    never as a container-wide app setting (which would error on oryx/wget/sh
    before /opt/dynatrace exists).
  * START_APP_CMD must be explicitly populated — an app that relied on the image's
    implicit default start will NOT start once a Startup Command is present.
  * The installer wrapper fails loud: if download/install fails the app does not
    start, rather than silently running un-instrumented.

Auth: relies on an already-authenticated `az` CLI. Dynatrace creds are read from
a JSON creds file (default ./creds.json) — dtApiEnvUrl + dtPaasToken.
"""

import argparse
import concurrent.futures
import csv
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ----------------------------------------------------------------------------- helpers

# Verbosity: 0 = normal (timestamped progress), 1 = verbose (-v: echo every az
# call + timing + applied values). Set from args in main().
VERBOSE = False

# Mask tokens/secrets anywhere they might be echoed (verbose az lines, errors).
_SECRET_RE = re.compile(
    r"(dt0[a-z]\d{2}\.[A-Z0-9]+\.[A-Z0-9]+)"           # dt0c01.../dt0s16... tokens
    r"|((?:Api-Token=)\S+)"                              # installer URL token
    r"|((?:DT_API_TOKEN|DT_TENANTTOKEN)=\S+)")          # token app settings


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def redact(s):
    return _SECRET_RE.sub("<redacted>", str(s))


def log(msg, *, err=False):
    stream = sys.stderr if err else sys.stdout
    print(f"{_ts()} {msg}", file=stream, flush=True)


def vlog(msg):
    """Verbose-only detail (enabled with -v/--verbose)."""
    if VERBOSE:
        print(f"{_ts()}   · {redact(msg)}", flush=True)


def sleep_with_heartbeat(seconds, label, interval=15):
    """Sleep, emitting a countdown line every `interval`s so it never looks frozen."""
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        rem = int(round(end - time.time()))
        if rem > 0:
            log(f"        {label} ... {rem}s remaining")


def run_az(args, *, check=True, timeout=180):
    """Run an `az` command, returning parsed JSON (or raw text if not -o json).

    In verbose mode echoes the (redacted) command + elapsed time. In normal mode,
    any call slower than 20s is surfaced so a slow ARM/Graph request never looks
    like a hang.
    """
    cmd = ["az"] + args
    label = redact(" ".join(args[:6]))
    if VERBOSE:
        vlog(f"az {label} ...")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if VERBOSE:
            vlog(f"az {label}  TIMED OUT after {timeout}s")
        if check:
            raise RuntimeError(f"az timed out: {redact(' '.join(args[:4]))}...")
        return None
    dt = time.time() - t0
    if VERBOSE:
        vlog(f"az {label}  ({dt:.1f}s, rc={proc.returncode})")
    elif dt > 20:
        log(f"    (slow: az {' '.join(args[:2])} took {dt:.0f}s)")
    if proc.returncode != 0:
        if check:
            raise RuntimeError(f"az failed ({proc.returncode}): {redact(' '.join(cmd))}\n{redact(proc.stderr.strip())}")
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


# ----------------------------------------------------------------------------- ARM REST fast path
#
# The `az` CLI pays a large per-invocation cost: each call is a fresh Python
# process that imports heavy command modules (the `appservice` module alone can
# take 60-200s on a resource-starved host — observed here). The *API* work is ~3s;
# the rest is module load. ARM REST bypasses that entirely: mint ONE bearer token
# via az, then hit management.azure.com directly over HTTPS (dependency-free urllib).
# Measured here: config read 4.5s vs az 113s; appsettings read 3.6s vs az 122s.
# Enabled with --rest.

USE_REST = False
NO_TOKEN_CACHE = False
AUTH_PREF = "auto"          # auto | az | managed-identity | service-principal (--auth)
AUTH_MODE = None            # resolved source once a token is minted
ARM = "https://management.azure.com"
ARM_RESOURCE = "https://management.azure.com/"
ARM_API = "2023-12-01"
# Delegated (az) tokens are keyed by subscription so az resolves the right tenant
# (incl. guest/Lighthouse). A managed identity / service principal is a SINGLE
# identity: one token for every subscription it can reach, cached under "default".
_arm_tokens = {}
_arm_token_lock = threading.Lock()
_TOKEN_DIR = tempfile.gettempdir()
_TOKEN_UID = getattr(os, "getuid", lambda: "u")()


def _token_cache_file(key):
    k = re.sub(r"[^A-Za-z0-9._-]", "_", key or "default")
    return os.path.join(_TOKEN_DIR, f"dt-fleet-arm-token-{_TOKEN_UID}-{k}.json")


def _token_via_sp():
    """Client-credentials token from AZURE_CLIENT_ID/SECRET/TENANT_ID env vars (fast, no az)."""
    cid, sec, tid = (os.environ.get("AZURE_CLIENT_ID"),
                     os.environ.get("AZURE_CLIENT_SECRET"),
                     os.environ.get("AZURE_TENANT_ID"))
    if not (cid and sec and tid):
        return None
    body = urllib.parse.urlencode({
        "client_id": cid, "client_secret": sec,
        "scope": ARM_RESOURCE + ".default", "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read())
    return d["access_token"], time.time() + int(d.get("expires_in", 3000))


def _token_via_imds():
    """Managed-identity token from the Azure IMDS endpoint (fast; only present on Azure)."""
    url = ("http://169.254.169.254/metadata/identity/oauth2/token"
           f"?api-version=2018-02-01&resource={urllib.parse.quote(ARM_RESOURCE, safe='')}")
    cid = os.environ.get("AZURE_CLIENT_ID")
    if cid:
        url += f"&client_id={cid}"
    req = urllib.request.Request(url, headers={"Metadata": "true"})
    with urllib.request.urlopen(req, timeout=3) as resp:   # short: local or absent
        d = json.loads(resp.read())
    return d["access_token"], float(d.get("expires_on") or (time.time() + 3000))


def _load_disk_token(key):
    try:
        with open(_token_cache_file(key)) as f:
            d = json.load(f)
        if d.get("exp", 0) - 120 > time.time():
            return d["token"], d["exp"]
    except Exception:  # noqa: BLE001
        pass
    return None


def _save_disk_token(key, token, exp):
    try:
        fd = os.open(_token_cache_file(key), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"token": token, "exp": exp}, f)
    except Exception:  # noqa: BLE001
        pass


def invalidate_token(subscription=None):
    key = subscription or "default"
    _arm_tokens.pop(key, None)
    try:
        os.remove(_token_cache_file(key))
    except OSError:
        pass


def auth_is_single_identity():
    """True once the resolved token source is a managed identity or service
    principal — a single identity whose token cannot be swapped per tenant."""
    return AUTH_MODE in ("managed-identity", "service-principal")


def arm_token(subscription=None):
    """Acquire an ARM bearer token, respecting --auth. Thread-safe.

    - Managed identity / service principal = ONE identity: a single token is used
      for every subscription it can reach (cached under "default"). It CANNOT be
      re-minted per tenant, so subscriptions outside its reach simply fail.
    - Delegated az = tokens keyed by subscription (`az account get-access-token
      --subscription <id>` resolves the right tenant, incl. guest/Lighthouse).
    """
    global AUTH_MODE
    with _arm_token_lock:
        now = time.time()
        single = auth_is_single_identity()
        key = "default" if single else (subscription or "default")
        cur = _arm_tokens.get(key)
        if cur and cur["exp"] - 120 > now:
            return cur["token"]
        if not NO_TOKEN_CACHE:
            disk = _load_disk_token(key)
            if disk:
                _arm_tokens[key] = {"token": disk[0], "exp": disk[1]}
                vlog(f"ARM token[{key}]: disk cache ({int((disk[1]-now)/60)}m left)")
                return disk[0]
        # SP / managed identity (single identity). Respect --auth.
        sources = []
        if AUTH_PREF in ("auto", "service-principal"):
            sources.append(("service-principal", _token_via_sp))
        if AUTH_PREF in ("auto", "managed-identity"):
            sources.append(("managed-identity", _token_via_imds))
        for label, fn in sources:
            try:
                got = fn()
            except Exception:  # noqa: BLE001
                got = None
            if got:
                if AUTH_MODE != label:
                    log(f"[auth] ARM token via {label} — single identity "
                        f"(only its accessible subscriptions are reachable)")
                AUTH_MODE = label
                _arm_tokens["default"] = {"token": got[0], "exp": got[1]}
                if not NO_TOKEN_CACHE:
                    _save_disk_token("default", *got)
                return got[0]
        # Delegated az (per-subscription so az resolves the right tenant).
        if AUTH_PREF not in ("auto", "az"):
            raise RuntimeError(f"--auth {AUTH_PREF}: no token available from that source")
        AUTH_MODE = "az"
        az_args = ["account", "get-access-token", "-o", "json"]
        if subscription:
            az_args += ["--subscription", subscription]
        log(f"[auth] ARM token[{subscription or 'default'}] via az CLI"
            + (" (set AZURE_CLIENT_ID/SECRET/TENANT_ID or use a managed identity to skip)"
               if not subscription else ""))
        data = run_az(az_args, check=True, timeout=180)
        tok = data["accessToken"]
        try:
            exp = float(data.get("expires_on") or 0) or (now + 3000)
        except (TypeError, ValueError):
            exp = now + 3000
        _arm_tokens[subscription or "default"] = {"token": tok, "exp": exp}
        if not NO_TOKEN_CACHE:
            _save_disk_token(subscription or "default", tok, exp)
        return tok


def _arm_call(method, url, body=None, timeout=60, subscription=None, _retry=True):
    headers = {"Authorization": f"Bearer {arm_token(subscription)}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    elif method == "POST":
        headers["Content-Length"] = "0"   # ARM *list* endpoints are POST-empty-body
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if VERBOSE:
            vlog(f"ARM {method} {url.split('?')[0].split('/providers/')[-1][:60]} ({time.time()-t0:.1f}s)")
        # Empty 200 (e.g. restart) -> {} (success); errors below -> None.
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read()[:400].decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
        # Wrong-tenant / wrong-issuer 401 is NOT retryable: re-minting yields the
        # same identity token. This happens when the token's identity can't access
        # the subscription's tenant (common with a single managed identity / SP).
        non_retryable = ("InvalidAuthenticationTokenTenant" in body_txt
                         or "wrong issuer" in body_txt.lower())
        if e.code == 401 and _retry and not non_retryable:
            if VERBOSE:
                vlog(f"ARM 401 — invalidating token[{subscription or 'default'}] and retrying")
            invalidate_token(subscription)
            return _arm_call(method, url, body, timeout, subscription, _retry=False)
        if e.code in (401, 403) and non_retryable and subscription:
            log(f"    (skip subscription {subscription}: the {AUTH_MODE or 'current'} "
                f"identity cannot access it — wrong tenant)", err=True)
        if VERBOSE:
            vlog(f"ARM {method} -> HTTP {e.code} ({time.time()-t0:.1f}s) {redact(body_txt)}")
        return None
    except Exception as e:  # noqa: BLE001
        if VERBOSE:
            vlog(f"ARM {method} failed ({time.time()-t0:.1f}s): {redact(str(e))}")
        return None


def arm_resource(method, resource_id, sub_path="", body=None, timeout=60, subscription=None):
    return _arm_call(method, f"{ARM}{resource_id}{sub_path}?api-version={ARM_API}",
                     body, timeout, subscription)


def arm_graph(kql, subscription_ids, subscription=None):
    url = f"{ARM}/providers/Microsoft.ResourceGraph/resources?api-version=2021-03-01"
    rows, skip = [], 0
    while True:
        body = {"subscriptions": subscription_ids, "query": kql,
                "options": {"resultFormat": "objectArray", "$top": 1000, "$skip": skip}}
        out = _arm_call("POST", url, body, subscription=subscription)
        batch = (out or {}).get("data", []) if isinstance(out, dict) else []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        skip += len(batch)
    return rows


def _rg_from_id(resource_id):
    m = re.search(r"/resourceGroups/([^/]+)/", resource_id or "", re.IGNORECASE)
    return m.group(1) if m else ""


def arm_webapp_list(subscription_ids, resource_group=None):
    """List web sites via ARM REST (fast; the --rest fallback for Graph indexing lag).
    Each subscription's call uses a token scoped to that subscription."""
    rows = []
    for sid in subscription_ids:
        if resource_group:
            url = (f"{ARM}/subscriptions/{sid}/resourceGroups/{resource_group}"
                   f"/providers/Microsoft.Web/sites?api-version={ARM_API}")
        else:
            url = f"{ARM}/subscriptions/{sid}/providers/Microsoft.Web/sites?api-version={ARM_API}"
        out = _arm_call("GET", url, subscription=sid)
        for v in (out or {}).get("value", []) if isinstance(out, dict) else []:
            props = v.get("properties", {}) or {}
            sc = props.get("siteConfig", {}) or {}
            rows.append({
                "name": v.get("name", ""), "kind": v.get("kind", ""),
                "resourceGroup": props.get("resourceGroup") or _rg_from_id(v.get("id", "")),
                "subscriptionId": sid, "location": v.get("location", ""),
                "state": props.get("state", ""),
                "linuxFx": sc.get("linuxFxVersion"), "windowsFx": sc.get("windowsFxVersion"),
            })
    return rows


# ---- ARM REST write helpers (deploy/uninstall fast path) --------------------
# PUT config/appsettings REPLACES the whole set, so we read-merge-write to preserve
# the app's own settings. config/web is PATCHed (merge) for just appCommandLine.
# Each raises on failure so deploy_one/uninstall_one's try/except reports an error.

def arm_set_appsettings(app, new_settings):
    s = app.get("subscription_id")
    cur = arm_resource("POST", app["id"], "/config/appsettings/list", subscription=s)
    existing = dict(cur.get("properties", {})) if isinstance(cur, dict) else {}
    existing.update(new_settings)
    if arm_resource("PUT", app["id"], "/config/appsettings",
                    body={"properties": existing}, subscription=s) is None:
        raise RuntimeError("ARM appsettings PUT failed")


def arm_delete_appsettings(app, keys):
    s = app.get("subscription_id")
    cur = arm_resource("POST", app["id"], "/config/appsettings/list", subscription=s)
    existing = dict(cur.get("properties", {})) if isinstance(cur, dict) else {}
    for k in keys:
        existing.pop(k, None)
    if arm_resource("PUT", app["id"], "/config/appsettings",
                    body={"properties": existing}, subscription=s) is None:
        raise RuntimeError("ARM appsettings PUT (delete) failed")


def arm_set_startup(app, cmd):
    if arm_resource("PATCH", app["id"], "/config/web",
                    body={"properties": {"appCommandLine": cmd}},
                    subscription=app.get("subscription_id")) is None:
        raise RuntimeError("ARM config/web PATCH failed")


def arm_restart(app):
    if _arm_call("POST", f"{ARM}{app['id']}/restart?api-version={ARM_API}",
                 subscription=app.get("subscription_id")) is None:
        raise RuntimeError("ARM restart failed")


def _matches_any(patterns, value):
    """True if `value` matches any of `patterns` (case-insensitive).

    A pattern containing a glob char (``*``, ``?`` or ``[``) is matched as a
    shell-style wildcard (anchored to the whole value); a plain pattern is matched
    as a substring (backward-compatible). Examples::

        "dtfleet"        -> substring  (matches ...dtfleet...)
        "dtfleet-node-*" -> glob       (matches dtfleet-node-express-8ab598)
        "*-prod-*"       -> glob
        "web?-01"        -> glob
    """
    if not patterns:
        return False
    v = (value or "").lower()
    for p in patterns:
        pl = (p or "").lower()
        if any(c in pl for c in "*?["):
            if fnmatch.fnmatchcase(v, pl):
                return True
        elif pl in v:
            return True
    return False


def load_state(path):
    """Read a JSONL checkpoint -> {app_id: last_status}. Last line per id wins."""
    state = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("id"):
                    state[d["id"]] = d.get("status", "")
    except FileNotFoundError:
        pass
    return state


def append_state(path, app, res):
    """Append one app's outcome to the JSONL checkpoint (flushed per app)."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "id": app["id"], "name": app["name"], "rg": app["resource_group"],
        "subscription": app.get("subscription_name", ""),
        "status": res.get("status", ""), "detail": res.get("detail", ""),
    }
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
    except Exception as e:  # noqa: BLE001
        log(f"    (warning: could not write state file: {redact(str(e))})", err=True)


def load_creds(path, require_paas=False):
    with open(path) as fh:
        data = json.load(fh)
    c = data.get("credentials", data)
    env_url = c.get("dtApiEnvUrl", "").rstrip("/")
    paas = c.get("dtPaasToken")
    if not env_url:
        raise SystemExit(f"creds file {path} missing dtApiEnvUrl")
    if require_paas and not paas:
        raise SystemExit(f"creds file {path} missing dtPaasToken (needed for deploy)")
    return {
        "env_url": env_url,
        "paas_token": paas,
        "tenant": c.get("dtTenantId"),
        # optional read token for the Dynatrace-side entity check (entities.read)
        "api_token": c.get("dtApiToken") or c.get("dtReadToken"),
    }


# ----------------------------------------------------------------------------- classification

# Map Dynatrace DT_INCLUDE technology to the App Service runtime family.
RUNTIME_TO_DT_INCLUDE = {
    "NODE": "nodejs",
    "PYTHON": "python",
    "DOTNETCORE": "dotnet",
    "JAVA": "java",
    "TOMCAT": "java",
    "JBOSSEAP": "java",
    "PHP": "php",
    "GO": "go",
    "RUBY": "ruby",
}


def classify(site):
    """Classify a site dict (from Resource Graph) into normalized fields."""
    kind = (site.get("kind") or "").lower()
    linux_fx = site.get("linuxFx") or ""
    windows_fx = site.get("windowsFx") or ""

    is_function = "functionapp" in kind
    is_linux = "linux" in kind
    is_container = "container" in kind or linux_fx.upper().startswith("DOCKER|") \
        or bool(windows_fx)

    # Runtime family + version
    runtime_family, runtime_version, registry, registry_type = "", "", "", ""
    fx = linux_fx or windows_fx
    if fx.upper().startswith("DOCKER|"):
        image = fx.split("|", 1)[1]
        runtime_family = "CONTAINER"
        runtime_version = image
        # registry host is everything before the first '/'
        host = image.split("/", 1)[0] if "/" in image else ""
        registry = host
        hl = host.lower()
        if "azurecr.io" in hl:
            registry_type = "ACR"
        elif "jfrog" in hl or "artifactory" in hl:
            registry_type = "JFrog"
        elif hl.startswith("mcr.microsoft.com"):
            registry_type = "MCR"
        elif "ghcr.io" in hl:
            registry_type = "GHCR"
        elif host == "" or "docker.io" in hl or "index.docker" in hl:
            registry_type = "DockerHub"
        else:
            registry_type = "Other"
    elif fx and "|" in fx:
        runtime_family, runtime_version = fx.split("|", 1)
    elif fx:
        runtime_family = fx
    elif not is_linux and not is_function:
        # Windows code app with no linuxFx and no explicit stack string
        runtime_family = "WINDOWS"

    # Normalize family casing so e.g. Function Apps' "Python|3.11" and Web Apps'
    # "PYTHON|3.12" bucket together.
    runtime_family = runtime_family.upper()

    # Deployment model
    if is_function:
        deploy_model = "function"
    elif is_container:
        deploy_model = "container"
    else:
        deploy_model = "code"  # a.k.a. "core" / bring-your-own-code on built-in image

    dt_include = RUNTIME_TO_DT_INCLUDE.get(runtime_family.upper(), "")

    return {
        "kind": site.get("kind") or "",
        "os": "linux" if is_linux else ("windows" if not is_function or kind == "functionapp" else "linux"),
        "is_function": is_function,
        "is_container": is_container,
        "is_linux": is_linux,
        "deploy_model": deploy_model,
        "runtime_family": runtime_family,
        "runtime_version": runtime_version,
        "runtime": f"{runtime_family}|{runtime_version}" if runtime_version else runtime_family,
        "registry": registry,
        "registry_type": registry_type,
        "dt_include": dt_include,
    }


# ----------------------------------------------------------------------------- inventory

def graph_inventory(subscription_ids, subscription=None):
    """Bulk-fetch all web sites across the given subscriptions via Resource Graph.
    Under --rest, pass `subscription` so the token is scoped correctly."""
    kql = (
        "resources "
        "| where type =~ 'microsoft.web/sites' "
        "| project name, kind, resourceGroup, subscriptionId, location, "
        "  linuxFx=properties.siteConfig.linuxFxVersion, "
        "  windowsFx=properties.siteConfig.windowsFxVersion, "
        "  state=properties.state "
        "| order by subscriptionId asc, name asc"
    )
    log(f"[inventory] querying Resource Graph across {len(subscription_ids)} subscription(s)"
        + (" via ARM REST" if USE_REST else "") + "...")
    if USE_REST:
        rows = arm_graph(kql, subscription_ids, subscription=subscription)
        log(f"[inventory] Resource Graph returned {len(rows)} site(s)")
        return rows
    rows, skip = [], 0
    while True:
        args = ["graph", "query", "-q", kql, "--first", "1000", "--skip", str(skip), "-o", "json"]
        for sid in subscription_ids:
            args += ["--subscriptions", sid]
        data = run_az(args)
        batch = data.get("data", []) if isinstance(data, dict) else (data or [])
        rows.extend(batch)
        if len(batch) >= 1000:
            log(f"[inventory] ...paged {len(rows)} so far")
        if len(batch) < 1000:
            break
        skip += len(batch)
    log(f"[inventory] Resource Graph returned {len(rows)} site(s)")
    return rows


def arm_inventory(subscription_ids, resource_group=None):
    """ARM-based inventory via `az webapp list` — immediate (no Resource Graph
    indexing lag), used as a fallback and for freshly-created resources."""
    rows = []
    n = len(subscription_ids)
    log(f"[inventory] querying ARM (az webapp list) across {n} subscription(s) "
        f"— this enriches siteConfig per site and can be slow...")
    for i, sid in enumerate(subscription_ids, 1):
        log(f"[inventory] ARM list {i}/{n} (subscription {sid[:8]}...)"
            + (f" rg={resource_group}" if resource_group else ""))
        args = ["webapp", "list", "--subscription", sid, "-o", "json",
                "--query", "[].{name:name, kind:kind, resourceGroup:resourceGroup, "
                "subscriptionId:'" + sid + "', location:location, state:state, "
                "linuxFx:siteConfig.linuxFxVersion, windowsFx:siteConfig.windowsFxVersion}"]
        if resource_group:
            args += ["-g", resource_group]
        # `az webapp list` enriches siteConfig per site and is slow (~20s/app),
        # so allow a generous timeout.
        data = run_az(args, check=False, timeout=600) or []
        log(f"[inventory] ARM list {i}/{n} -> {len(data)} site(s)")
        rows.extend(data)
    return rows


def enrich_startup(app):
    """Fetch the startup command + DT instrumentation state for one app (per-app az call).

    Resilient: a slow/failed per-app ARM call degrades to unknown rather than
    aborting the whole fleet run.
    """
    try:
        return _enrich_startup(app)
    except Exception as e:  # noqa: BLE001
        app.setdefault("startup_command", None)
        app.setdefault("app_settings", {})
        app.setdefault("dt_instrumented", None)
        app["enrich_error"] = str(e)
        return app


def _enrich_startup(app):
    rg, name, sub = app["resource_group"], app["name"], app["subscription_id"]

    if USE_REST:
        # ARM REST: ~30x faster than az here (no per-call CLI module load).
        s = app.get("subscription_id")
        cfg = arm_resource("GET", app["id"], "/config/web", subscription=s)
        cfg_ok = isinstance(cfg, dict) and "properties" in cfg
        startup = (cfg["properties"].get("appCommandLine") or "") if cfg_ok else ""
        app["startup_command"] = startup if cfg_ok else None

        setres = arm_resource("POST", app["id"], "/config/appsettings/list", subscription=s)
        settings_ok = isinstance(setres, dict) and "properties" in setres
        names = dict(setres["properties"]) if settings_ok else {}
        app["app_settings"] = names
    else:
        # Startup command: `config show` accepts --ids.
        cfg = run_az([
            "webapp", "config", "show",
            "--ids", app["id"],
            "-o", "json",
        ], check=False, timeout=300)
        cfg_ok = isinstance(cfg, dict)
        startup = (cfg.get("appCommandLine") or "") if cfg_ok else ""
        app["startup_command"] = startup if cfg_ok else None

        # App settings: NOTE `appsettings list` does NOT accept --ids — it requires
        # -g/-n (/--subscription). Using --ids here silently returns nothing.
        settings = run_az([
            "webapp", "config", "appsettings", "list",
            "-g", rg, "-n", name, "--subscription", sub, "-o", "json",
        ], check=False, timeout=300)
        settings_ok = isinstance(settings, list)
        names = {s.get("name"): s.get("value") for s in (settings or []) if isinstance(s, dict)}
        app["app_settings"] = names

    if not cfg_ok and not settings_ok:
        # Both reads failed (typically az timeout) — state is genuinely UNKNOWN.
        # Do NOT assert "not instrumented"; that would mislead and could drive a
        # wrong deploy decision.
        app["dt_instrumented"] = None
        app["enrich_error"] = "az config/appsettings reads failed (likely timeout)"
        return app

    app["dt_instrumented"] = (
        any(k in names for k in ("DT_ENDPOINT", "DT_API_TOKEN", "DT_TENANT"))
        or _looks_instrumented(startup)
    )
    return app


# ----------------------------------------------------------------------------- scope

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def list_subscriptions_rest():
    """Subscriptions via ARM REST — ONLY the token's home tenant (single-tenant)."""
    out = _arm_call("GET", f"{ARM}/subscriptions?api-version=2020-01-01")
    vals = (out or {}).get("value", []) if isinstance(out, dict) else []
    return [{"id": v.get("subscriptionId", ""), "name": v.get("displayName", ""),
             "state": v.get("state", ""), "tenant": v.get("tenantId", "")} for v in vals]


def list_subscriptions_az():
    """Subscriptions via `az account list --all` — CROSS-tenant (all logged-in tenants)."""
    subs = run_az(["account", "list", "--all", "-o", "json"], check=False) or []
    return [{"id": s.get("id", ""), "name": s.get("name", ""),
             "state": s.get("state", ""), "tenant": s.get("tenantId", "")} for s in subs]


def list_subscriptions():
    return list_subscriptions_rest() if USE_REST else list_subscriptions_az()


def _sub_matches(args, s):
    """Each --subscription value matches by exact GUID (id) or, if not a GUID, as a
    name substring/glob against the subscription's display name."""
    if args.all_subscriptions:
        return True
    if not args.subscription:
        return False
    sid, name = s.get("id", ""), s.get("name", "")
    for v in args.subscription:
        if _GUID_RE.match(v):
            if v.lower() == sid.lower():
                return True
        elif _matches_any([v], name):
            return True
    return False


def _pick_subs(subs, args):
    return [s for s in subs if s.get("state") == "Enabled" and _sub_matches(args, s)]


def resolve_subscriptions(args):
    subs = list_subscriptions()
    chosen = _pick_subs(subs, args)
    want_scope = bool(args.subscription or args.all_subscriptions)

    # ARM REST /subscriptions is SINGLE-tenant. If a scope didn't fully resolve,
    # enumerate across tenants via az and retry — BUT only when the TOKEN itself is
    # the delegated az user. A managed identity / service principal is a different,
    # single identity: pulling in az's cross-tenant subscriptions (which it can't
    # access) just produces wrong-tenant 401s. So gate on AUTH_MODE == "az".
    unmatched_ids = ([v for v in (args.subscription or [])
                      if _GUID_RE.match(v) and v not in {s["id"] for s in chosen}])
    if (USE_REST and AUTH_MODE == "az"
            and (args.all_subscriptions or unmatched_ids or (want_scope and not chosen))):
        az_subs = list_subscriptions_az()
        if az_subs:
            known = {s["id"] for s in subs}
            if any(s["id"] not in known for s in az_subs):
                log(f"[scope] REST list is single-tenant; az enumerates "
                    f"{len(az_subs)} subscription(s) across tenants — matching against those")
            merged = {s["id"]: s for s in subs}
            for s in az_subs:
                merged.setdefault(s["id"], s)
            subs = list(merged.values())
            chosen = _pick_subs(subs, args)

    # Per-value resolution: a GUID not yet matched is trusted (delegated az mints
    # its tenant on demand); a name/glob that matched nothing is reported.
    if args.subscription:
        by_id = {s.get("id"): s for s in subs}
        have = {s.get("id") for s in chosen}
        chosen_names = {s.get("name") for s in chosen}
        for val in args.subscription:
            if _GUID_RE.match(val):
                if val in have:
                    continue
                if auth_is_single_identity() and val not in by_id:
                    # A managed identity / SP can only reach subscriptions it was
                    # granted — trusting an unknown GUID would just 401.
                    log(f"[scope] subscription {val} is not accessible to the "
                        f"{AUTH_MODE}; grant it access or use --auth az", err=True)
                    continue
                s = by_id.get(val) or {"id": val, "name": val, "state": "Enabled", "tenant": None}
                log(f"[scope] trusting explicit subscription id {val}"
                    + (f" (tenant {s.get('tenant')})" if s.get("tenant") else ""))
                chosen.append(s)
                have.add(val)
            elif not any(_matches_any([val], n) for n in chosen_names):
                log(f"[scope] '{val}' matched no subscription id or name", err=True)

    # Managed identity / SP: if nothing matched, show what IS reachable so the user
    # can see whether it's an access grant or a naming issue (not a tool failure).
    if not chosen and want_scope and auth_is_single_identity():
        reachable = [s.get("name", s.get("id")) for s in subs if s.get("state") == "Enabled"]
        shown = ", ".join(reachable[:12]) + (" ..." if len(reachable) > 12 else "")
        log(f"[scope] the {AUTH_MODE} can reach {len(reachable)} subscription(s): "
            f"{shown or '(none)'}", err=True)
        log("[scope] your --subscription matched none of them. Grant the identity "
            "access to the target subscription, or run with --auth az.", err=True)

    # No scope given: REST -> all enabled (home tenant); az -> current subscription.
    if not chosen and not want_scope:
        if USE_REST:
            chosen = [s for s in subs if s.get("state") == "Enabled"]
        else:
            cur = run_az(["account", "show", "-o", "json"])
            if cur:
                chosen = [{"id": cur.get("id", ""), "name": cur.get("name", ""),
                           "state": cur.get("state", ""), "tenant": cur.get("tenantId", "")}]
    return chosen


def build_app_records(args, subs):
    sub_ids = [s["id"] for s in subs]
    sub_name = {s["id"]: s.get("name", s["id"]) for s in subs}
    sub_tenant = {s["id"]: s.get("tenant") for s in subs}
    want_arm = getattr(args, "use_arm", False) or bool(args.resource_group)

    def graph_matches(raw):
        return (not args.app_service_name) or any(_matches_any(args.app_service_name, s.get("name")) for s in raw)

    if USE_REST:
        raw = []
        if want_arm:
            raw = arm_webapp_list(sub_ids, resource_group=args.resource_group)
        elif auth_is_single_identity():
            # One identity/token -> a single Graph call across all its subscriptions.
            raw = graph_inventory(sub_ids)
            if not graph_matches(raw):
                log("[inventory] Graph returned no name-filter matches; "
                    "falling back to ARM REST site list (handles indexing lag)...")
                raw = arm_webapp_list(sub_ids)
        else:
            # Delegated az: tokens are per-subscription (cross-tenant guest/Lighthouse),
            # so inventory each subscription with its own token.
            for sid in sub_ids:
                raw += graph_inventory([sid], subscription=sid)
            if not graph_matches(raw):
                log("[inventory] Graph returned no name-filter matches; "
                    "falling back to ARM REST site list (handles indexing lag)...")
                raw = arm_webapp_list(sub_ids)
    else:
        # az path handles cross-tenant natively.
        if want_arm:
            raw = arm_inventory(sub_ids, args.resource_group)
        else:
            raw = graph_inventory(sub_ids)
            if not graph_matches(raw):
                log("[inventory] Graph returned no name-filter matches; "
                    "falling back to ARM (handles indexing lag)...")
                raw = arm_inventory(sub_ids)

    records = []
    for site in raw:
        name = site.get("name", "")
        rg = site.get("resourceGroup", "")
        if args.app_service_name and not _matches_any(args.app_service_name, name):
            continue
        if args.resource_group and rg.lower() != args.resource_group.lower():
            continue
        cls = classify(site)
        sid = site.get("subscriptionId", "")
        rec = {
            "name": name,
            "resource_group": rg,
            "subscription_id": sid,
            "subscription_name": sub_name.get(sid, sid),
            "tenant": sub_tenant.get(sid),
            "location": site.get("location", ""),
            "state": site.get("state", ""),
            "id": f"/subscriptions/{sid}/resourceGroups/{rg}/providers/Microsoft.Web/sites/{name}",
            "startup_command": None,
            "dt_instrumented": None,
        }
        rec.update(cls)
        records.append(rec)
    return records


def enrich_parallel(records, workers=None):
    # az calls are slow + resource-heavy -> keep concurrency low; ARM REST is light
    # and fast -> parallelize hard.
    if workers is None:
        workers = 16 if USE_REST else 3
    total = len(records)
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(enrich_startup, r): r for r in records}
        for fut in concurrent.futures.as_completed(futs):
            fut.result()  # enrich_startup swallows its own errors
            r = futs[fut]
            done += 1
            if r.get("enrich_error"):
                state = "enrich-error"
            elif r.get("dt_instrumented"):
                state = "already-instrumented"
            else:
                state = "not-instrumented"
            log(f"[enrich] ({done}/{total}) {r['name']}  [{state}]")
    return records


# ----------------------------------------------------------------------------- reporting

def distribution_table(records):
    """Build the runtime x deployment-model distribution as rows."""
    buckets = {}
    for r in records:
        fam = r["runtime_family"] or "(unknown)"
        if r["deploy_model"] == "container":
            model = f"container:{r['registry_type'] or 'Other'}"
        elif r["deploy_model"] == "function":
            model = "function"
        else:
            model = "code/core"
        key = (fam, model)
        buckets[key] = buckets.get(key, 0) + 1
    rows = [{"runtime": k[0], "deployment": k[1], "count": v}
            for k, v in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))]
    return rows


def ascii_table(headers, rows):
    cols = list(headers)
    widths = [len(h) for h in cols]
    srows = []
    for r in rows:
        sr = [("" if r.get(c) is None else str(r.get(c))) for c in cols]
        srows.append(sr)
        widths = [max(w, len(c)) for w, c in zip(widths, sr)]
    line = "  ".join(h.ljust(w) for h, w in zip(cols, widths))
    sep = "  ".join("-" * w for w in widths)
    body = "\n".join("  ".join(c.ljust(w) for c, w in zip(sr, widths)) for sr in srows)
    return f"{line}\n{sep}\n{body}"


def render_report(records, fmt):
    dist = distribution_table(records)
    total = len(records)
    n_code = sum(1 for r in records if r["deploy_model"] == "code")
    n_cont = sum(1 for r in records if r["deploy_model"] == "container")
    n_func = sum(1 for r in records if r["deploy_model"] == "function")
    n_linux_code = sum(1 for r in records if r["deploy_model"] == "code" and r["is_linux"])
    n_instr = sum(1 for r in records if r.get("dt_instrumented"))

    detail_cols = ["subscription_name", "resource_group", "name", "kind",
                   "runtime", "deploy_model", "registry_type", "dt_instrumented",
                   "startup_command"]

    if fmt == "json":
        return json.dumps({
            "generated": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total": total, "code": n_code, "container": n_cont,
                "function": n_func, "linux_code_deployable": n_linux_code,
                "already_instrumented": n_instr,
            },
            "distribution": dist,
            "apps": records,
        }, indent=2, default=str)

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(detail_cols)
        for r in records:
            w.writerow([r.get(c) for c in detail_cols])
        return buf.getvalue()

    # txt / md
    md = fmt == "md"
    out = []
    h1 = "# " if md else ""
    out.append(f"{h1}Dynatrace OneAgent — Azure App Service Fleet Assessment")
    out.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    out.append("")
    out.append(("## " if md else "") + "Summary")
    out.append(f"  Total App Services assessed : {total}")
    out.append(f"  Code / core (built-in image): {n_code}  (Linux deployable: {n_linux_code})")
    out.append(f"  Standalone container        : {n_cont}")
    out.append(f"  Function Apps (skipped)     : {n_func}")
    out.append(f"  Already DT-instrumented     : {n_instr}")
    out.append("")
    out.append(("## " if md else "") + "Distribution by runtime x deployment model")
    if md:
        out.append("| runtime | deployment | count |")
        out.append("|---|---|---|")
        for d in dist:
            out.append(f"| {d['runtime']} | {d['deployment']} | {d['count']} |")
    else:
        out.append(ascii_table(["runtime", "deployment", "count"], dist))
    out.append("")
    out.append(("## " if md else "") + "Per-app detail")
    if any(r.get("startup_command") is not None for r in records):
        if md:
            out.append("| " + " | ".join(detail_cols) + " |")
            out.append("|" + "|".join("---" for _ in detail_cols) + "|")
            for r in records:
                vals = [str(r.get(c) if r.get(c) is not None else "").replace("|", "\\|") for c in detail_cols]
                out.append("| " + " | ".join(vals) + " |")
        else:
            out.append(ascii_table(detail_cols, records))
    else:
        out.append("  (startup command not fetched — run with --detailed for per-app startup/DT state)")
        out.append(ascii_table(["subscription_name", "name", "kind", "runtime", "deploy_model", "registry_type"], records))
    return "\n".join(out)


def emit(report, outfile):
    if outfile:
        parent = os.path.dirname(os.path.abspath(outfile))
        os.makedirs(parent, exist_ok=True)
        with open(outfile, "w") as fh:
            fh.write(report if report.endswith("\n") else report + "\n")
        log(f"[assess] report written to {outfile}")
    else:
        print(report)


# ----------------------------------------------------------------------------- deploy

# Self-contained startup wrapper. All $VARS resolve from app settings at container
# boot. Fails LOUD: if download or install fails, the app does not launch (so a
# missing agent can never silently run un-instrumented). LD_PRELOAD is inline for
# the app process only — never a container-wide app setting.
STARTUP_WRAPPER = (
    'wget --no-check-certificate -O /tmp/dt-oneagent-install.sh '
    '"$DT_ENDPOINT/api/v1/deployment/installer/agent/unix/paas-sh/latest'
    '?Api-Token=$DT_API_TOKEN&flavor=$DT_FLAVOR&include=$DT_INCLUDE&arch=x86" '
    '&& sh /tmp/dt-oneagent-install.sh '
    '&& LD_PRELOAD=/opt/dynatrace/oneagent/agent/lib64/liboneagentproc.so $START_APP_CMD'
)

# Resilient wrapper (--resilient): installs the agent to /home/dynatrace (the
# persistent Azure Files mount), so it survives restarts/scale and is downloaded
# ONCE instead of on every cold start; and is fail-OPEN — if the agent can't be
# obtained, the app still starts (without the preload) rather than failing to boot.
# The .so path is discovered (install nesting under a target dir differs from /opt).
STARTUP_WRAPPER_RESILIENT = (
    'DT_HOME=/home/dynatrace; '
    'DT_LIB=$(find "$DT_HOME" -name liboneagentproc.so 2>/dev/null | head -1); '
    'if [ -z "$DT_LIB" ]; then '
    'wget --no-check-certificate -O /tmp/dt-oneagent-install.sh '
    '"$DT_ENDPOINT/api/v1/deployment/installer/agent/unix/paas-sh/latest'
    '?Api-Token=$DT_API_TOKEN&flavor=$DT_FLAVOR&include=$DT_INCLUDE&arch=x86" '
    '&& sh /tmp/dt-oneagent-install.sh "$DT_HOME" '
    '&& DT_LIB=$(find "$DT_HOME" -name liboneagentproc.so 2>/dev/null | head -1); '
    'fi; '
    'if [ -n "$DT_LIB" ]; then echo "[dt-fleet] OneAgent: $DT_LIB"; export LD_PRELOAD="$DT_LIB"; '
    'else echo "[dt-fleet] WARNING: OneAgent unavailable; starting WITHOUT instrumentation (fail-open)"; fi; '
    'exec $START_APP_CMD'
)

DT_MARKER = "dt-oneagent-install.sh"


def _looks_instrumented(startup):
    """True if a startup command already launches via a OneAgent installer wrapper
    (ours *or* a foreign one — e.g. the hand-rolled paas-sh snippet). Used so we
    never capture a wrapper as START_APP_CMD or stash it as the 'original'."""
    s = (startup or "").lower()
    return (DT_MARKER in s) or ("liboneagentproc" in s) or ("paas-sh/latest" in s)


def deployable_reason(app):
    """Return None if deployable, else a human reason why not."""
    if app["is_function"]:
        return "Function App (Web-App startup/LD_PRELOAD path is inert)"
    if not app["is_linux"]:
        return "Windows App Service (this tool targets Linux built-in image)"
    if app["is_container"]:
        return "standalone container (use custom-image/sidecar path, not built-in)"
    if not app["dt_include"]:
        return f"no OneAgent mapping for runtime '{app['runtime_family']}'"
    return None


def deploy_one(app, creds, args):
    name, rg = app["name"], app["resource_group"]
    sub = app["subscription_id"]
    reason = deployable_reason(app)
    result = {"name": name, "rg": rg, "status": "", "detail": ""}
    if reason:
        result["status"] = "skipped"
        result["detail"] = reason
        return result

    # Resolve START_APP_CMD. Priority:
    #   1. an existing *non-wrapper* startup command (the app's real launch line)
    #   2. an explicit --start-app-cmd override
    #   3. the START_APP_CMD we captured on a prior deploy (idempotent re-run)
    # If the only startup command present is itself an installer wrapper (ours or a
    # foreign one) and we have no captured START_APP_CMD, we refuse rather than
    # nest a wrapper inside a wrapper.
    existing = (app.get("startup_command") or "").strip()
    app_settings = app.get("app_settings", {}) or {}
    prior_start = app_settings.get("START_APP_CMD")
    if existing and not _looks_instrumented(existing):
        start_cmd = existing
    elif args.start_app_cmd:
        start_cmd = args.start_app_cmd
    elif prior_start:
        start_cmd = prior_start
    else:
        defaults = {
            "nodejs": "",            # no safe universal default — must be explicit
            "python": "",
            "dotnet": "",
            "java": "",
        }
        start_cmd = defaults.get(app["dt_include"], "")
        if not start_cmd:
            result["status"] = "needs-input"
            if _looks_instrumented(existing):
                result["detail"] = ("already has an installer wrapper as its startup "
                                     "command but no captured START_APP_CMD (foreign "
                                     "instrumentation); pass --start-app-cmd with the "
                                     "real launch command to adopt it safely")
            else:
                result["detail"] = ("no existing Startup Command and none provided; "
                                     "pass --start-app-cmd (app relied on implicit default)")
            return result

    include = args.dt_include or app["dt_include"]
    flavor = args.dt_flavor

    # Dynatrace SERVICE entity name. Default is the Azure site name; override with
    # --service-name (supports {name}/{rg}/{sub} placeholders). Per gotcha §7 a name
    # distinct from the site name keeps the service entity separable from the
    # process group (which already derives its identity from the site name).
    if args.service_name:
        service_name = args.service_name.format(name=name, rg=rg, sub=app["subscription_id"])
    else:
        service_name = name

    # Custom ENVIRONMENT tags via DT_TAGS. Tags sourced from the DT_TAGS env var
    # surface in Dynatrace as [Environment]<key>:<value> (OneAgent attribute
    # enrichment). We always stamp the real Azure site name as a reliable identity
    # tag — this does NOT depend on OTel-awareness and attaches to the process group,
    # which is the App Service's identity on the PaaS (application-only) path.
    #   DT_TAGS="azure.app_service_name=<site> managed-by=dt-fleet env=<tag> ..."
    tag_list = [
        f"azure.app_service_name={name}",
        "managed-by=dt-fleet",
        f"env={args.env_tag}",
    ]
    for t in (args.set_host_tag or []):
        tag_list.append(t.format(name=name, rg=rg, sub=app["subscription_id"]))
    dt_tags = " ".join(tag_list)
    vlog(f"[{name}] DT_TAGS = {dt_tags}")

    # Settings as a dict (ARM REST PUT needs a dict; az needs K=V strings).
    settings_dict = {
        "DT_ENDPOINT": creds["env_url"],
        "DT_API_TOKEN": creds["paas_token"],
        "DT_INCLUDE": include,
        "DT_FLAVOR": flavor,
        "START_APP_CMD": start_cmd,
        # push agent logs to the App Service log stream (container FS is ephemeral)
        "DT_LOGSTREAM": "stdout",
        "DT_LOGLEVELCON": "INFO",
        # naming: distinguish the service entity from the process group
        "OTEL_SERVICE_NAME": service_name,
        "DT_TAGS": dt_tags,
    }
    if args.otel_resource_attributes:
        settings_dict["OTEL_RESOURCE_ATTRIBUTES"] = args.otel_resource_attributes
    if args.host_name:
        settings_dict["DT_LOCALTOVIRTUALHOSTNAME"] = \
            args.host_name.format(name=name, rg=rg, sub=app["subscription_id"])

    # Resilient path: persist the agent to the /home Azure Files mount (needs the
    # storage mount enabled) and launch fail-open.
    wrapper = STARTUP_WRAPPER_RESILIENT if args.resilient else STARTUP_WRAPPER
    if args.resilient:
        settings_dict["WEBSITES_ENABLE_APP_SERVICE_STORAGE"] = "true"

    if args.what_if:
        result["status"] = "would-deploy"
        extra = ("".join([" resilient(/home,fail-open)" if args.resilient else "",
                          " via-REST" if USE_REST else ""]))
        result["detail"] = f"START_APP_CMD='{start_cmd}' include={include}{extra}"
        return result

    # Stash the ORIGINAL startup command so uninstall can restore it. Idempotency:
    #   - if we already captured it on a prior deploy, preserve that value (never
    #     overwrite it with the wrapper on a re-run);
    #   - else, if the current startup command is a real (non-wrapper) command, save it;
    #   - else save empty (don't stash a wrapper as the "original").
    if "DT_FLEET_ORIG_STARTUP" in app_settings:
        orig_val = app_settings["DT_FLEET_ORIG_STARTUP"]
        vlog(f"[{name}] preserving existing DT_FLEET_ORIG_STARTUP='{orig_val}'")
    elif existing and not _looks_instrumented(existing):
        orig_val = existing
    else:
        orig_val = ""
    settings_dict["DT_FLEET_ORIG_STARTUP"] = orig_val

    vlog(f"[{name}] START_APP_CMD = {start_cmd}")
    vlog(f"[{name}] settings = {[redact(f'{k}={v}') for k, v in settings_dict.items()]}")
    vlog(f"[{name}] startup wrapper = {wrapper}")

    writer = "ARM REST" if USE_REST else "az"
    wlabel = "resilient wrapper (/home, fail-open)" if args.resilient else "installer wrapper"
    try:
        log(f"    [{name}] 1/4 writing {len(settings_dict)} app settings (DT_*, START_APP_CMD) [{writer}]...")
        if USE_REST:
            arm_set_appsettings(app, settings_dict)
        else:
            run_az(["webapp", "config", "appsettings", "set", "--ids", app["id"],
                    "--settings", *[f"{k}={v}" for k, v in settings_dict.items()], "-o", "none"])
        log(f"    [{name}] 2/4 swapping startup command -> OneAgent {wlabel}...")
        if USE_REST:
            arm_set_startup(app, wrapper)
        else:
            run_az(["webapp", "config", "set", "--ids", app["id"],
                    "--startup-file", wrapper, "-o", "none"])
        # restart twice: once to install, once to instrument
        log(f"    [{name}] 3/4 restart #1 (triggers agent download + install)...")
        if USE_REST:
            arm_restart(app)
        else:
            run_az(["webapp", "restart", "--ids", app["id"], "-o", "none"])
        log(f"    [{name}] waiting {args.restart_wait}s for the agent to install before restart #2...")
        sleep_with_heartbeat(args.restart_wait, f"[{name}] install wait")
        log(f"    [{name}] 4/4 restart #2 (instrument)...")
        if USE_REST:
            arm_restart(app)
        else:
            run_az(["webapp", "restart", "--ids", app["id"], "-o", "none"])
        result["status"] = "deployed"
        result["detail"] = f"include={include} START_APP_CMD='{start_cmd}'"
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["detail"] = redact(str(e))
    return result


def uninstall_one(app, args):
    name, rg = app["name"], app["resource_group"]
    result = {"name": name, "rg": rg, "status": "", "detail": ""}
    settings = app.get("app_settings", {})
    orig = settings.get("DT_FLEET_ORIG_STARTUP", "")
    if args.what_if:
        result["status"] = "would-uninstall"
        result["detail"] = f"restore startup='{orig}'"
        return result
    keys = ["DT_ENDPOINT", "DT_API_TOKEN", "DT_INCLUDE", "DT_FLAVOR",
            "START_APP_CMD", "DT_LOGSTREAM", "DT_LOGLEVELCON", "OTEL_SERVICE_NAME",
            "OTEL_RESOURCE_ATTRIBUTES", "DT_LOCALTOVIRTUALHOSTNAME",
            "DT_TAGS", "DT_FLEET_ORIG_STARTUP"]
    try:
        log(f"    [{name}] 1/3 restoring original startup command ('{orig or '(empty)'}')...")
        if USE_REST:
            arm_set_startup(app, orig)
        else:
            run_az(["webapp", "config", "set", "--ids", app["id"],
                    "--startup-file", orig, "-o", "none"])
        log(f"    [{name}] 2/3 removing {len(keys)} DT_* app settings...")
        if USE_REST:
            arm_delete_appsettings(app, keys)
        else:
            run_az(["webapp", "config", "appsettings", "delete", "--ids", app["id"],
                    "--setting-names", *keys, "-o", "none"], check=False)
        log(f"    [{name}] 3/3 restarting...")
        if USE_REST:
            arm_restart(app)
        else:
            run_az(["webapp", "restart", "--ids", app["id"], "-o", "none"])
        result["status"] = "uninstalled"
        result["detail"] = f"restored startup='{orig}'"
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["detail"] = redact(str(e))
    return result


# ----------------------------------------------------------------------------- verify

def kudu_get(app, vfs_path):
    """GET a Kudu/SCM VFS path using the ARM bearer token (no basic auth needed)."""
    url = f"https://{app['name']}.scm.azurewebsites.net/api/vfs/{vfs_path}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {arm_token(app.get('subscription_id'))}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        if VERBOSE:
            vlog(f"kudu GET {vfs_path} failed: {redact(str(e))}")
        return None


# ---- Dynatrace-side confirmation (does the entity actually report?) ----------
# Needs a Dynatrace READ credential. Two options, preferred first:
#   1. creds.dtApiToken  — classic API token with `entities.read`  -> /api/v2/entities
#   2. dtctl             — a configured context for the tenant (user OAuth)  -> DQL
# Without either, the DT-side check is skipped (the Azure-side log check still runs).

_DT_TAG = "[Environment]azure.app_service_name:{name}"


def _dt_check_api(creds, name):
    """Query /api/v2/entities for the app's process group by our tag."""
    sel = (f'type("PROCESS_GROUP_INSTANCE"),'
           f'tag("azure.app_service_name:{name}")')
    url = (f"{creds['env_url']}/api/v2/entities?entitySelector={urllib.parse.quote(sel)}"
           f"&from=now-24h&pageSize=20&fields=tags")
    req = urllib.request.Request(url, headers={"Authorization": f"Api-Token {creds['api_token']}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        ents = d.get("entities", [])
        if ents:
            return "present", f"{ents[0].get('displayName')} ({ents[0].get('entityId')})"
        return "absent", "no matching process group in Dynatrace (last 24h)"
    except urllib.error.HTTPError as e:
        return "skipped", f"entities API HTTP {e.code} — token needs the 'entities.read' scope"
    except Exception as e:  # noqa: BLE001
        return "skipped", f"entities API error: {redact(str(e))}"


def _dtctl_bin():
    return shutil.which("dtctl") or next(
        (p for p in [os.path.expanduser("~/.local/bin/dtctl"), "/usr/local/bin/dtctl"]
         if os.path.exists(p)), None)


def _dtctl_context(dtctl, tenant):
    try:
        out = subprocess.run([dtctl, "config", "get-contexts", "-o", "json", "--plain"],
                             capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "null")
        ctxs = data.get("result", data) if isinstance(data, dict) else data
        for c in ctxs or []:
            if tenant and tenant.lower() in (str(c.get("Environment", "")) + str(c.get("Name", ""))).lower():
                return c.get("Name")
    except Exception:  # noqa: BLE001
        pass
    return None


def _dt_check_dtctl(tenant, name):
    dtctl = _dtctl_bin()
    if not dtctl:
        return None
    ctx = _dtctl_context(dtctl, tenant)
    if not ctx:
        return None
    dql = (f'fetch dt.entity.process_group_instance, from:now()-24h | fieldsAdd tags '
           f'| filter in("{_DT_TAG.format(name=name)}", tags) '
           f'| fields name=entity.name, id | limit 5')
    try:
        out = subprocess.run([dtctl, "query", dql, "--context", ctx, "-o", "json", "--plain"],
                             capture_output=True, text=True, timeout=90)
        recs = (json.loads(out.stdout or "{}") or {}).get("records", [])
        if recs:
            return "present", f"{recs[0].get('name')} ({recs[0].get('id')}) via dtctl/{ctx}"
        return "absent", f"no matching process group in Dynatrace (dtctl/{ctx}, last 24h)"
    except Exception as e:  # noqa: BLE001
        return "skipped", f"dtctl query error: {redact(str(e))}"


def dt_entity_check(app, creds):
    """(state, detail) with state in {present, absent, skipped}."""
    if not creds:
        return "skipped", "no creds for Dynatrace-side check"
    name = app["name"]
    if creds.get("api_token"):
        return _dt_check_api(creds, name)
    res = _dt_check_dtctl(creds.get("tenant"), name)
    if res is not None:
        return res
    return ("skipped", "no Dynatrace read path (add creds.dtApiToken with 'entities.read', "
                       "or configure dtctl for this tenant)")


def verify_one(app, creds=None):
    """Confirm the OneAgent actually loaded — distinct from 'deploy succeeded'.
    Reads the newest container docker log via Kudu (Azure side) and, when a
    Dynatrace read credential is available, confirms the entity reports (DT side)."""
    name = app["name"]
    res = {"name": name, "rg": app["resource_group"], "verdict": "", "detail": ""}
    listing = kudu_get(app, "LogFiles/")
    if listing is None:
        res["verdict"] = "unreachable"
        res["detail"] = "could not read container logs via SCM (Linux app? access? basic-auth/network?)"
        return res
    try:
        files = [f.get("name", "") for f in json.loads(listing) if isinstance(f, dict)]
    except Exception:  # noqa: BLE001
        files = []
    docker = sorted(f for f in files if f.endswith("default_docker.log"))
    if not docker:
        res["verdict"] = "no-log"
        res["detail"] = "no *_default_docker.log yet (enable App Service logging, or no recent boot)"
        return res
    # Boot markers (install/LD_PRELOAD) rotate out of the current file, so read the
    # newest few logs together; the agent's live "Connected to ..." lines are enough
    # on their own to prove instrumentation.
    chunks = [c for c in (kudu_get(app, f"LogFiles/{fn}") for fn in docker[-3:]) if c]
    t = "\n".join(chunks).lower()
    if not t:
        res["verdict"] = "unreachable"
        res["detail"] = "container log fetched empty"
        return res

    fail_open = "[dt-fleet] warning: oneagent unavailable" in t
    boot = ("[dt-fleet] oneagent:" in t) or ("liboneagentproc" in t) or ("unpacking to" in t)
    connected = ("/communication" in t and "dynatrace.com" in t) or "connected to https://" in t
    inactivated = ("agent inactivated" in t) or ("active state to off" in t)
    launched = any(s in t for s in ("listening on", "server running", "application startup complete",
                                    "started server process", "booting worker", "serving"))

    if not (connected or boot) and fail_open:
        res["verdict"] = "FAIL-OPEN — NOT instrumented"
        res["detail"] = ("wrapper could not obtain the agent; app started WITHOUT it. "
                         "Check DT_API_TOKEN scope + DT_ENDPOINT for this tenant, and egress to it.")
    elif inactivated:
        res["verdict"] = "injected but AGENT INACTIVATED"
        res["detail"] = ("agent loaded but the tenant disabled this process group "
                         "(monitored technologies / PG rule — see gotchas section 6)")
    elif connected:
        res["verdict"] = "INSTRUMENTED (connected)"
        res["detail"] = ("agent loaded + connected to Dynatrace"
                         + ("; app launched" if launched else "")
                         + ("; install confirmed" if boot else ""))
    elif boot:
        res["verdict"] = "injected, connection unconfirmed"
        res["detail"] = "agent loaded; no 'Connected to ...' line yet (cold-starting, or egress blocked)"
    else:
        res["verdict"] = "no agent markers"
        res["detail"] = "no OneAgent lines in recent logs (cold-starting? logging off? not instrumented?)"

    # Dynatrace-side confirmation: is the process group actually reporting?
    if creds:
        dt_state, dt_detail = dt_entity_check(app, creds)
        res["dt_state"] = dt_state
        res["detail"] += f"  |  Dynatrace: {dt_state} — {dt_detail}"
        if dt_state == "present" and res["verdict"].startswith("INSTRUMENTED"):
            res["verdict"] = "CONFIRMED (Azure + Dynatrace)"
    return res


# ----------------------------------------------------------------------------- ntfy

def notify(topic, title, message, tags="information_source"):
    if not topic:
        return
    try:
        subprocess.run(
            ["curl", "-s", "-m", "15",
             "-H", f"Title: {title}", "-H", f"Tags: {tags}",
             "-d", message, f"https://ntfy.sh/{topic}"],
            capture_output=True, timeout=20,
        )
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", nargs="?", default="assess",
                   choices=["assess", "deploy", "uninstall", "verify"],
                   help="assess (dry run, default) | deploy | uninstall | verify (confirm injection from container logs)")
    # scope
    p.add_argument("--subscription", "--subscription-filter", dest="subscription",
                   action="append", metavar="GUID|NAME",
                   help="subscription GUID, or name — substring, or glob with */?/[ (repeatable). "
                        "A GUID not visible to enumeration is trusted. (--subscription-filter is an alias.)")
    p.add_argument("--all-subscriptions", action="store_true", help="every enabled subscription (cross-tenant via az)")
    p.add_argument("--resource-group", help="limit to a resource group")
    p.add_argument("--app-service-name", "--name-filter", dest="app_service_name",
                   action="append", metavar="NAME",
                   help="app service name — substring, or glob with */?/[ (repeatable). (--name-filter is an alias.)")
    # output
    p.add_argument("--output-file", help="write report to file (.txt/.md/.csv/.json by extension)")
    p.add_argument("--format", choices=["txt", "md", "csv", "json"], help="override output format")
    p.add_argument("--detailed", action="store_true", help="fetch startup command + DT state per app (slower)")
    p.add_argument("--use-arm", action="store_true", help="inventory via ARM instead of Resource Graph (immediate; avoids Graph indexing lag)")
    p.add_argument("-v", "--verbose", action="store_true", help="echo every az call with timing + applied values (tokens redacted)")
    p.add_argument("--rest", action="store_true", help="use ARM REST instead of the az CLI (~30x faster when az is slow)")
    p.add_argument("--no-token-cache", action="store_true", help="do not read/write the ARM bearer token disk cache (mint fresh each run)")
    p.add_argument("--auth", choices=["auto", "az", "managed-identity", "service-principal"],
                   default="auto",
                   help="token source: auto (SP/MI env/metadata, else az login), or force one. "
                        "Use 'az' for cross-tenant delegated access; 'managed-identity' on Azure compute.")
    # deploy knobs
    p.add_argument("--creds", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "creds.json"))
    p.add_argument("--dt-include", help="override DT_INCLUDE (else inferred per runtime)")
    p.add_argument("--dt-flavor", default="default", help="DT_FLAVOR (default=glibc, musl for Alpine)")
    p.add_argument("--start-app-cmd", help="explicit START_APP_CMD for apps with no existing startup command")
    p.add_argument("--env-tag", default="demo", help="value for DT_TAGS env=")
    p.add_argument("--service-name", help="OTEL_SERVICE_NAME for the Dynatrace service entity; supports {name}/{rg}/{sub} placeholders (default: the app's site name)")
    p.add_argument("--otel-resource-attributes", help="OTEL_RESOURCE_ATTRIBUTES, e.g. 'service.namespace=retail,deployment.environment=prod'")
    p.add_argument("--host-name", help="DT_LOCALTOVIRTUALHOSTNAME for the host entity; supports {name}/{rg}/{sub} placeholders")
    p.add_argument("--set-host-tag", action="append", metavar="KEY=VALUE",
                   help="extra DT_TAGS entry shown as [Environment]KEY:VALUE; supports {name}/{rg}/{sub} placeholders (repeatable). "
                        "azure.app_service_name=<site> is always added.")
    p.add_argument("--restart-wait", type=int, default=45, help="seconds between the two restarts")
    p.add_argument("--resilient", action="store_true", help="persist agent to /home (survives restarts, no re-download) and launch fail-open (app still starts if OneAgent is unavailable)")
    p.add_argument("--what-if", action="store_true", help="deploy/uninstall: show actions without applying")
    p.add_argument("--force", action="store_true", help="deploy: re-apply to apps already instrumented (default skips them)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt for mutating modes")
    p.add_argument("--state-file", help="JSONL checkpoint for resumable rollouts: completed apps are recorded and skipped on re-run (errors/unknowns are retried)")
    p.add_argument("--verify", action="store_true", help="deploy: after deploying, confirm injection (container log + Dynatrace entity) inline")
    p.add_argument("--no-dt-check", action="store_true", help="verify: skip the Dynatrace-side entity check (container log only)")
    p.add_argument("--verify-wait", type=int, default=60, help="deploy --verify: seconds to wait before verifying (cold start + agent connect)")
    # notify
    p.add_argument("--ntfy-topic", help="ntfy.sh topic for progress notifications")
    args = p.parse_args()

    global VERBOSE, USE_REST, NO_TOKEN_CACHE, AUTH_PREF
    VERBOSE = args.verbose
    USE_REST = args.rest
    NO_TOKEN_CACHE = args.no_token_cache
    AUTH_PREF = args.auth

    # format resolution
    fmt = args.format
    if not fmt and args.output_file:
        ext = os.path.splitext(args.output_file)[1].lower().lstrip(".")
        fmt = {"md": "md", "markdown": "md", "csv": "csv", "json": "json"}.get(ext, "txt")
    fmt = fmt or "txt"

    subs = resolve_subscriptions(args)
    if not subs:
        raise SystemExit("no subscriptions resolved — check --subscription / --subscription-filter")
    log(f"[scope] {len(subs)} subscription(s): " + ", ".join(s['name'] for s in subs))

    records = build_app_records(args, subs)
    log(f"[inventory] {len(records)} App Service(s) in scope")

    # Assess enriches all records (if --detailed) then reports. Mutating modes
    # enrich only the candidate set (and, when resuming, only the not-yet-done ones).
    if args.mode == "assess":
        if args.detailed and records:
            log(f"[enrich] fetching startup command + DT state for {len(records)} app(s)...")
            enrich_parallel(records)
        report = render_report(records, fmt)
        emit(report, args.output_file)
        if args.ntfy_topic:
            notify(args.ntfy_topic, "DT Fleet — assessment complete",
                   f"{len(records)} app services assessed across {len(subs)} subscription(s).")
        return

    if args.mode == "verify":
        if not records:
            raise SystemExit("no apps matched the scope to verify")
        vcreds = None
        if not args.no_dt_check:
            try:
                vcreds = load_creds(args.creds)
            except SystemExit:
                vcreds = None
        log(f"[verify] reading container logs{' + Dynatrace entities' if vcreds else ''} "
            f"for {len(records)} app(s)...")
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(lambda a: verify_one(a, vcreds), records):
                results.append(r)
                log(f"    {r['verdict']:32}  {r['name']}: {r['detail']}")
        ok = sum(1 for r in results if r["verdict"].startswith(("INSTRUMENTED", "CONFIRMED")))
        bad = [r for r in results if "NOT instrumented" in r["verdict"] or "INACTIVATED" in r["verdict"]]
        log(f"\n[verify] {ok}/{len(results)} confirmed instrumented")
        for r in bad:
            log(f"    !! {r['name']}: {r['verdict']}", err=True)
        return

    # --- mutating modes ---
    # Candidate set from classification only (no enrich needed yet): Linux code apps.
    candidates = [r for r in records if (r["deploy_model"] == "code" and r["is_linux"])]
    n_class = len(candidates)                 # Linux-code apps found in scope
    state_removed = 0

    # Resumability: a prior run's checkpoint lets us skip finished apps BEFORE the
    # (potentially large) enrich, so an interrupted rollout resumes cheaply.
    done_terminal = {"deployed", "uninstalled"}
    if args.state_file:
        done = load_state(args.state_file)
        done_ok = {i for i, s in done.items() if s in done_terminal}
        before = len(candidates)
        candidates = [r for r in candidates if r["id"] not in done_ok]
        state_removed = before - len(candidates)
        log(f"[resume] state file '{args.state_file}': {len(done_ok)} already done, "
            f"{len(candidates)}/{before} remaining (errors/unknowns are retried)")

    if candidates:
        log(f"[enrich] fetching startup command + DT state for {len(candidates)} app(s)...")
        enrich_parallel(candidates)

    if args.mode == "deploy":
        if args.force:
            # --force still can't act on apps whose state we couldn't read.
            actionable = [r for r in candidates if r.get("dt_instrumented") is not None]
        else:
            # Idempotent default: only act on apps KNOWN to be clean (False).
            # Skip already-instrumented (True) and unknown/unreadable (None).
            actionable = [r for r in candidates if r.get("dt_instrumented") is False]
    else:
        actionable = [r for r in candidates if r.get("dt_instrumented")]

    log(f"[{args.mode}] {len(actionable)} candidate app(s):")
    for r in actionable:
        log(f"    - {r['subscription_name']}/{r['resource_group']}/{r['name']}  [{r['runtime']}]")
    for r in [c for c in candidates if c not in actionable]:
        if args.mode == "deploy":
            why = deployable_reason(r)
            if why is None:
                if r.get("dt_instrumented") is None:
                    why = (f"state UNKNOWN — {r.get('enrich_error', 'enrich incomplete')}; "
                           "re-run before deploying")
                elif r.get("dt_instrumented"):
                    why = "already instrumented (use --force to re-apply)"
        else:
            why = "not instrumented"
        log(f"    (skip) {r['name']}: {why}")

    if not actionable:
        if n_class == 0:
            reason = (f"no Linux code App Services matched the scope "
                      f"({len(records)} app(s) in scope, none are Linux-code/deployable)")
        elif state_removed and not candidates:
            reason = f"all {state_removed} remaining app(s) already completed per state file"
        elif args.mode == "deploy":
            reason = ("nothing actionable — candidates are already instrumented "
                      "(use --force) or their state is unknown (re-run)")
        else:
            reason = "no instrumented apps found to uninstall"
        raise SystemExit(f"no apps to {args.mode} — {reason}")

    if not args.yes and not args.what_if:
        resp = input(f"\nProceed to {args.mode} {len(actionable)} app(s)? [y/N] ").strip().lower()
        if resp != "y":
            raise SystemExit("aborted")

    creds = load_creds(args.creds, require_paas=True) if args.mode == "deploy" else None
    if args.ntfy_topic:
        notify(args.ntfy_topic, f"DT Fleet — {args.mode} starting",
               f"{args.mode} on {len(actionable)} app(s).", tags="gear")

    results = []
    for i, r in enumerate(actionable, 1):
        log(f"[{args.mode}] ({i}/{len(actionable)}) {r['name']} ...")
        if args.mode == "deploy":
            res = deploy_one(r, creds, args)
        else:
            res = uninstall_one(r, args)
        log(f"    -> {res['status']}: {res['detail']}")
        results.append(res)
        # Checkpoint each app immediately so an interruption loses at most one app.
        if args.state_file and not args.what_if:
            append_state(args.state_file, r, res)

    ok = sum(1 for x in results if x["status"] in ("deployed", "uninstalled", "would-deploy", "would-uninstall"))
    log(f"\n[{args.mode}] done: {ok}/{len(results)} succeeded")
    for x in results:
        if x["status"] in ("error", "needs-input"):
            log(f"    !! {x['name']}: {x['status']} — {x['detail']}", err=True)

    # Inline verification after a real deploy (container log + Dynatrace entity).
    if args.mode == "deploy" and args.verify and not args.what_if:
        deployed = [r for r in actionable
                    if any(x["name"] == r["name"] and x["status"] == "deployed" for x in results)]
        if deployed:
            vcreds = creds if not args.no_dt_check else None
            log(f"\n[verify] waiting {args.verify_wait}s for cold start + agent connect...")
            sleep_with_heartbeat(args.verify_wait, "verify wait")
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                for v in ex.map(lambda a: verify_one(a, vcreds), deployed):
                    log(f"    {v['verdict']:32}  {v['name']}: {v['detail']}")

    if args.ntfy_topic:
        notify(args.ntfy_topic, f"DT Fleet — {args.mode} complete",
               f"{ok}/{len(results)} {args.mode} ok.", tags="white_check_mark")


if __name__ == "__main__":
    main()
