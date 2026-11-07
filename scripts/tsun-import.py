#!/usr/bin/env python3
"""Converts the history of a TSUN micro inverter (talent-monitoring.com) into VictoriaMetrics import data.

The TSUN portal has no official API. This script uses what its web page uses:

- the chart export (`exportChartData`, xlsx) with the 5 minute AC power, downloaded week by week. The portal
  keeps these only for a limited time (here from late September 2023 on).
- the daily yield of every day (`selectChartData`, group=day, one request per month), available since the
  start of the plant.

Days with 5 minute values keep their power curve; the counter is scaled up by the few percent the daily yield is
higher, and if more is missing, the rest of the daily yield goes to the hours without values, spread like a typical
solar day. Days with only a daily yield get it spread over the whole day that way. The result is written
for VictoriaMetrics' /api/v1/import:

  balcony_apower{source="tsun"}             AC output in W, negative like a Shelly plug sees feed-in
  balcony_ret_aenergy_total{source="tsun"}  energy fed in since --since in Wh

The dashboard sums all balcony_* series, so the imported history continues seamlessly with the Shelly data
as long as --until is the time the Shelly data starts (no overlap, nothing counted twice).

Usage:
  read -rs TSUN_TOKEN && export TSUN_TOKEN    # Authorization header of a portal request (browser dev tools)
  python3 scripts/tsun-import.py --device <deviceGuid> --since 2023-04-30 --until 2026-11-03T12:10:00Z
  curl -X POST --data-binary @tsun-import.jsonl http://localhost:8428/api/v1/import

The portal API is unofficial and may change. Not affiliated with or endorsed by TSUN. Use it with your own
account and data only.

Downloaded blocks are kept in --cache, a second run only fetches what is missing. The export is requested
in UTC (timezone=+00:00), its timestamps are UTC then. Daily yields are per local calendar day (--tz).
"""
import argparse, datetime as dt, http.client, io, json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request, zipfile
from zoneinfo import ZoneInfo

ap = argparse.ArgumentParser()
ap.add_argument("--device", required=True, help="deviceGuid from the exportChartData URL")
ap.add_argument("--since", required=True, help="first day, YYYY-MM-DD")
ap.add_argument("--tz", default="Europe/Berlin", help="timezone of the plant, for the daily yields")
ap.add_argument("--until", required=True, help="end (exclusive), ISO time with zone, e.g. 2026-11-03T12:10:00Z")
ap.add_argument("--cache", default="tsun-cache")
ap.add_argument("--out", default="tsun-import.jsonl")
ap.add_argument("--days", type=int, default=1, help="days per request; larger blocks often time out (HTTP 504)")
ap.add_argument("--max-gap", type=int, default=900, help="seconds; longer gaps between samples add no energy")
a = ap.parse_args()

BASE = "https://www.talent-monitoring.com/prod-api"
API = BASE + "/df/exportChartData"
tz = dt.timezone.utc  # the export uses the timezone of the request
local = ZoneInfo(a.tz)
until = dt.datetime.fromisoformat(a.until.replace("Z", "+00:00"))
os.makedirs(a.cache, exist_ok=True)


class PortalError(Exception):
    pass


def get(url, token, what):
    req = urllib.request.Request(url, headers={"Authorization": token, "User-Agent": "Mozilla/5.0"})
    for attempt in range(6):  # the portal answers 502/504 now and then, a quick retry usually succeeds
        try:
            return urllib.request.urlopen(req, timeout=180).read()
        except urllib.error.HTTPError as ex:
            if ex.code < 500:
                sys.exit(f"{what}: HTTP {ex.code}: {ex.read()[:300].decode(errors='replace')}")
            error = f"HTTP {ex.code}"
        except (OSError, http.client.HTTPException) as ex:  # timeouts, lost connection (e.g. after standby)
            error = str(ex) or type(ex).__name__
        if attempt == 5:
            raise PortalError(f"{what}: {error}")
        print(f"{what}: {error}, retrying in 10s", file=sys.stderr)
        time.sleep(10)


def token():
    t = os.environ.get("TSUN_TOKEN") or sys.exit("TSUN_TOKEN is not set")
    return t if t.lower().startswith("bearer ") else "Bearer " + t


