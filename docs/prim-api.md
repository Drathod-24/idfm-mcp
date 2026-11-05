# PRIM API notes

What this project uses from the [IDFM PRIM](https://prim.iledefrance-mobilites.fr) APIs,
and the non-obvious details that shaped the code. Sources: the official PRIM
documentation pages (auth, *Prochains passages*, *Calculateur*, *Info trafic*) and, for
exact SIRI field names, the MIT-licensed [`droso-hass/idfm-api`](https://github.com/droso-hass/idfm-api).

## Common

| | |
|---|---|
| Base URL | `https://prim.iledefrance-mobilites.fr/marketplace` |
| Auth | HTTP header `apikey: <token>` (generate under *Mes jetons d'authentification* → *API*) |
| Default quota | 5 requests/s and **1,000 requests/day per API** (increase on request) |

## Endpoints

| Tool | Endpoint | Notes |
|---|---|---|
| `search_stop` | `GET /v2/navitia/places?q=…&type[]=stop_area` | Navitia. Returns `stop_area:IDFM:<id>` |
| `next_departures` | `GET /stop-monitoring?MonitoringRef=STIF:StopArea:SP:<id>:[&LineRef=STIF:Line::C01743:]` | SIRI Lite. Data in `Siri.ServiceDelivery.StopMonitoringDelivery[0].MonitoredStopVisit[]` |
| (line names) | `GET /v2/navitia/stop_areas/<id>/lines` | Turns `C01743` into "RER B" |
| (line lookup) | `GET /v2/navitia/pt_objects?q=…&type[]=line` | Turns "metro 7" into `line:IDFM:C01377` |
| `plan_journey` | `GET /v2/navitia/journeys?from=…&to=…&datetime=YYYYMMDDTHHMMSS` | `no_solution` error means no route, not a failure |
| `traffic_disruptions` | `GET /v2/navitia/[lines/<line_id>/]line_reports` | Messages: channel `titre` = title, `moteur` = HTML body |

## Gotchas

1. **Two ID systems, same number.** Navitia `stop_area:IDFM:71264` ⇔ SIRI
   `STIF:StopArea:SP:71264:`. Lines: `line:IDFM:C01743` ⇔ `STIF:Line::C01743:`.
2. **SNCF trains (RER, Transilien) are only available at stop-area level** since
   13/03/2025, not per platform. We always query stop areas.
3. **Time zones differ.** SIRI times are UTC (`…Z`); Navitia times are naive Paris local
   time. Windows needs the `tzdata` package for `ZoneInfo("Europe/Paris")`.
4. **Real-time coverage is partial.** An empty departure list is a normal answer.
