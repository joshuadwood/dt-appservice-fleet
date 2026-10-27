#!/usr/bin/env python3
"""
generate_load.py — drive CRUD traffic against the demo App Services so the
Dynatrace OneAgent has request activity to trace.

Targets are discovered from the resource group (default) or passed explicitly.
Each worker loops a full CRUD cycle (POST -> GET list -> GET one -> PUT -> GET ->
DELETE) plus health checks, at a configurable rate, for a configurable duration.

Examples:
  # discover apps in the demo RG and drive load for 10 minutes
  python3 generate_load.py --resource-group dt-oneagent-fleet-demo --duration 600

  # explicit targets
  python3 generate_load.py --url https://app1.azurewebsites.net --url https://app2.azurewebsites.net
"""
import argparse
import concurrent.futures
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request


def discover(resource_group):
    out = subprocess.run(
        ["az", "webapp", "list", "-g", resource_group,
         "--query", "[].defaultHostName", "-o", "json"],
        capture_output=True, text=True,
    )
    hosts = json.loads(out.stdout or "[]")
    return [f"https://{h}" for h in hosts]


def req(method, url, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            resp.read()
            return resp.status, (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, (time.time() - t0) * 1000
    except Exception:  # noqa: BLE001
        return 0, (time.time() - t0) * 1000


def crud_cycle(base, stats):
    def record(code, ms):
        stats["n"] += 1
        stats["ms"] += ms
        if code and 200 <= code < 400:
            stats["ok"] += 1
        else:
            stats["err"] += 1

    record(*req("GET", f"{base}/health"))
    code, ms = req("POST", f"{base}/items", {"name": f"item-{random.randint(1, 1_000_000)}"})
    record(code, ms)
    item_id = None
    # read id from create response for the full cycle
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"{base}/items", method="GET"), timeout=15) as resp:
            items = json.loads(resp.read() or "[]")
            if items:
                item_id = items[-1]["id"]
    except Exception:  # noqa: BLE001
        pass
    record(*req("GET", f"{base}/items"))
    if item_id is not None:
        record(*req("GET", f"{base}/items/{item_id}"))
        record(*req("PUT", f"{base}/items/{item_id}", {"name": "updated"}))
        record(*req("GET", f"{base}/items/{item_id}"))
        record(*req("DELETE", f"{base}/items/{item_id}"))


def worker(base, deadline, rate, stats):
    interval = 1.0 / rate if rate > 0 else 0
    while time.time() < deadline:
        start = time.time()
        crud_cycle(base, stats)
        if interval:
            sleep = interval - (time.time() - start)
            if sleep > 0:
                time.sleep(sleep)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", action="append", default=[], help="target base URL (repeatable)")
    p.add_argument("--resource-group", help="discover targets from this RG")
    p.add_argument("--duration", type=int, default=300, help="seconds to run (default 300)")
    p.add_argument("--rate", type=float, default=1.0, help="CRUD cycles/sec per target")
    p.add_argument("--concurrency", type=int, default=2, help="workers per target")
    args = p.parse_args()

    targets = list(args.url)
    if args.resource_group:
        targets += discover(args.resource_group)
    targets = sorted(set(targets))
    if not targets:
        sys.exit("no targets — pass --url or --resource-group")

    print(f"[load] {len(targets)} target(s), {args.concurrency} worker(s) each, "
          f"{args.rate}/s, {args.duration}s")
    for t in targets:
        print(f"    {t}")

    deadline = time.time() + args.duration
    stats = {t: {"n": 0, "ok": 0, "err": 0, "ms": 0.0} for t in targets}
    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets) * args.concurrency) as ex:
        for t in targets:
            for _ in range(args.concurrency):
                tasks.append(ex.submit(worker, t, deadline, args.rate, stats[t]))
        # progress ticker
        while time.time() < deadline:
            time.sleep(min(15, max(1, args.duration // 10)))
            done = int(time.time() - (deadline - args.duration))
            total = sum(s["n"] for s in stats.values())
            print(f"[load] t+{done}s  requests={total}", flush=True)
            if all(f.done() for f in tasks):
                break
        concurrent.futures.wait(tasks)

    print("\n[load] summary")
    for t in targets:
        s = stats[t]
        avg = s["ms"] / s["n"] if s["n"] else 0
        print(f"    {t}\n        requests={s['n']} ok={s['ok']} err={s['err']} avg={avg:.0f}ms")


if __name__ == "__main__":
    main()
