#!/usr/bin/env python3
"""Runs every query of grafana/dashboard.json against VictoriaMetrics and reports empty results or errors.

Usage: python3 scripts/check-queries.py [http://localhost:8428] [--tz Europe/Berlin]
Grafana variables are substituted roughly the way Grafana would (range = 1 day, 30 days for long panels).
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("url", nargs="?", default="http://localhost:8428")
ap.add_argument("--tz", default="Europe/Berlin")
ap.add_argument("--price-buy", default="0.30")
ap.add_argument("--price-sell", default="0.08")
ap.add_argument("--time", type=int, default=int(time.time()), help="evaluation time (unix seconds)")
a = ap.parse_args()

here = os.path.dirname(os.path.abspath(__file__))
dash = json.load(open(os.path.join(here, "..", "grafana", "dashboard.json")))
subst = {"${tz}": a.tz, "${price_buy:raw}": a.price_buy, "${price_sell:raw}": a.price_sell}

def panels(ps):
    for p in ps:
        yield p
        yield from panels(p.get("panels", []))

bad = total = 0
for p in panels(dash["panels"]):
    for t in p.get("targets", []):
        if "expr" not in t:
            continue
        total += 1
        e = t["expr"]
        rng = 30 * 86400 if p.get("timeFrom", "").endswith(("M", "y")) else 86400
        for k, v in subst.items():
            e = e.replace(k, v)
        e = e.replace("$__range_s", str(rng)).replace("$__range", f"{rng}s").replace("$__interval", "5m")
        if t.get("instant"):
            url = f"{a.url}/api/v1/query?" + urllib.parse.urlencode({"query": e, "time": a.time})
        else:
            step = 86400 if t.get("interval") == "1d" else 300
            start = (a.time - 7 * 86400) // step * step
            url = f"{a.url}/api/v1/query_range?" + urllib.parse.urlencode(
                {"query": e, "start": start, "end": a.time, "step": step})
        label = f"[{p['id']}] {p.get('title') or '-'} / {t.get('legendFormat', t['refId'])}"
        try:
            t0 = time.time()
            res = json.load(urllib.request.urlopen(url, timeout=120))["data"]["result"]
            dur = time.time() - t0
            if not res:
                print(f"EMPTY  {label}")
                bad += 1
            else:
                v = res[0].get("value") or res[0]["values"][-1]
                print(f"ok     {label}: {float(v[1]):.4g}  ({dur:.2f}s)")
        except urllib.error.HTTPError as ex:
            print(f"ERROR  {label}: {ex.read().decode()[:300]}")
            bad += 1
print(f"\n{total} queries, {bad} problems")
sys.exit(1 if bad else 0)