def daily(month):
    """{local date: Wh} of one month."""
    path = os.path.join(a.cache, f"daily-{month:%Y-%m}.json")
    if os.path.exists(path):
        body = open(path, "rb").read()
    else:
        offset = dt.datetime.combine(month.replace(day=15), dt.time(12), local).strftime("%z")
        q = urllib.parse.urlencode({"deviceGuid": a.device, "group": "day", "date": f"{month}",
                                    "series": "energy_today", "timezone": offset[:3] + ":" + offset[3:]})
        body = get(f"{BASE}/df/selectChartData?{q}", token(), f"daily {month:%Y-%m}")
        try:
            answer = json.loads(body)
        except ValueError:
            answer = {}
        if answer.get("code") != 200 or not isinstance(answer.get("data"), dict):
            sys.exit(f"daily {month:%Y-%m}: unexpected answer: {body[:300].decode(errors='replace')}")
        if answer["data"].get("chart"):
            open(path, "wb").write(body)
        time.sleep(1)
    chart = json.loads(body)["data"].get("chart") or []
    return {dt.date(c["year"], c["month"], c["day"]): float(c["energyToday"]) for c in chart
            if c.get("energyToday") is not None}


# cached exports by the days they cover, so runs with another --since or --days reuse them
cached = {}
for name in os.listdir(a.cache):
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d)_(\d{4}-\d\d-\d\d)\.xlsx", name)
    if m:
        cached[name] = tuple(dt.date.fromisoformat(x) for x in m.groups())


def download(first, last):
    """xlsx files of the block; a block that keeps failing is fetched day by day."""
    try:
        return [fetch(first, last, os.path.join(a.cache, f"{first}_{last}.xlsx"))]
    except PortalError as ex:
        if first == last:
            sys.exit(f"{ex}, giving up; run again later or with another --days")
        print(f"{ex}, fetching day by day", file=sys.stderr)
        days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
        return [b for d in days for b in download(d, d)]


def fetch(first, last, path):
    what = f"{first}~{last}"
    q = urllib.parse.urlencode({"deviceGuid": a.device, "group": "hour", "date": what,
                                "series": "phaseA_active_power", "timezone": "+00:00"})
    body = get(f"{API}?{q}", token(), what)
    if not body.startswith(b"PK"):
        # the portal writes the xlsx to a file and answers with its path:
        # {"code":200,"data":"/profile/upload/chart-report/…/chart-….xlsx"}; errors are JSON as well
        try:
            answer = json.loads(body)
        except ValueError:
            answer = {}
        file = answer.get("data") if answer.get("code") == 200 else None
        if not isinstance(file, str) or not file.endswith(".xlsx"):
            sys.exit(f"{what}: unexpected answer: {body[:300].decode(errors='replace')}")
        body = get(BASE + file, token(), what)
        if not body.startswith(b"PK"):
            sys.exit(f"{what}: {file} is no xlsx: {body[:300].decode(errors='replace')}")
    open(path, "wb").write(body)  # empty answers too: old 5 minute data is deleted by the portal
    cached[os.path.basename(path)] = (first, last)
    time.sleep(1)
    return body


