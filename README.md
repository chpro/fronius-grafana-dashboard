# Fronius Grafana Dashboard for VictoriaMetrics

Grafana dashboard for Fronius inverters (Symo GEN24, Smart Meter, battery) with
**json-exporter + VictoriaMetrics** instead of Telegraf + InfluxDB.

This is a fork of [chpro/fronius-grafana-dashboard](https://github.com/chpro/fronius-grafana-dashboard).
The dashboard layout and panels are the original work of chpro; this fork replaces the data pipeline and
rewrites every query in MetricsQL.

**Disclaimer:** This project is intended for advanced users who are comfortable with data visualization and
Fronius systems. No support or warranty is provided. Use at your own risk.

![Dashboard](grafana/screenshots/dashboard.png)

## What is different from the original

- **No Telegraf, no InfluxDB:** [json-exporter](https://github.com/prometheus-community/json_exporter) turns the
  Fronius Solar API into Prometheus metrics, VictoriaMetrics scrapes and stores them.
- **History can be migrated:** metric names are identical to what Telegraf wrote to InfluxDB, see
  [migration/influxdb-to-victoriametrics.md](migration/influxdb-to-victoriametrics.md).
- **Fewer Grafana plugins:** the power flow gauges read from VictoriaMetrics too, so Grafana no longer talks to
  the inverter directly (no Infinity/JSON datasource needed).
- **Correct calendar days in any timezone, including daylight saving time changes:** daily bars use local
  midnight in the configured timezone.
- **Energy prices as dashboard variables** instead of an InfluxDB bucket. Savings are calculated per hour, so
  price changes are applied exactly when they take effect.
- **Docker Compose and Kubernetes** deployment included.

## Architecture

```
Fronius inverter ──(Solar API v1, HTTP/JSON)──> json-exporter :7979
                                                      ▲
                                                      │ scrape /probe every 60s
                                                      │
forecast.solar ──> fetch-forecast.sh ──(CSV import)──> VictoriaMetrics :8428 ──> Grafana
                                                      │
Fronius Wattpilot ──(WebSocket)──> wattpilot-exporter :9101 (optional)
```

The Ohmpilot is read through the inverter's Solar API (json-exporter module `fronius_ohmpilot`).

## Requirements

- Fronius inverter with **Solar API v1 enabled** (inverter web UI → Communication → Solar API).
  Developed with Symo GEN24 10.0, Smart Meter TS 65A-3 (meter location: grid interconnection point) and
  BYD Battery-Box Premium HVS. Min/max values in the panels match this setup.
- **VictoriaMetrics** (single-node or cluster/vmagent). Plain Prometheus is not enough: the queries use
  MetricsQL functions such as `first_over_time`, `sum_gt_over_time` and `timezone_offset`.
- **Grafana 11 or newer** with these plugins:
  - [VictoriaMetrics datasource](https://grafana.com/grafana/plugins/victoriametrics-metrics-datasource/)
  - [Sun and Moon](https://grafana.com/grafana/plugins/fetzerch-sunandmoon-datasource/) (sun altitude in the
    "Energy Usage" panel), configured with your latitude/longitude
- Optional: a [forecast.solar](https://forecast.solar) URL for the "Expected yield" panel.
- Optional: Fronius **Wattpilot** and/or **Ohmpilot** for the "Wattpilot & Ohmpilot" row, see
  [Wattpilot and Ohmpilot](#wattpilot-and-ohmpilot).

## Quick start with Docker Compose

Starts json-exporter, VictoriaMetrics and Grafana with provisioned datasources and dashboard.

```bash
cd docker-compose
cp .env.example .env    # set FRONIUS_HOST, LATITUDE, LONGITUDE, GRAFANA_ADMIN_PASSWORD
docker compose up -d
# with forecast (set FORECAST_URL in .env first):
docker compose --profile forecast up -d
```

Open Grafana at <http://localhost:3000> and the dashboard **Fronius PV (VictoriaMetrics)**.
Then set timezone and prices, see [Dashboard settings](#dashboard-settings).

## Kubernetes

The repository root is a kustomization that deploys json-exporter, the forecast CronJob and the dashboard as a
ConfigMap for the Grafana sidecar (label `grafana_dashboard: "1"`, as used by kube-prometheus-stack and the
Grafana Helm chart). VictoriaMetrics and Grafana are expected to exist already.

```bash
kubectl create namespace monitoring
kubectl -n monitoring create secret generic pv-forecast \
  --from-literal=FORECAST_URL='https://api.forecast.solar/estimate/watthours/<lat>/<lon>/<dec>/<az>/<kwp>?time=utc'
kubectl apply --server-side -k .
```

For Argo CD or Flux, point an application at this repository (or a fork of it) and override the namespace with
an overlay if needed. Adjust `VM_URL` in [kubernetes/pv-forecast-cronjob.yaml](kubernetes/pv-forecast-cronjob.yaml)
to your VictoriaMetrics service.

Then add the scrape job from [victoriametrics/scrape.yml](victoriametrics/scrape.yml) to your VictoriaMetrics or
vmagent config, with `json-exporter:7979` replaced by `json-exporter.monitoring.svc:7979`, and set the
environment variable `FRONIUS_HOST` on VictoriaMetrics/vmagent (or replace `%{FRONIUS_HOST}` directly).

## Using an existing VictoriaMetrics and Grafana

1. Run json-exporter with [json-exporter/config.yml](json-exporter/config.yml).
2. Add the scrape job from [victoriametrics/scrape.yml](victoriametrics/scrape.yml).
3. For the forecast: start VictoriaMetrics with `-futureRetention=7d` (forecast values have timestamps in the
   future) and run [forecast/fetch-forecast.sh](forecast/fetch-forecast.sh) every 15 minutes.
4. Import [grafana/dashboard.json](grafana/dashboard.json) (Dashboards → New → Import) and select your
   VictoriaMetrics datasource in the variable at the top of the dashboard.

## Wattpilot and Ohmpilot

The row "Wattpilot & Ohmpilot" shows how much energy went into the car (Wattpilot) and into hot water
(Ohmpilot), split into **PV** and **grid**, for the selected time range and per day.

- **Ohmpilot:** nothing to do if the `fronius_ohmpilot` target in [victoriametrics/scrape.yml](victoriametrics/scrape.yml)
  is active (the inverter forwards the Ohmpilot values).
- **Wattpilot:** the Wattpilot has no HTTP API, only a password protected WebSocket.
  [wattpilot-exporter/wattpilot_exporter.py](wattpilot-exporter/wattpilot_exporter.py) (Python standard library
  only) keeps that connection open and serves `wattpilot_*` metrics on port 9101. Use the password of the
  Solar.wattpilot app. Tested with Wattpilot firmware 42.x (`pbkdf2` login); the bcrypt login of the Wattpilot
  Flex is not supported.
  - Docker Compose: set `WATTPILOT_HOST` and `WATTPILOT_PASSWORD` in `.env`, then
    `docker compose --profile wattpilot up -d`.
  - Kubernetes: `kubectl -n monitoring create secret generic wattpilot --from-literal=WATTPILOT_PASSWORD='<password>'`,
    set `WATTPILOT_HOST` in [kubernetes/wattpilot-exporter.yaml](kubernetes/wattpilot-exporter.yaml) and add the
    `wattpilot` job from [victoriametrics/scrape.yml](victoriametrics/scrape.yml) (target
    `wattpilot-exporter.monitoring.svc:9101`).

**How the PV/grid split is calculated:** no device measures where its energy comes from, so the split is estimated
per minute. Grid import at the grid connection point (`powerflow_P_Grid > 0`) is attributed to Wattpilot and
Ohmpilot first, up to their combined power, and divided between both by power. The rest of their consumption
counts as PV. Consequences:

- Energy from the **battery** counts as PV (stored solar energy).
- A **balcony power plant** or any other generator behind the grid meter is included in PV automatically,
  because it reduces the measured grid import.
- Short load peaks within a minute are not visible.
- Selected time ranges longer than about 69 days need `-search.maxPointsSubqueryPerTimeseries=1100000` on
  VictoriaMetrics (already set in the Compose setup), otherwise the panels show an error.

## Dashboard settings

Dashboard settings → Variables:

| Variable | Default | Meaning |
|---|---|---|
| `datasource` | first VictoriaMetrics datasource | datasource for all metric panels |
| `sunandmoon` | first Sun and Moon datasource | sun altitude |
| `tz` | `Europe/Berlin` | IANA timezone for calendar days. Should match the dashboard/browser timezone. |
| `price_buy` | `0.30` | price per kWh drawn from the grid |
| `price_sell` | `0.08` | feed-in tariff per kWh |

Savings are shown in EUR; change the unit of the savings panels if you use another currency.

Prices can change over time. Use a MetricsQL expression with `time()` (Unix timestamp of the change), e.g.

```
(0.35 * (time() < bool 1772319600) + 0.25 * (time() >= bool 1772319600))
```

for 0.35 until 2026-03-01 00:00 CET and 0.25 afterwards. Add one term per price period.

Save the dashboard after changing variables. With file provisioning, set `allowUiUpdates` (already done in the
Compose setup) or edit the defaults in `grafana/dashboard.json`.

## How the numbers are calculated

- **Energy within the selected time range** (stat panels): difference of the meter counters
  (`powerflow_E_Total`, `smartmeter_EnergyReal_WAC_Plus_Absolute`, `smartmeter_EnergyReal_WAC_Minus_Absolute`)
  between start and end of the range. Battery energy is integrated from `powerflow_P_Akku`.
- **Daily bars:** VictoriaMetrics aligns daily steps to 00:00 UTC. The queries therefore evaluate the counters
  on an hourly grid, pick the local midnight in `tz` and subtract the counter at the previous local midnight.
  Days with 23 or 25 hours (DST changes) are handled correctly.
- **Savings:** for every hour, `(production − feed-in) × price_buy + feed-in × price_sell`, summed over the range.

Home usage = production − feed-in + grid draw. Autonomy = self-consumed production / home usage.

## Checking the queries

[scripts/check-queries.py](scripts/check-queries.py) runs every dashboard query against your VictoriaMetrics and
lists errors and empty results (some are expected, e.g. "Grid down" when the grid never failed):

```bash
python3 scripts/check-queries.py http://localhost:8428 --tz Europe/Vienna
```

## Known limitations

- Timezones with a non-hour UTC offset (e.g. India, +05:30) shift daily boundaries by up to 30 minutes.
- Daily battery energy on DST change days is extrapolated from 24 hours of data. Daily Wattpilot/Ohmpilot
  energy on these days covers 24 hours instead of 23/25.
- The "Energy Production & Cost Summary" dashboard of the original project has not been ported.
- Panels rely on metric names only. If you scrape several inverters into the same VictoriaMetrics, add label
  filters to the queries.

## Migrating from the original (Telegraf + InfluxDB)

See [migration/influxdb-to-victoriametrics.md](migration/influxdb-to-victoriametrics.md).

## Credits

- Dashboard and original Telegraf/InfluxDB setup: [chpro/fronius-grafana-dashboard](https://github.com/chpro/fronius-grafana-dashboard)
- [Grafana](https://github.com/grafana/grafana), [VictoriaMetrics](https://github.com/VictoriaMetrics/VictoriaMetrics),
  [json_exporter](https://github.com/prometheus-community/json_exporter), [forecast.solar](https://forecast.solar)

The original project credits:

- [Powerwall Dashboard by jasonacox](https://github.com/jasonacox/Powerwall-Dashboard)
- [Fronius-to-Influx by szymi-](https://github.com/szymi-/fronius-to-influx)
- [Solar Panel Monitoring with Telegraf](https://gist.github.com/farmio/618ca3c1b3f335dd966b19a92acef832)
- [How I Created a Telegraf Plugin to Monitor Solar Panels](https://thenewstack.io/how-i-created-a-telegraf-plugin-to-monitor-solar-panels/)

## License

MIT, see [LICENSE](LICENSE). Original work © 2023 chpro.
