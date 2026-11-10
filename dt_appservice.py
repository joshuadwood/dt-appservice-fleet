#!/usr/bin/env python3
"""
DT Fleet — UNIFIED entry point (code + container).

One tool that instruments any Linux App Service by auto-routing each app to the mechanism
that actually works for its **Publish** model:

  * Publish: Code (built-in image)  -> code path  (startup-command wrapper, or
                                        --leave-startup-command / --preserve-oryx)
  * Publish: Container (BYOC)        -> sidecar path (dynatrace-codemodules bootstrapper)

It does ONE inventory pass, classifies every app, and dispatches to the already-validated
per-app functions in dt_appservice_fleet.py (code) and dt_appservice_sidecar.py (sidecar).
A sidecar can only attach to a Publish: Container app (Azure requires it), so code apps
never go down the sidecar path — they get the startup-command or LD_PRELOAD route instead.

Actions: assess (default, dry run) | deploy | verify | uninstall. `update` applies to the
container (sidecar) apps only. Flags are the union of both paths; each app only consumes
the flags relevant to its route.
"""
import argparse
import concurrent.futures
import os

import dt_appservice_fleet as core
import dt_appservice_sidecar as side
from dt_appservice_fleet import log, vlog


# ------------------------------------------------------------------- routing

def route_of(rec):
    """('container'|'code'|'skip', reason). Container check first (a converted
    sitecontainers app looks like 'code' to core.classify, so sidecar's own detector wins)."""
    if not rec.get("is_linux"):
        return "skip", "Windows App Service"
    if rec.get("is_function"):
        return "skip", "Function App"
    if side._is_container_candidate(rec):
        return "container", "Publish: Container -> sidecar path"
    reason = core.deployable_reason(rec)   # None iff deployable Linux code app
    if reason is None:
        return "code", "Publish: Code -> startup-command / LD_PRELOAD path"
    return "skip", reason


# ------------------------------------------------------------------- assess

def _assess(code_apps, container_apps, args):
    lines = ["DT Fleet — unified readiness", "=" * 72]
    lines.append(f"{len(code_apps)} code app(s) -> code path; "
                 f"{len(container_apps)} container app(s) -> sidecar path")
    lines.append("")
    if code_apps:
        core.enrich_parallel(code_apps)
        lines.append("CODE (startup-command / LD_PRELOAD path)")
        lines.append("-" * 50)
        for a in sorted(code_apps, key=lambda x: x["name"]):
            state = "instrumented" if a.get("dt_instrumented") else "not-instrumented"
            cmd = a.get("startup_command") or "(Oryx auto-detect)"
            lines.append(f"  {a['runtime']:14} {state:16} {a['subscription_name']}/{a['name']}")
            lines.append(f"  {'':14} startup: {cmd}")
    if container_apps:
        workers = max(1, args.parallel) if core.USE_REST else 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda a: side.sidecar_readiness(a, args.sidecar_name), container_apps))
        lines.append("")
        lines.append("CONTAINER (sidecar path)")
        lines.append("-" * 50)
        for a in sorted(container_apps, key=lambda x: x["name"]):
            sc = a.get("sidecar", {})
            lines.append(f"  {a.get('sidecar_status',''):17} {sc.get('container_mode',''):13} "
                         f"{a['subscription_name']}/{a['name']}")
            lines.append(f"  {'':17} {a.get('sidecar_reason','')}")
    print("\n" + "\n".join(lines))


# ------------------------------------------------------------------- deploy / uninstall / verify

def _run_code(code_apps, creds, args):
    """Deploy/verify/uninstall the code apps via the code path."""
    results = []
    if not code_apps:
        return results
    core.enrich_parallel(code_apps)
    if args.action == "deploy" and not args.force:
        before = len(code_apps)
        code_apps = [a for a in code_apps if not a.get("dt_instrumented")]
        if before - len(code_apps):
            log(f"[code] {before - len(code_apps)} already instrumented — skipped (use --force)")
    workers = max(1, args.parallel) if core.USE_REST else 1

    def one(a):
        if args.action == "deploy":
            res = core.deploy_one(a, creds, args)
        elif args.action == "uninstall":
            res = core.uninstall_one(a, args)
        else:
            res = {"name": a["name"], "status": "n/a", "detail": ""}
        if args.state_file:
            core.append_state(args.state_file, a, res)
        return a, res
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for a, res in ex.map(one, code_apps):
            log(f"[code] {res['name']} -> {res['status']}: {res['detail']}")
            results.append((a, res))
    return results


