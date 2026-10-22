# Migrating from Telegraf + InfluxDB (original project) to VictoriaMetrics

If you run the [original dashboard](https://github.com/chpro/fronius-grafana-dashboard) with Telegraf and
InfluxDB 2, you can keep your complete history. The json-exporter config produces **the same metric
names** that Telegraf wrote to InfluxDB (`<measurement>_<field>`, e.g. `powerflow_P_PV`,
`smartmeter_EnergyReal_WAC_Plus_Absolute`), so imported history and new data form one continuous series.

| | before | after |
|---|---|---|
| Fronius Solar API | Telegraf `inputs.http` → InfluxDB | json-exporter, scraped by VictoriaMetrics |
| forecast.solar | Telegraf → InfluxDB | `forecast/fetch-forecast.sh` (CSV import) |
| History | InfluxDB buckets | one-time import via line protocol |

Labels differ between old and new data (old: `host`, `url`, `influxdb_bucket`; new: `job`, `module`,
`instance`). The dashboard therefore only selects by metric name and aggregates with `max()`/`sum()`.

## 1. Start the new collectors

Deploy json-exporter and the scrape config (Docker Compose or Kubernetes, see README) **while Telegraf is
still running**. Check that data arrives:

```bash
curl -sG http://localhost:8428/api/v1/query --data-urlencode 'query=up{job="fronius"}'
curl -sG http://localhost:8428/api/v1/query --data-urlencode 'query=powerflow_P_PV'
```

Write down the current time as `T0`. This is where the InfluxDB history will end:

```bash
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "$T0"
```

## 2. Stop Telegraf

```bash
docker compose stop telegraf   # or however you run it
```

## 3. Export the history from InfluxDB

`influxd inspect export-lp` reads the storage engine directly, no token needed. Look up the bucket IDs
in the InfluxDB UI (*Load Data → Buckets*) or with `influx bucket list`. The original project uses the
buckets `inverter`, `pvforecast` and `energyprices`; adjust to your setup.

```bash
T0=2026-01-01T00:00:00Z   # value from step 1
for b in inverter:<bucket-id> pvforecast:<bucket-id>; do
  name=${b%%:*}; id=${b##*:}
  docker exec influxdb influxd inspect export-lp \
    --bucket-id "$id" --engine-path /var/lib/influxdb2/engine \
    --end "$T0" --compress --output-path "/tmp/$name.lp.gz"
  docker cp "influxdb:/tmp/$name.lp.gz" "./$name.lp.gz"
  docker exec influxdb rm -f "/tmp/$name.lp.gz"
done
```

## 4. Import into VictoriaMetrics

VictoriaMetrics accepts InfluxDB line protocol on `/write` and converts `measurement,tags field=value`
into `measurement_field{tags}`. Import in chunks to stay below request size limits. The extra label
`source=influxdb` marks imported data, so it can be removed again if something goes wrong
(`/api/v1/admin/tsdb/delete_series?match[]={source="influxdb"}`).

```bash
VM=http://localhost:8428
for f in *.lp.gz; do
  echo "== $f"; rm -rf chunks && mkdir chunks
  gzip -dc "$f" | split -l 100000 - chunks/part_
  for c in chunks/part_*; do
    curl -sf --data-binary @"$c" "$VM/write?extra_label=source=influxdb" || { echo "failed: $c"; break; }
  done
done
rm -rf chunks
curl -s "$VM/internal/force_flush"
```

String fields (e.g. `InverterState`) are dropped by VictoriaMetrics; the dashboard does not use them.

If VictoriaMetrics runs with little memory, the import can be OOM-killed. Give it 2 GiB for the import.

**Forecast history:** the original Telegraf config stores the forecast as `pv_forecast_watt_hours_day`.
The dashboard uses `pv_forecast_watthours_value` (accumulated Wh from `estimate/watthours`). Old
forecasts are not needed for the dashboard; the forecast panel only shows today and tomorrow.

## 5. Verify

Every metric name from the import should also exist in the new data:

```bash
for m in $(curl -sG $VM/api/v1/label/__name__/values --data-urlencode 'match[]={source="influxdb"}' | jq -r '.data[]'); do
  new=$(curl -sG $VM/api/v1/series --data-urlencode "match[]=${m}{source!=\"influxdb\"}" | jq '.data | length')
  echo "$m  new:$new"
done
```

Names with `new:0` are either not collected anymore (old measurements) or missing in
`json-exporter/config.yml`. Finally look at `powerflow_P_PV` over the last years in vmui
(`http://localhost:8428/vmui`): the curve should be continuous across `T0`.

## 6. Energy prices

The original project reads prices from an `energyprices` bucket. This dashboard uses the variables
`price_buy` and `price_sell` instead (dashboard settings → Variables). Price changes are expressed as a
MetricsQL expression over `time()`, e.g. a price of 0.35 until 2026-03-01 and 0.25 afterwards:

```
(0.35 * (time() < bool 1772319600) + 0.25 * (time() >= bool 1772319600))
```

`1772319600` is the Unix timestamp of the change (`date -d '2026-03-01 00:00 CET' +%s`).
Savings are computed per hour, so each hour is valued with the price valid at that time.

## 7. Shut down InfluxDB and Telegraf

Once everything looks right, back up the InfluxDB data directory one last time and remove both services.
