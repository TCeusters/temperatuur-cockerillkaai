#!/usr/bin/env python3
"""
Bouwt een interactieve, responsieve grafiekpagina (index.html + data.json + data.csv):
  - Binnentemperatuur van je Netatmo-toestel (weerstation of Home Coach)
  - Buitentemperatuur op de Cockerillkaai, Antwerpen (Open-Meteo)

Boven de grafiek: de HUIDIGE binnen- en buitentemperatuur (live-meting van het
toestel + Open-Meteo). De grafiek zelf: keuze uur/dag/week/maand (met gemiddelde),
periodekiezer en een verversknop.

Twee datasets houden de pagina snel:
  * DAGdata over de VOLLEDIGE geschiedenis  -> dag/week/maand
  * UURdata over de laatste ~6 weken        -> uur-weergave

Enkel Python-standaardbibliotheek. Draait in GitHub Actions.

Omgevingsvariabelen (GitHub secrets/variables):
  NETATMO_CLIENT_ID, NETATMO_CLIENT_SECRET, NETATMO_REFRESH_TOKEN  (verplicht)
  LAT (standaard 51.212), LON (standaard 4.399)   = Cockerillkaai
  HOURLY_DAYS (standaard 42)                        = venster voor uurdata
"""

import csv
import json
import os
import sys
import time
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
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "public")


def die(msg):
    print(f"FOUT: {msg}", file=sys.stderr)
    sys.exit(1)


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_form(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fmt_epoch(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TZ).strftime("%d-%m %H:%M")


def fmt_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).strftime("%d-%m %H:%M")
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 1. Netatmo: token vernieuwen
# --------------------------------------------------------------------------
def netatmo_refresh_access_token():
    cid = os.environ.get("NETATMO_CLIENT_ID")
    secret = os.environ.get("NETATMO_CLIENT_SECRET")
    refresh = os.environ.get("NETATMO_REFRESH_TOKEN")
    if not (cid and secret and refresh):
        die("NETATMO_CLIENT_ID, NETATMO_CLIENT_SECRET en NETATMO_REFRESH_TOKEN "
            "moeten alle drie ingesteld zijn als secrets.")
    try:
        result = http_post_form(NETATMO_TOKEN_URL, {
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": cid, "client_secret": secret,
        })
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        die(f"Netatmo-token vernieuwen mislukt (HTTP {e.code}). Vaak is het refresh-token "
            f"verlopen/ongeldig en moet je op dev.netatmo.com een nieuw genereren. Antwoord: {detail}")

    access = result.get("access_token")
    new_refresh = result.get("refresh_token")
    if not access:
        die(f"Geen access_token ontvangen van Netatmo. Antwoord: {result}")
    if new_refresh and new_refresh != refresh:
        with open(os.path.join(REPO_ROOT, "new_refresh_token.txt"), "w") as f:
            f.write(new_refresh)
        print("Info: Netatmo gaf een nieuw refresh-token terug; wordt bewaard.")
    return access