def _run_container(container_apps, creds, args):
    results = []
    if not container_apps:
        return results
    workers = max(1, args.parallel) if core.USE_REST else 1
    conn = None
    if args.action == "deploy":
        conn = side.dt_connection_info(creds, args.network_zone)
        args._resolved_version = side.resolve_oneagent_version(args.oneagent_version)
        log(f"[container] codemodules tag: {args._resolved_version}")
        if args.allow_convert and args.parallel > 1:
            log("[container] --allow-convert forces serial", err=True); workers = 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda a: side.sidecar_readiness(a, args.sidecar_name), container_apps))

    def one(a):
        if args.action == "deploy":
            res = side.sidecar_deploy_one(a, creds, conn, args)
        elif args.action == "uninstall":
            res = side.sidecar_uninstall_one(a, args)
        else:
            res = {"name": a["name"], "status": "n/a", "detail": ""}
        if args.state_file:
            core.append_state(args.state_file, a, res)
        return a, res
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for a, res in ex.map(one, container_apps):
            log(f"[container] {res['name']} -> {res['status']}: {res['detail']}")
            results.append((a, res))
    return results


def _verify_all(deployed_code, deployed_ctr, creds, args):
    poll = args.dt_timeout if (creds and not args.no_dt_check) else 0
    log(f"\n[verify] waiting {args.verify_wait}s for cold start + agent connect...")
    core.sleep_with_heartbeat(args.verify_wait, "verify wait")
    verdicts = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {}
        for a in deployed_code:
            futs[ex.submit(core.verify_one, a, (None if args.no_dt_check else creds), poll, not args.no_traffic)] = a
        for a in deployed_ctr:
            futs[ex.submit(side.sidecar_verify_one, a, (None if args.no_dt_check else creds), args)] = a
        for fut in concurrent.futures.as_completed(futs):
            a = futs[fut]; v = fut.result()
            log(f"    {v['verdict']:34}  {v['name']}: {v['detail']}")
            verdicts[a["name"]] = (a, v)
    if args.rollback_on_fail:
        log(f"\n[rollback] health-probing apps not already Dynatrace-confirmed...")
        rolled = 0
        for nm, (a, v) in verdicts.items():
            # A CONFIRMED (Dynatrace) verdict already proves the app served traced traffic —
            # don't second-guess it on a cold-start HTTP timeout. Only probe the rest.
            if v["verdict"].startswith("CONFIRMED") or core.app_http_ok(a):
                continue
            log(f"    [{nm}] NOT serving HTTP (verdict '{v['verdict']}') — rolling back...", err=True)
            rb = (side.sidecar_uninstall_one(a, args) if a.get("sidecar")
                  else core.uninstall_one(a, args))
            rolled += 1
            log(f"    [{nm}] rollback -> {rb['status']}: {rb['detail']}", err=True)
        log(f"[rollback] {rolled}/{len(verdicts)} restored (were not serving HTTP); rest healthy.")


# ------------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(prog="dt_appservice.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", nargs="?", default="assess",
                   choices=["assess", "deploy", "verify", "uninstall", "update"])
    # scope / client
    p.add_argument("--subscription", "--subscription-filter", dest="subscription", action="append", metavar="GUID|NAME")
    p.add_argument("--all-subscriptions", action="store_true")
    p.add_argument("--resource-group")
    p.add_argument("--app-service-name", "--name-filter", dest="app_service_name", action="append", metavar="NAME")
    p.add_argument("--use-arm", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--rest", action="store_true")
    p.add_argument("--no-token-cache", action="store_true")
    p.add_argument("--auth", choices=["auto", "az", "managed-identity", "service-principal"], default="auto")
    p.add_argument("--creds", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "creds.json"))
    p.add_argument("--parallel", type=int, default=1, metavar="N")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--what-if", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--state-file")
    p.add_argument("--ntfy-topic")
    # tagging / naming (shared)
    p.add_argument("--env-tag", default="demo")
    p.add_argument("--set-host-tag", action="append", metavar="KEY=VALUE")
    p.add_argument("--restart-wait", type=int, default=45)
    # verify (shared)
    p.add_argument("--verify", action="store_true")
    p.add_argument("--verify-wait", type=int, default=60)
    p.add_argument("--dt-timeout", type=int, default=180)
    p.add_argument("--no-dt-check", action="store_true")
    p.add_argument("--no-traffic", action="store_true")
    p.add_argument("--rollback-on-fail", action="store_true")
    # code-path flags
    p.add_argument("--dt-include"); p.add_argument("--dt-flavor", default="default")
    p.add_argument("--start-app-cmd"); p.add_argument("--discover-start-cmd", action="store_true")
    p.add_argument("--preserve-oryx", action="store_true")
    p.add_argument("--leave-startup-command", action="store_true")
    p.add_argument("--resilient", action="store_true")
    p.add_argument("--service-name"); p.add_argument("--otel-resource-attributes"); p.add_argument("--host-name")
    # container/sidecar flags
    p.add_argument("--sidecar-name", default=side.DEFAULT_SIDECAR_NAME)
    p.add_argument("--oneagent-version"); p.add_argument("--image-registry")
    p.add_argument("--registry-auth", default="anonymous")
    p.add_argument("--technology"); p.add_argument("--technology-map")
    p.add_argument("--network-zone")
    p.add_argument("--sidecar-wait", type=int, default=180); p.add_argument("--no-sidecar-wait", action="store_true")
    p.add_argument("--enable-storage", action="store_true"); p.add_argument("--allow-alpine", action="store_true")
    p.add_argument("--disable-app-insights", action="store_true")
    p.add_argument("--allow-convert", action="store_true"); p.add_argument("--revert-conversion", action="store_true")
    p.add_argument("--target-port", help="container port for a converted main site-container (if the legacy app has no WEBSITES_PORT)")
    p.add_argument("--include-slots", action="store_true"); p.add_argument("--prune-old-versions", action="store_true")
    p.add_argument("--keyvault-ref", metavar="VAULT")
    return p


