#!/usr/bin/env python3
"""
Bouwt een grafiek (index.html) + data.csv met twee lijnen:
  1. Binnentemperatuur van je Netatmo-toestel (weerstation of Home Coach), per uur
  2. Buitentemperatuur op de Cockerillkaai, Antwerpen (Open-Meteo, per uur)

Gebruikt enkel de Python-standaardbibliotheek (geen pip install nodig).
Draait in GitHub Actions; niets hoeft lokaal geinstalleerd te worden.

Configuratie via omgevingsvariabelen (in GitHub als "secrets"/"variables"):
  NETATMO_CLIENT_ID       (secret, verplicht)
  NETATMO_CLIENT_SECRET   (secret, verplicht)
  NETATMO_REFRESH_TOKEN   (secret, verplicht)
  LAT                     (optioneel, standaard 51.212  = Cockerillkaai)
  LON                     (optioneel, standaard 4.399   = Cockerillkaai)
  DAYS                    (optioneel, standaard 30 dagen historiek)

Bij het vernieuwen van de Netatmo-token kan Netatmo een NIEUW refresh-token
teruggeven. Dat schrijven we naar het bestand new_refresh_token.txt, zodat de
workflow het als nieuwe secret kan bewaren voor de volgende dag.
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Brussels")
NETATMO_TOKEN_URL = "https://api.netatmo.com/oauth2/token"
NETATMO_STATIONS_URL = "https://api.netatmo.com/api/getstationsdata"
NETATMO_HOMECOACH_URL = "https://api.netatmo.com/api/gethomecoachsdata"
NETATMO_MEASURE_URL = "https://api.netatmo.com/api/getmeasure"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "public")


def die(msg):
    """Stop met een duidelijke foutmelding (zichtbaar in de GitHub-log)."""
    print(f"FOUT: {msg}", file=sys.stderr)
    sys.exit(1)


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_form(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def hour_key(dt_local):
    """Sleutel per uur in lokale (Brusselse) tijd: 'YYYY-MM-DD HH:00'."""
    return dt_local.strftime("%Y-%m-%d %H:00")


# --------------------------------------------------------------------------
# 1. Netatmo: token vernieuwen
# --------------------------------------------------------------------------
def netatmo_refresh_access_token():
    client_id = os.environ.get("NETATMO_CLIENT_ID")
    client_secret = os.environ.get("NETATMO_CLIENT_SECRET")
    refresh_token = os.environ.get("NETATMO_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        die("NETATMO_CLIENT_ID, NETATMO_CLIENT_SECRET en NETATMO_REFRESH_TOKEN "
            "moeten alle drie ingesteld zijn als secrets.")

    try:
        result = http_post_form(NETATMO_TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        })
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        die(f"Netatmo-token vernieuwen mislukt (HTTP {e.code}). "
            f"Vaak betekent dit dat het refresh-token verlopen/ongeldig is en "
            f"opnieuw gegenereerd moet worden op dev.netatmo.com. Antwoord: {detail}")

    access_token = result.get("access_token")
    new_refresh = result.get("refresh_token")
    if not access_token:
        die(f"Geen access_token ontvangen van Netatmo. Antwoord: {result}")

    # Bewaar een (mogelijk) nieuw refresh-token onmiddellijk, zodat het niet
    # verloren gaat als de rest van het script faalt.
    if new_refresh and new_refresh != refresh_token:
        with open(os.path.join(REPO_ROOT, "new_refresh_token.txt"), "w") as f:
            f.write(new_refresh)
        print("Info: Netatmo gaf een nieuw refresh-token terug; wordt bewaard.")

    return access_token


# --------------------------------------------------------------------------
# 2. Netatmo: toestel vinden (weerstation of Home Coach) + uurdata ophalen
# --------------------------------------------------------------------------
def netatmo_get_indoor_series(access_token, days):
    headers = {"Authorization": f"Bearer {access_token}"}

    def fetch_devices(url, label):
        try:
            resp = http_get(url, headers=headers)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            print(f"Info: {label} gaf HTTP {e.code}: {detail}")
            return []
        if isinstance(resp, dict) and resp.get("error"):
            print(f"Info: {label} gaf een foutmelding: {resp.get('error')}")
        return resp.get("body", {}).get("devices", []) or []

    # Eerst zoeken naar een weerstation, daarna naar een Home Coach.
    device_type = "weerstation"
    devices = fetch_devices(NETATMO_STATIONS_URL, "getstationsdata (weerstation)")
    if not devices:
        device_type = "Home Coach"
        devices = fetch_devices(NETATMO_HOMECOACH_URL, "gethomecoachsdata (Home Coach)")

    if not devices:
        die("Geen Netatmo-toestel gevonden op dit account (noch weerstation, noch Home Coach). "
            "Controleer dat het token de scopes 'read_station' EN 'read_homecoach' bevat, en dat je "
            "op dev.netatmo.com hetzelfde account gebruikt als in de Netatmo-app.")

    device = devices[0]
    device_id = device["_id"]                      # MAC van het toestel
    station_name = device.get("station_name") or device.get("home_name") or "Netatmo"
    indoor_name = device.get("module_name", "Binnen")

    date_begin = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp())

    params = {
        "device_id": device_id,       # binnenmodule = het toestel zelf
        "scale": "1hour",
        "type": "temperature",
        "date_begin": date_begin,
        "optimize": "false",          # geeft {timestamp: [waarde]} terug
    }
    url = NETATMO_MEASURE_URL + "?" + urllib.parse.urlencode(params)
    try:
        measure = http_get(url, headers=headers)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        die(f"getmeasure mislukt (HTTP {e.code}). Antwoord: {detail}")

    body = measure.get("body", {})
    series = {}
    for ts_str, values in body.items():
        if not values or values[0] is None:
            continue
        dt_local = datetime.fromtimestamp(int(ts_str), tz=timezone.utc).astimezone(TZ)
        series[hour_key(dt_local)] = round(float(values[0]), 1)

    if not series:
        die("Geen binnentemperatuur-metingen ontvangen van Netatmo.")

    print(f"Netatmo: {len(series)} uurwaarden opgehaald "
          f"({device_type}: {station_name} / {indoor_name}).")
    return series, station_name, indoor_name


# --------------------------------------------------------------------------
# 3. Open-Meteo: buitentemperatuur Cockerillkaai
# --------------------------------------------------------------------------
def openmeteo_get_series(lat, lon, days):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "past_days": min(days, 92),   # Open-Meteo forecast-API: max 92 dagen historiek
        "forecast_days": 1,
        "timezone": "Europe/Brussels",
    }
    url = OPEN_METEO_URL + "?" + urllib.parse.urlencode(params)
    try:
        data = http_get(url)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        die(f"Open-Meteo mislukt (HTTP {e.code}). Antwoord: {detail}")

    times = data.get("hourly", {}).get("time", [])
    temps = data.get("hourly", {}).get("temperature_2m", [])
    series = {}
    for t, temp in zip(times, temps):
        if temp is None:
            continue
        # t = 'YYYY-MM-DDTHH:MM' in lokale (Brusselse) tijd
        dt_local = datetime.fromisoformat(t).replace(tzinfo=TZ)
        series[hour_key(dt_local)] = round(float(temp), 1)

    if not series:
        die("Geen buitentemperatuur ontvangen van Open-Meteo.")

    print(f"Open-Meteo: {len(series)} uurwaarden opgehaald.")
    return series


# --------------------------------------------------------------------------
# 4. Samenvoegen en wegschrijven
# --------------------------------------------------------------------------
def build_outputs(indoor, outdoor, station_name, indoor_name, lat, lon):
    os.makedirs(OUT_DIR, exist_ok=True)
    all_keys = sorted(set(indoor) | set(outdoor))

    rows = []
    for k in all_keys:
        rows.append({
            "tijd": k,
            "binnen_C": indoor.get(k, ""),
            "buiten_C": outdoor.get(k, ""),
        })

    # CSV voor Excel
    with open(os.path.join(OUT_DIR, "data.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["tijd", "binnen_C", "buiten_C"])
        w.writeheader()
        w.writerows(rows)

    # Data voor de grafiek (ISO-tijd zodat de tijd-as correct schaalt)
    labels = [r["tijd"].replace(" ", "T") for r in rows]
    binnen = [r["binnen_C"] if r["binnen_C"] != "" else None for r in rows]
    buiten = [r["buiten_C"] if r["buiten_C"] != "" else None for r in rows]

    updated = datetime.now(tz=TZ).strftime("%d-%m-%Y %H:%M")
    html = HTML_TEMPLATE.format(
        station=station_name,
        indoor_name=indoor_name,
        updated=updated,
        lat=lat,
        lon=lon,
        labels=json.dumps(labels),
        binnen=json.dumps(binnen),
        buiten=json.dumps(buiten),
    )
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Klaar: {len(rows)} rijen weggeschreven naar public/index.html en public/data.csv.")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Temperatuur: binnen vs. buiten (Cockerillkaai)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/luxon@3.4.4/build/global/luxon.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.3.1/dist/chartjs-adapter-luxon.umd.min.js"></script>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 0; padding: 24px; color: #1a1a1a; background: #fafafa; }}
  .wrap {{ max-width: 1000px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
  .card {{ background: #fff; border: 1px solid #e5e5e5; border-radius: 10px;
          padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
  .foot {{ color: #888; font-size: 12px; margin-top: 16px; }}
  a {{ color: #2563eb; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Binnentemperatuur ({station} &ndash; {indoor_name}) vs. buitentemperatuur Cockerillkaai</h1>
  <div class="sub">Uurwaarden. Laatst bijgewerkt: {updated} (Brusselse tijd).</div>
  <div class="card">
    <canvas id="grafiek" height="120"></canvas>
  </div>
  <div class="foot">
    Buitentemperatuur: Open-Meteo, coordinaten {lat}, {lon}. &nbsp;|&nbsp;
    <a href="data.csv" download>Download data (CSV)</a>
  </div>
</div>
<script>
  const labels = {labels};
  const binnen = {binnen};
  const buiten = {buiten};
  new Chart(document.getElementById('grafiek'), {{
    type: 'line',
    data: {{
      labels: labels,
      datasets: [
        {{ label: 'Binnen (Netatmo)', data: binnen, borderColor: '#e11d48',
           backgroundColor: '#e11d48', tension: 0.25, spanGaps: true,
           pointRadius: 0, borderWidth: 2 }},
        {{ label: 'Buiten (Cockerillkaai)', data: buiten, borderColor: '#2563eb',
           backgroundColor: '#2563eb', tension: 0.25, spanGaps: true,
           pointRadius: 0, borderWidth: 2 }}
      ]
    }},
    options: {{
      responsive: true,
      interaction: {{ mode: 'index', intersect: false }},
      scales: {{
        x: {{ type: 'time', time: {{ unit: 'day', tooltipFormat: 'dd-MM HH:mm' }},
             ticks: {{ maxRotation: 0, autoSkip: true }} }},
        y: {{ title: {{ display: true, text: 'Temperatuur (Celsius)' }} }}
      }},
      plugins: {{ legend: {{ position: 'top' }} }}
    }}
  }});
</script>
</body>
</html>
"""


def main():
    days = int(os.environ.get("DAYS", "30"))
    lat = os.environ.get("LAT", "51.212")
    lon = os.environ.get("LON", "4.399")

    access_token = netatmo_refresh_access_token()
    indoor, station_name, indoor_name = netatmo_get_indoor_series(access_token, days)
    outdoor = openmeteo_get_series(lat, lon, days)
    build_outputs(indoor, outdoor, station_name, indoor_name, lat, lon)


if __name__ == "__main__":
    main()
