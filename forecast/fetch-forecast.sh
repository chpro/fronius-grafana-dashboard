#!/bin/sh
# Fetches the PV forecast from forecast.solar and imports it into VictoriaMetrics
# as pv_forecast_watthours_value (Wh accumulated per day, timestamps in the future).
#
# Needs only busybox/coreutils (wget, tr, grep, wc, head, tail, cut). Run every 15 minutes or less often
# (forecast.solar's free tier allows 12 requests per hour).
#
# Environment:
#   FORECAST_URL  https://api.forecast.solar/estimate/watthours/<lat>/<lon>/<dec>/<az>/<kwp>?time=utc
#                 (use "watthours", not "watthours/day": the dashboard needs the accumulated values)
#                 (with API key: https://api.forecast.solar/<key>/estimate/...)
#   VM_URL        VictoriaMetrics base URL, default http://victoriametrics:8428
#
# VictoriaMetrics must accept future timestamps: start it with -futureRetention=7d (or more).
set -eu
: "${FORECAST_URL:?FORECAST_URL is not set}"
VM_URL="${VM_URL:-http://victoriametrics:8428}"
tmp="${TMPDIR:-/tmp}"
raw="$tmp/forecast.csv"
data="$tmp/import.csv"

# CSV is only returned with the Accept header (a format=csv parameter is ignored)
wget -q -T 30 -O "$raw" --header "Accept: text/csv" "$FORECAST_URL"

# keep data rows only, ";" -> ","; format: <RFC3339 time>,<Wh>
tr ';' ',' < "$raw" | grep -E '^[0-9]{4}-' > "$data" || true
rows=$(wc -l < "$data")

# on errors print the message only, not the whole response (the URL may contain an API key)
[ "$rows" -gt 0 ] || { echo "no forecast rows received: $(grep -o '"text":"[^"]*"' "$raw" || head -c 200 "$raw")" >&2; exit 1; }

wget -q -T 30 -O - --post-file="$data" \
  "$VM_URL/api/v1/import/csv?format=1:time:rfc3339,2:metric:pv_forecast_watthours_value&extra_label=job=pv-forecast"
echo "forecast imported: $rows values ($(head -1 "$data" | cut -d, -f1) to $(tail -1 "$data" | cut -d, -f1))"