def main():
    args = build_parser().parse_args()
    core.VERBOSE = args.verbose
    core.USE_REST = args.rest
    core.NO_TOKEN_CACHE = args.no_token_cache
    core.AUTH_PREF = args.auth

    subs = core.resolve_subscriptions(args)
    records = core.build_app_records(args, subs)
    code_apps, container_apps, skipped = [], [], []
    for r in records:
        route, reason = route_of(r)
        r["_route"], r["_route_reason"] = route, reason
        (code_apps if route == "code" else container_apps if route == "container" else skipped).append(r)
    log(f"[route] {len(records)} app(s): {len(code_apps)} code, {len(container_apps)} container, "
        f"{len(skipped)} skipped")

    if args.action == "update":
        log("[update] applies to sidecar (container) apps only...")
        return _run_update(container_apps, args)

    if args.action == "assess":
        _assess(code_apps, container_apps, args)
        return

    creds = None
    if args.action in ("deploy", "verify"):
        creds = core.load_creds(args.creds, require_paas=(args.action == "deploy"))

    if args.action == "verify":
        if code_apps:
            core.enrich_parallel(code_apps)
        workers = max(1, args.parallel) if core.USE_REST else 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda a: side.sidecar_readiness(a, args.sidecar_name), container_apps))
        _verify_all([a for a in code_apps if a.get("dt_instrumented")] or code_apps,
                    [a for a in container_apps if a.get("dt_instrumented")] or container_apps,
                    creds, args)
        return

    # deploy / uninstall
    if (code_apps or container_apps) and not args.yes and not args.what_if:
        if input(f"\nProceed to {args.action} {len(code_apps)} code + {len(container_apps)} "
                 f"container app(s)? [y/N] ").strip().lower() != "y":
            raise SystemExit("aborted")
    code_res = _run_code(code_apps, creds, args)
    ctr_res = _run_container(container_apps, creds, args)

    if args.action == "deploy" and args.verify and not args.what_if:
        dc = [a for a, x in code_res if x["status"] == "deployed"]
        dk = [a for a, x in ctr_res if x["status"] == "deployed"]
        if dc or dk:
            _verify_all(dc, dk, creds, args)

    ok = sum(1 for _, x in code_res + ctr_res
             if x["status"] in ("deployed", "uninstalled", "would-deploy", "would-uninstall"))
    log(f"\n[done] {args.action}: {ok}/{len(code_res) + len(ctr_res)} ok")
    if args.ntfy_topic:
        core.notify(args.ntfy_topic, f"DT Fleet (unified) — {args.action}", f"{ok} ok.", tags="white_check_mark")


def _run_update(container_apps, args):
    if not container_apps:
        log("[update] no container apps in scope.")
        return
    core.USE_REST = args.rest
    args._resolved_version = side.resolve_oneagent_version(args.oneagent_version)
    log(f"[update] codemodules tag: {args._resolved_version}")
    workers = max(1, args.parallel) if core.USE_REST else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda a: side.sidecar_readiness(a, args.sidecar_name), container_apps))
    if not args.yes and not args.what_if:
        if input(f"\nProceed to update {len(container_apps)} container app(s)? [y/N] ").strip().lower() != "y":
            raise SystemExit("aborted")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for v in ex.map(lambda a: side.sidecar_update_one(a, args), container_apps):
            log(f"[update] {v['name']} -> {v['status']}: {v['detail']}")


if __name__ == "__main__":
    main()
