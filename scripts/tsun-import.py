#!/usr/bin/env python3
"""Converts the history of a TSUN micro inverter (talent-monitoring.com) into VictoriaMetrics import data.

The TSUN portal has no official API. Its chart export (`exportChartData`) returns an xlsx file with the
5 minute values of a device; this script downloads it week by week, integrates the AC output power to an
energy counter and writes a file for VictoriaMetrics' /api/v1/import:

  balcony_apower{source="tsun"}             AC output in W, negative like a Shelly plug sees feed-in
  balcony_ret_aenergy_total{source="tsun"}  energy fed in since --since in Wh

The dashboard sums all balcony_* series, so the imported history continues seamlessly with the Shelly data
as long as --until is the time the Shelly data starts (no overlap, nothing counted twice).

Usage:
  read -rs TSUN_TOKEN && export TSUN_TOKEN    # Authorization header of a portal request (browser dev tools)
  python3 scripts/tsun-import.py --device <deviceGuid> --since 2023-04-30 --until 2026-11-03T12:10:00Z
  curl -X POST --data-binary @tsun-import.jsonl http://localhost:8428/api/v1/import

Downloaded blocks are kept in --cache, a second run only fetches what is missing. The script requests UTC
(timezone=+00:00): days and timestamps of the export are then UTC, so daylight saving time does not matter.
"""
import argparse, datetime as dt, io, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request, zipfile

ap = argparse.ArgumentParser()
ap.add_argument("--device", required=True, help="deviceGuid from the exportChartData URL")
ap.add_argument("--since", required=True, help="first day, YYYY-MM-DD")
ap.add_argument("--until", required=True, help="end (exclusive), ISO time with zone, e.g. 2026-11-03T12:10:00Z")
ap.add_argument("--cache", default="tsun-cache")
ap.add_argument("--out", default="tsun-import.jsonl")
ap.add_argument("--days", type=int, default=7, help="days per request, smaller if the portal times out")
ap.add_argument("--max-gap", type=int, default=900, help="seconds; longer gaps between samples add no energy")
a = ap.parse_args()

API = "https://www.talent-monitoring.com/prod-api/df/exportChartData"
tz = dt.timezone.utc  # the export uses the timezone of the request
until = dt.datetime.fromisoformat(a.until.replace("Z", "+00:00"))
os.makedirs(a.cache, exist_ok=True)


class PortalError(Exception):
    pass


def get(url, token, what):
    req = urllib.request.Request(url, headers={"Authorization": token, "User-Agent": "Mozilla/5.0"})
    for attempt in range(4):  # the portal answers 502/504 now and then, especially for older data
        try:
            return urllib.request.urlopen(req, timeout=180).read()
        except urllib.error.HTTPError as ex:
            if ex.code < 500:
                sys.exit(f"{what}: HTTP {ex.code}: {ex.read()[:300].decode(errors='replace')}")
            if attempt == 3:
                raise PortalError(f"{what}: HTTP {ex.code}")
            print(f"{what}: HTTP {ex.code}, retrying in {30 * (attempt + 1)}s", file=sys.stderr)
            time.sleep(30 * (attempt + 1))


def download(first, last):
    """xlsx files of the block; a block that keeps failing is fetched day by day."""
    path = os.path.join(a.cache, f"{first}_{last}.xlsx")
    if os.path.exists(path):
        return [open(path, "rb").read()]
    days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
    if len(days) > 1 and all(os.path.exists(os.path.join(a.cache, f"{d}_{d}.xlsx")) for d in days):
        return [b for d in days for b in download(d, d)]
    try:
        return [fetch(first, last, path)]
    except PortalError as ex:
        if len(days) == 1:
            sys.exit(f"{ex}, giving up; run again later or with another --days")
        print(f"{ex}, fetching day by day", file=sys.stderr)
        return [b for d in days for b in download(d, d)]


def fetch(first, last, path):
    token = os.environ.get("TSUN_TOKEN") or sys.exit("TSUN_TOKEN is not set")
    if not token.lower().startswith("bearer "):
        token = "Bearer " + token
    what = f"{first}~{last}"
    q = urllib.parse.urlencode({"deviceGuid": a.device, "group": "hour", "date": what,
                                "series": "phaseA_active_power", "timezone": "+00:00"})
    body = get(f"{API}?{q}", token, what)
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
        body = get(API.split("/df/")[0] + file, token, what)
        if not body.startswith(b"PK"):
            sys.exit(f"{what}: {file} is no xlsx: {body[:300].decode(errors='replace')}")
    open(path, "wb").write(body)
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
day = dt.date.fromisoformat(a.since)
while dt.datetime.combine(day, dt.time(), tz) < until:
    last = day + dt.timedelta(days=a.days - 1)
    n = 0
    for ts, p in (r for x in download(day, last) for r in rows(x)):
        t = dt.datetime.strptime(ts[:16], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        if t < until:
            samples[int(t.timestamp())] = p
            n += 1
    print(f"{day} – {last}: {n} samples", file=sys.stderr)
    day = last + dt.timedelta(days=1)

ts = sorted(samples)
if not ts:
    sys.exit("no data")
# counter: one value per sample, plus every 30 minutes during gaps (nights), so increase() always finds a
# recent sample before its window
energy, counter, gaps = 0.0, [], 0
for i, t in enumerate(ts):
    if i:
        prev = ts[i - 1]
        if t - prev <= a.max_gap:
            energy += (samples[prev] + samples[t]) / 2 * (t - prev) / 3600
        else:
            gaps += 1
            counter += [(f, round(energy, 2)) for f in range(prev + 1800, t, 1800)]
    counter.append((t, round(energy, 2)))

with open(a.out, "w") as f:
    for name, series in (("balcony_apower", [(t, -samples[t]) for t in ts]), ("balcony_ret_aenergy_total", counter)):
        for i in range(0, len(series), 10000):
            chunk = series[i:i + 10000]
            f.write(json.dumps({"metric": {"__name__": name, "source": "tsun"}, "values": [v for _, v in chunk],
                                "timestamps": [t * 1000 for t, _ in chunk]}) + "\n")
fmt = lambda t: dt.datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d %H:%M")
print(f"{len(ts)} samples {fmt(ts[0])} – {fmt(ts[-1])}, {energy / 1000:.1f} kWh, {gaps} gaps > {a.max_gap}s"
      f" -> {a.out}", file=sys.stderr)