def rows(xlsx):
    """(local time string, AC power) of the first sheet, header skipped."""
    z = zipfile.ZipFile(io.BytesIO(xlsx))
    names = z.namelist()
    strings = []
    if "xl/sharedStrings.xml" in names:
        for si in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode(), re.S):
            strings.append("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    for row in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S)[1:]:
        vals = []
        for attrs, inner in re.findall(r"<c ([^>]*?)(?:/>|>(.*?)</c>)", row, re.S):
            v = re.search(r"<v>(.*?)</v>", inner or "") or re.search(r"<t[^>]*>(.*?)</t>", inner or "")
            v = v.group(1) if v else ""
            vals.append(strings[int(v)] if 't="s"' in attrs and v else v)
        if len(vals) >= 2 and vals[0] and vals[1]:
            yield vals[0], float(vals[1])


samples = {}
begin = dt.datetime.combine(dt.date.fromisoformat(a.since), dt.time(), local)
day = dt.date.fromisoformat(a.since)
while dt.datetime.combine(day, dt.time(), tz) < until:
    cover = [(e, n) for n, (s, e) in cached.items() if s <= day <= e]
    if cover:
        last, name = max(cover)
        files = [open(os.path.join(a.cache, name), "rb").read()]
    else:
        last = min([day + dt.timedelta(days=a.days - 1)] +
                   [s - dt.timedelta(days=1) for s, _ in cached.values() if s > day])
        files = download(day, last)
    n = 0
    for ts, p in (r for x in files for r in rows(x)):
        t = dt.datetime.strptime(ts[:16], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        if begin <= t < until:
            samples[int(t.timestamp())] = p
            n += 1
    print(f"{day} – {last}: {n} samples{' (cached)' if cover else ''}", file=sys.stderr)
    day = last + dt.timedelta(days=1)

yields = {}
month = dt.date.fromisoformat(a.since).replace(day=1)
while dt.datetime.combine(month, dt.time(), local) < until:
    y = daily(month)
    yields.update(y)
    print(f"{month:%Y-%m}: {len(y)} daily yields, {sum(y.values()) / 1000:.1f} kWh", file=sys.stderr)
    month = (month + dt.timedelta(days=32)).replace(day=1)

ts = sorted(samples)
if not ts and not yields:
    sys.exit("no data")
by_day = {}
for t in ts:
    by_day.setdefault(dt.datetime.fromtimestamp(t, local).date(), []).append(t)

# energy increments (time, Wh) per local day
SLOT = 900  # a typical solar day: a half sine between 6:00 and 20:00 in 15 minute slots
SUN = [(6 * 3600 + i * SLOT, math.sin(math.pi * (i + 0.5) / 56)) for i in range(56)]


def sun(day, wh, covered=()):
    """wh spread like a typical solar day over the slots not in covered, one step at the end of each slot"""
    midnight = int(dt.datetime.combine(day, dt.time(), local).timestamp())
    free = [(midnight + s, w) for s, w in SUN if not any(p < midnight + s + SLOT and t > midnight + s
                                                          for p, t in covered)]
    weight = sum(w for _, w in free)
    return [(s + SLOT, wh * w / weight) for s, w in free]


steps = []
n_fine = n_scaled = n_filled = n_daily = 0
wh_fine = wh_total = wh_daily = 0.0  # 5 minute values, the same days with the daily yield, daily yield only
day, last_day = dt.date.fromisoformat(a.since), until.astimezone(local).date()
while day <= last_day:
    times = by_day.get(day, [])
    inc = [(t, (samples[p] + samples[t]) / 2 * (t - p) / 3600 if t - p <= a.max_gap else 0.0)
           for p, t in zip(times, times[1:])]
    fine = sum(e for _, e in inc)
    total = yields.get(day) if day != last_day else None  # the day of --until is only partly before it
    covered = [(p, t) for p, t in zip(times, times[1:]) if t - p <= a.max_gap]
    if fine > 0:
        wh_fine += fine
        n_fine += 1
        rest = sun(day, total - fine, covered) if total and total > 1.1 * fine else []
        if rest:
            # values missing for part of the day: the rest of the daily yield goes to the time without values
            steps += inc + rest
            n_filled += 1
            wh_total += total
        else:
            # the portal's daily yield is a few percent above the 5 minute values
            factor = max(1.0, total / fine) if total else 1.0
            steps += [(t, e * factor) for t, e in inc]
            n_scaled += factor > 1.02
            wh_total += fine * factor
    elif total:
        steps += sun(day, total)
        n_daily += 1
        wh_daily += total
    day += dt.timedelta(days=1)

# counter: one value per step, plus every 30 minutes in between (nights, days without data), so increase()
# always finds a recent sample before its window
start = int(dt.datetime.combine(dt.date.fromisoformat(a.since), dt.time(), local).timestamp())
end = int(until.timestamp()) - 1
energy, counter, prev = 0.0, [(start, 0.0)], start
for t, e in sorted(steps) + [(end, 0.0)]:
    counter += [(f, round(energy, 2)) for f in range(prev + 1800, t, 1800)]
    energy += e
    if t == counter[-1][0]:
        counter.pop()
    counter.append((t, round(energy, 2)))
    prev = t

with open(a.out, "w") as f:
    for name, series in (("balcony_apower", [(t, -samples[t]) for t in ts]), ("balcony_ret_aenergy_total", counter)):
        for i in range(0, len(series), 10000):
            chunk = series[i:i + 10000]
            f.write(json.dumps({"metric": {"__name__": name, "source": "tsun"}, "values": [v for _, v in chunk],
                                "timestamps": [t * 1000 for t, _ in chunk]}) + "\n")
print(f"{n_fine} days with 5 minute values: {wh_fine / 1000:.1f} kWh, with the daily yield {wh_total / 1000:.1f} kWh "
      f"({n_scaled} scaled up, {n_filled} partly filled); {n_daily} days with the daily yield only: "
      f"{wh_daily / 1000:.1f} kWh; total {energy / 1000:.1f} kWh -> {a.out}", file=sys.stderr)