# --------------------------------------------------------------------------
# 2. Netatmo: toestel vinden (weerstation of Home Coach) + live-meting
# --------------------------------------------------------------------------
def netatmo_find_device(access):
    headers = {"Authorization": f"Bearer {access}"}

    def fetch(url, label):
        try:
            resp = http_get(url, headers=headers)
        except urllib.error.HTTPError as e:
            print(f"Info: {label} gaf HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
            return []
        if isinstance(resp, dict) and resp.get("error"):
            print(f"Info: {label} gaf een foutmelding: {resp.get('error')}")
        return resp.get("body", {}).get("devices", []) or []

    device_type = "weerstation"
    devices = fetch(NETATMO_STATIONS_URL, "getstationsdata (weerstation)")
    if not devices:
        device_type = "Home Coach"
        devices = fetch(NETATMO_HOMECOACH_URL, "gethomecoachsdata (Home Coach)")
    if not devices:
        die("Geen Netatmo-toestel gevonden op dit account (noch weerstation, noch Home Coach). "
            "Controleer dat het token de scopes 'read_station' EN 'read_homecoach' bevat, en dat je "
            "op dev.netatmo.com hetzelfde account gebruikt als in de Netatmo-app.")

    d = devices[0]
    dash = d.get("dashboard_data") or {}
    return {
        "id": d["_id"],
        "station": d.get("station_name") or d.get("home_name") or "Netatmo",
        "module": d.get("module_name", "Binnen"),
        "date_setup": int(d.get("date_setup") or 0),
        "type": device_type,
        "cur_in": dash.get("Temperature"),
        "cur_in_time": fmt_epoch(dash.get("time_utc")),
        "cur_hum": dash.get("Humidity"),
        "cur_co2": dash.get("CO2"),
    }


def _measure(access, device_id, scale, date_begin):
    headers = {"Authorization": f"Bearer {access}"}
    params = {
        "device_id": device_id, "scale": scale, "type": "temperature",
        "date_begin": int(date_begin), "optimize": "false",
    }
    url = NETATMO_MEASURE_URL + "?" + urllib.parse.urlencode(params)
    try:
        resp = http_get(url, headers=headers)
    except urllib.error.HTTPError as e:
        die(f"getmeasure ({scale}) mislukt (HTTP {e.code}). Antwoord: {e.read().decode('utf-8', 'ignore')}")
    return resp.get("body", {}) or {}


def netatmo_daily_full(access, device_id, date_setup):
    """Volledige historiek op dagbasis, via voorwaartse paginatie (1024/req)."""
    now = int(time.time())
    begin = date_setup if date_setup else now - 6 * 365 * 86400
    out = {}
    for _ in range(12):
        body = _measure(access, device_id, "1day", begin)
        if not body:
            break
        last_ts = None
        for ts_str, vals in sorted(body.items(), key=lambda kv: int(kv[0])):
            last_ts = int(ts_str)
            if vals and vals[0] is not None:
                d = datetime.fromtimestamp(last_ts, tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d")
                out[d] = round(float(vals[0]), 1)
        if last_ts is None or len(body) < 1000:
            break
        begin = last_ts + 86400
        if begin > now:
            break
    print(f"Netatmo dagdata: {len(out)} dagen.")
    return out


def netatmo_hourly_full(access, device_id, date_setup):
    """Volledige historiek op uurbasis, via voorwaartse paginatie (1024/req)."""
    now = int(time.time())
    begin = date_setup if date_setup else now - 3 * 365 * 86400
    out = {}
    for _ in range(60):
        body = _measure(access, device_id, "1hour", begin)
        if not body:
            break
        last_ts = None
        for ts_str, vals in sorted(body.items(), key=lambda kv: int(kv[0])):
            last_ts = int(ts_str)
            if vals and vals[0] is not None:
                dt = datetime.fromtimestamp(last_ts, tz=timezone.utc).astimezone(TZ)
                out[dt.strftime("%Y-%m-%dT%H:00")] = round(float(vals[0]), 1)
        if last_ts is None or len(body) < 1000:
            break
        begin = last_ts + 3600
        if begin > now:
            break
    print(f"Netatmo uurdata (volledig): {len(out)} uren.")
    return out


# --------------------------------------------------------------------------
# 3. Open-Meteo: buitentemperatuur
# --------------------------------------------------------------------------
def openmeteo_daily(lat, lon, start_date):
    today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
    out = {}
    try:
        p = {"latitude": lat, "longitude": lon, "start_date": start_date, "end_date": today,
             "daily": "temperature_2m_mean", "timezone": "Europe/Brussels"}
        data = http_get(OPEN_METEO_ARCHIVE_URL + "?" + urllib.parse.urlencode(p))
        for d, t in zip(data.get("daily", {}).get("time", []),
                        data.get("daily", {}).get("temperature_2m_mean", [])):
            if t is not None:
                out[d] = round(float(t), 1)
    except urllib.error.HTTPError as e:
        print(f"Info: Open-Meteo archief gaf HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
    try:
        p = {"latitude": lat, "longitude": lon, "daily": "temperature_2m_mean",
             "past_days": 14, "forecast_days": 1, "timezone": "Europe/Brussels"}
        data = http_get(OPEN_METEO_FORECAST_URL + "?" + urllib.parse.urlencode(p))
        for d, t in zip(data.get("daily", {}).get("time", []),
                        data.get("daily", {}).get("temperature_2m_mean", [])):
            if t is not None:
                out[d] = round(float(t), 1)
    except urllib.error.HTTPError as e:
        print(f"Info: Open-Meteo forecast (dag) gaf HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
    print(f"Open-Meteo dagdata: {len(out)} dagen.")
    return out


def openmeteo_hourly_full(lat, lon, start_date):
    """Uurdata buiten over de volledige historie (archief) + recent/toekomst (forecast)."""
    today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
    out = {}
    try:
        p = {"latitude": lat, "longitude": lon, "start_date": start_date, "end_date": today,
             "hourly": "temperature_2m", "timezone": "Europe/Brussels"}
        data = http_get(OPEN_METEO_ARCHIVE_URL + "?" + urllib.parse.urlencode(p))
        for t, temp in zip(data.get("hourly", {}).get("time", []),
                           data.get("hourly", {}).get("temperature_2m", [])):
            if temp is not None:
                out[t[:13] + ":00"] = round(float(temp), 1)
    except urllib.error.HTTPError as e:
        print(f"Info: Open-Meteo archief (uur) gaf HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
    try:
        p = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
             "past_days": 14, "forecast_days": 3, "timezone": "Europe/Brussels"}
        data = http_get(OPEN_METEO_FORECAST_URL + "?" + urllib.parse.urlencode(p))
        for t, temp in zip(data.get("hourly", {}).get("time", []),
                           data.get("hourly", {}).get("temperature_2m", [])):
            if temp is not None:
                out[t[:13] + ":00"] = round(float(temp), 1)
    except urllib.error.HTTPError as e:
        print(f"Info: Open-Meteo forecast (uur) gaf HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
    print(f"Open-Meteo uurdata (volledig): {len(out)} uren.")
    return out


def openmeteo_current(lat, lon):
    p = {"latitude": lat, "longitude": lon, "current": "temperature_2m", "timezone": "Europe/Brussels"}
    try:
        data = http_get(OPEN_METEO_FORECAST_URL + "?" + urllib.parse.urlencode(p))
    except urllib.error.HTTPError as e:
        print(f"Info: Open-Meteo current gaf HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
        return None, None
    c = data.get("current", {})
    t = c.get("temperature_2m")
    return (round(float(t), 1) if t is not None else None), fmt_iso(c.get("time"))


# --------------------------------------------------------------------------
# 4. Samenvoegen en wegschrijven
# --------------------------------------------------------------------------
def build_outputs(dev, in_daily, out_daily, in_hourly, out_hourly, forecast, csv_rows, cur_out, cur_out_t, lat, lon):
    os.makedirs(OUT_DIR, exist_ok=True)

    daily_keys = sorted(set(in_daily) | set(out_daily))
    daily = [{"d": k, "binnen": in_daily.get(k), "buiten": out_daily.get(k)} for k in daily_keys]

    hourly_keys = sorted(set(in_hourly) | set(out_hourly))
    hourly = [{"t": k, "binnen": in_hourly.get(k), "buiten": out_hourly.get(k)} for k in hourly_keys]

    forecast_list = [{"t": k, "buiten": forecast[k]} for k in sorted(forecast)]

    data = {
        "station": dev["station"], "indoor_name": dev["module"], "device_type": dev["type"],
        "updated": datetime.now(tz=TZ).strftime("%d-%m-%Y %H:%M"),
        "lat": str(lat), "lon": str(lon),
        "current_in": dev.get("cur_in"), "current_in_time": dev.get("cur_in_time"),
        "current_out": cur_out, "current_out_time": cur_out_t,
        "current_hum": dev.get("cur_hum"), "current_co2": dev.get("cur_co2"),
        "hourly_from": hourly[0]["t"] if hourly else None,
        "daily": daily, "hourly": hourly, "forecast": forecast_list,
    }
    with open(os.path.join(OUT_DIR, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # CSV = volledige uurhistorie (handig voor Excel)
    with open(os.path.join(OUT_DIR, "data.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["tijd", "binnen_C", "buiten_C"])
        w.writeheader()
        for row in csv_rows:
            w.writerow({"tijd": row["t"].replace("T", " "),
                        "binnen_C": "" if row["binnen"] is None else row["binnen"],
                        "buiten_C": "" if row["buiten"] is None else row["buiten"]})

    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(INDEX_HTML)

    print(f"Klaar: {len(daily)} dagen, {len(hourly)} uren -> public/index.html, data.json, data.csv.")


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Temperatuur Cockerillkaai 26</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 0; padding: 20px; color: #1a1a1a; background: #fafafa; }
  .wrap { max-width: 1040px; margin: 0 auto; }
  .head { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 6px; }
  h1 { font-size: 18px; margin: 0; }
  .badge { color: #8a8a8a; font-size: 12px; }
  .stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 14px 0 6px; }
  .stat { flex: 1 1 200px; background: #fff; border: 1px solid #e5e5e5; border-radius: 12px; padding: 14px 16px; }
  .stat-label { font-size: 12px; color: #666; }
  .stat-val { font-size: 34px; font-weight: 650; line-height: 1.05; margin-top: 4px; }
  .stat.in .stat-val { color: #e11d48; }
  .stat.out .stat-val { color: #2563eb; }
  .stat.hum .stat-val { color: #0891b2; }
  .stat.co2 .stat-val { color: #65a30d; }
  .stat-time { font-size: 11px; color: #9a9a9a; margin-top: 3px; }
  .controls { display: flex; gap: 10px 12px; align-items: center; flex-wrap: wrap; margin: 14px 0 6px; }
  .seg button { border: 1px solid #d0d0d0; background: #fff; padding: 7px 13px; cursor: pointer; font-size: 13px; }
  .seg button:first-child { border-radius: 6px 0 0 6px; }
  .seg button:last-child { border-radius: 0 6px 6px 0; }
  .seg button + button { border-left: none; }
  .seg button.active { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  select, input[type=date] { padding: 7px 8px; font-size: 13px; border: 1px solid #d0d0d0; border-radius: 6px; background: #fff; }
  #refresh { padding: 7px 13px; font-size: 13px; border: 1px solid #d0d0d0; border-radius: 6px;
             background: #fff; cursor: pointer; }
  #refresh:active { background: #eee; }
  .note { color: #a15c00; font-size: 12px; min-height: 15px; margin: 2px 0 8px; }
  .card { background: #fff; border: 1px solid #e5e5e5; border-radius: 12px; padding: 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
  .chartbox { position: relative; height: 58vh; min-height: 320px; }
  .foot { color: #888; font-size: 12px; margin-top: 14px; }
  a { color: #2563eb; }
  label { font-size: 13px; color: #555; }
  @media (max-width: 640px) {
    body { padding: 12px; }
    h1 { font-size: 15px; }
    .stat-val { font-size: 30px; }
    .chartbox { height: 62vh; min-height: 360px; }
    .card { padding: 10px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <h1 id="title">Temperatuur Cockerillkaai 26</h1>
    <span class="badge" id="updated"></span>
  </div>

  <div class="stats">
    <div class="stat out">
      <div class="stat-label">Temperatuur buiten</div>
      <div class="stat-val" id="curOut">–</div>
    </div>
    <div class="stat in">
      <div class="stat-label">Temperatuur binnen</div>
      <div class="stat-val" id="curIn">–</div>
    </div>
    <div class="stat hum">
      <div class="stat-label">Vochtigheid binnen</div>
      <div class="stat-val" id="curHum">–</div>
    </div>
    <div class="stat co2">
      <div class="stat-label">CO₂ binnen</div>
      <div class="stat-val" id="curCo2">–</div>
    </div>
  </div>

  <div class="controls">
    <div class="seg" id="gran">
      <button data-g="uur">Uur</button>
      <button data-g="dag">Dag</button>
      <button data-g="week">Week</button>
      <button data-g="maand">Maand</button>
    </div>
    <label>Periode:
      <select id="period">
        <option value="7">Laatste 7 dagen</option>
        <option value="30">Laatste 30 dagen</option>
        <option value="90">Laatste 3 maanden</option>
        <option value="365">Laatste 12 maanden</option>
        <option value="all">Alles</option>
        <option value="custom">Aangepast…</option>
      </select>
    </label>
    <span id="customRange" style="display:none">
      <input type="date" id="from"> – <input type="date" id="to">
    </span>
  </div>

  <div class="note" id="note"></div>
  <div class="card"><div class="chartbox"><canvas id="grafiek"></canvas></div></div>
  <div class="foot">
    Buitentemperatuur: Open-Meteo (<span id="coord"></span>).
    &nbsp;|&nbsp; <a href="data.csv" download>Download volledige uurdata (CSV)</a>
  </div>
</div>
<script>
let DATA = null, chart = null, wired = false;
const $ = id => document.getElementById(id);
const LS = { get:(k,d)=>localStorage.getItem('ck_'+k)||d, set:(k,v)=>localStorage.setItem('ck_'+k,v) };

function loadData(){
  fetch('data.json?t=' + Date.now(), {cache:'no-store'})
    .then(r => r.json())
    .then(d => { DATA = d; onData(); })
    .catch(e => { $('note').textContent = 'Kon data niet laden: ' + e; });
}
loadData();

function onData(){
  $('coord').textContent = DATA.lat + ', ' + DATA.lon;
  $('updated').textContent = 'bijgewerkt: ' + DATA.updated;
  showCurrent();
  refreshOutdoorLive();
  if (!wired) wireControls();
  render();
}

function showCurrent(){
  $('curIn').textContent  = DATA.current_in  != null ? Number(DATA.current_in).toFixed(1)  + ' °C' : '–';
  $('curOut').textContent = DATA.current_out != null ? Number(DATA.current_out).toFixed(1) + ' °C' : '–';
  $('curHum').textContent = DATA.current_hum != null ? Math.round(DATA.current_hum) + ' %' : '–';
  $('curCo2').textContent = DATA.current_co2 != null ? Math.round(DATA.current_co2) + ' ppm' : '–';
}

function refreshOutdoorLive(){
  const url = 'https://api.open-meteo.com/v1/forecast?latitude=' + DATA.lat +
              '&longitude=' + DATA.lon + '&current=temperature_2m&timezone=Europe%2FBrussels';
  fetch(url, {cache:'no-store'}).then(r => r.json()).then(d => {
    const c = d.current;
    if (c && c.temperature_2m != null){
      $('curOut').textContent = Number(c.temperature_2m).toFixed(1) + ' °C';
    }
  }).catch(()=>{});
}

function wireControls(){
  wired = true;
  const g = LS.get('g', 'dag');
  document.querySelectorAll('#gran button').forEach(b => {
    b.classList.toggle('active', b.dataset.g === g);
    b.onclick = () => setGran(b.dataset.g);
  });
  $('period').value = LS.get('period', '30');
  $('from').value = LS.get('from', '');
  $('to').value = LS.get('to', '');
  $('period').onchange = () => { LS.set('period', $('period').value); toggleCustom(); render(); };
  $('from').onchange = () => { LS.set('from', $('from').value); render(); };
  $('to').onchange = () => { LS.set('to', $('to').value); render(); };
  toggleCustom();
}

function setGran(g){
  LS.set('g', g);
  document.querySelectorAll('#gran button').forEach(b => b.classList.toggle('active', b.dataset.g === g));
  render();
}
function toggleCustom(){ $('customRange').style.display = $('period').value === 'custom' ? 'inline' : 'none'; }

function periodBounds(){
  const p = $('period').value;
  if (p === 'custom') return { start: $('from').value || '0000-01-01', end: $('to').value || '9999-12-31' };
  if (p === 'all')    return { start: '0000-01-01', end: '9999-12-31' };
  const s = new Date(Date.now() - parseInt(p,10)*86400000);
  return { start: s.toISOString().slice(0,10), end: '9999-12-31' };
}

function avg(a){ return a.length ? Math.round(a.reduce((x,y)=>x+y,0)/a.length*10)/10 : null; }
function isoWeek(ds){
  const d = new Date(ds + 'T00:00:00Z');
  const day = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - day + 3);
  const f = new Date(Date.UTC(d.getUTCFullYear(), 0, 4));
  const week = 1 + Math.round(((d - f)/86400000 - 3 + ((f.getUTCDay()+6)%7))/7);
  return { year: d.getUTCFullYear(), week };
}
function groupDaily(points, keyFn, labelFn){
  const m = new Map();
  for (const p of points){
    const k = keyFn(p.d);
    if (!m.has(k)) m.set(k, { b:[], o:[], label: labelFn(p.d) });
    const g = m.get(k);
    if (p.binnen != null) g.b.push(p.binnen);
    if (p.buiten != null) g.o.push(p.buiten);
  }
  const keys = [...m.keys()].sort();
  return { labels: keys.map(k=>m.get(k).label), binnen: keys.map(k=>avg(m.get(k).b)), buiten: keys.map(k=>avg(m.get(k).o)) };
}

function buildSeries(){
  const { start, end } = periodBounds();
  const g = LS.get('g', 'dag');
  $('note').textContent = '';
  if (g === 'uur'){
    const pts = DATA.hourly.filter(p => p.t.slice(0,10) >= start && p.t.slice(0,10) <= end);
    if (DATA.hourly_from && start < DATA.hourly_from.slice(0,10))
      $('note').textContent = 'Uurweergave is beschikbaar vanaf ' + DATA.hourly_from.slice(0,10) + '. Kies Dag/Week/Maand voor oudere periodes.';
    const fc = (DATA.forecast || []).filter(p => p.t.slice(0,10) <= end);
    const fmt = t => t.slice(8,10)+'-'+t.slice(5,7)+' '+t.slice(11,16);
    const labels = pts.map(p => fmt(p.t)).concat(fc.map(p => fmt(p.t)));
    const binnen = pts.map(p => p.binnen).concat(fc.map(() => null));
    const buiten = pts.map(p => p.buiten).concat(fc.map(() => null));
    const verwacht = pts.map(() => null).concat(fc.map(p => p.buiten));
    if (pts.length && fc.length){
      for (let i = pts.length - 1; i >= 0; i--){ if (pts[i].buiten != null){ verwacht[i] = pts[i].buiten; break; } }
    }
    return { labels, binnen, buiten, verwacht };
  }
  const pts = DATA.daily.filter(p => p.d >= start && p.d <= end);
  if (g === 'dag')
    return { labels: pts.map(p => p.d.slice(8,10)+'-'+p.d.slice(5,7)+'-'+p.d.slice(0,4)),
             binnen: pts.map(p => p.binnen), buiten: pts.map(p => p.buiten) };
  if (g === 'week')
    return groupDaily(pts, d => { const w = isoWeek(d); return w.year+'-W'+String(w.week).padStart(2,'0'); },
                      d => { const w = isoWeek(d); return 'wk '+w.week+' '+w.year; });
  return groupDaily(pts, d => d.slice(0,7), d => d.slice(5,7)+'-'+d.slice(0,4));
}

function render(){
  const s = buildSeries();
  const datasets = [
    { label: 'Binnen (Netatmo)', data: s.binnen, borderColor:'#e11d48', backgroundColor:'#e11d48',
      tension: 0.25, spanGaps: true, pointRadius: 0, borderWidth: 2 },
    { label: 'Buiten (Cockerillkaai)', data: s.buiten, borderColor:'#2563eb', backgroundColor:'#2563eb',
      tension: 0.25, spanGaps: true, pointRadius: 0, borderWidth: 2 },
  ];
  if (s.verwacht && s.verwacht.some(v => v != null))
    datasets.push({ label: 'Buiten (verwacht 48u)', data: s.verwacht, borderColor:'#93c5fd',
      backgroundColor:'#93c5fd', borderDash: [6,4], tension: 0.25, spanGaps: true, pointRadius: 0, borderWidth: 2 });
  const data = { labels: s.labels, datasets };
  if (chart){ chart.data = data; chart.update(); return; }
  chart = new Chart($('grafiek'), {
    type: 'line', data,
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
                y: { title: { display: true, text: 'Temperatuur (°C)' } } },
      plugins: { legend: { position: 'top' } },
    },
  });
}
</script>
</body>
</html>
"""


def main():
    lat = os.environ.get("LAT", "51.212")
    lon = os.environ.get("LON", "4.399")
    hourly_days = int(os.environ.get("HOURLY_DAYS", "42"))

    access = netatmo_refresh_access_token()
    dev = netatmo_find_device(access)

    in_daily = netatmo_daily_full(access, dev["id"], dev["date_setup"])
    in_hourly_full = netatmo_hourly_full(access, dev["id"], dev["date_setup"])

    if in_daily:
        start_date = min(in_daily)
    elif in_hourly_full:
        start_date = min(in_hourly_full)[:10]
    else:
        start_date = (datetime.now(tz=TZ) - timedelta(days=hourly_days)).strftime("%Y-%m-%d")

    out_daily = openmeteo_daily(lat, lon, start_date)
    out_hourly_full = openmeteo_hourly_full(lat, lon, start_date)
    cur_out, cur_out_t = openmeteo_current(lat, lon)

    now_h = datetime.now(tz=TZ).strftime("%Y-%m-%dT%H:00")
    limit_h = (datetime.now(tz=TZ) + timedelta(hours=48)).strftime("%Y-%m-%dT%H:00")
    recent_h = (datetime.now(tz=TZ) - timedelta(days=hourly_days)).strftime("%Y-%m-%dT%H:00")

    out_hourly_obs = {k: v for k, v in out_hourly_full.items() if k <= now_h}
    forecast = {k: v for k, v in out_hourly_full.items() if now_h < k <= limit_h}

    # Volledige uurhistorie voor de CSV-export (binnen + buiten, tot nu)
    csv_keys = sorted(set(in_hourly_full) | set(out_hourly_obs))
    csv_rows = [{"t": k, "binnen": in_hourly_full.get(k), "buiten": out_hourly_obs.get(k)} for k in csv_keys]

    # Recente uurdata voor de interactieve pagina (houdt de pagina snel)
    in_hourly = {k: v for k, v in in_hourly_full.items() if k >= recent_h}
    out_hourly = {k: v for k, v in out_hourly_obs.items() if k >= recent_h}
    print(f"CSV: {len(csv_rows)} uren; pagina: {len(in_hourly)} recente uren; voorspeld: {len(forecast)}.")

    build_outputs(dev, in_daily, out_daily, in_hourly, out_hourly, forecast, csv_rows, cur_out, cur_out_t, lat, lon)


if __name__ == "__main__":
    main()
