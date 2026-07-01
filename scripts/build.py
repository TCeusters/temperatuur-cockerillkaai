#!/usr/bin/env python3
"""
Bouwt een interactieve grafiekpagina (index.html + data.json + data.csv):
  - Binnentemperatuur van je Netatmo-toestel (weerstation of Home Coach)
  - Buitentemperatuur op de Cockerillkaai, Antwerpen (Open-Meteo)

Twee datasets worden opgehaald, zodat de pagina snel blijft:
  * DAGdata over de VOLLEDIGE geschiedenis (compact) -> voor dag/week/maand
  * UURdata over de laatste ~6 weken            -> voor de uur-weergave

De pagina zelf regelt de periodekeuze, de granulariteit (uur/dag/week/maand,
met gemiddelde) en toont wanneer ze laatst is bijgewerkt.

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
# 2. Netatmo: toestel vinden (weerstation of Home Coach)
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
    return {
        "id": d["_id"],
        "station": d.get("station_name") or d.get("home_name") or "Netatmo",
        "module": d.get("module_name", "Binnen"),
        "date_setup": int(d.get("date_setup") or 0),
        "type": device_type,
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


def netatmo_hourly_recent(access, device_id, days):
    now = int(time.time())
    body = _measure(access, device_id, "1hour", now - days * 86400)
    out = {}
    for ts_str, vals in body.items():
        if vals and vals[0] is not None:
            dt = datetime.fromtimestamp(int(ts_str), tz=timezone.utc).astimezone(TZ)
            out[dt.strftime("%Y-%m-%dT%H:00")] = round(float(vals[0]), 1)
    print(f"Netatmo uurdata: {len(out)} uren.")
    return out


# --------------------------------------------------------------------------
# 3. Open-Meteo: buitentemperatuur
# --------------------------------------------------------------------------
def openmeteo_daily(lat, lon, start_date):
    """Dagelijkse gemiddelde buitentemp: archief (oud) + forecast (recent)."""
    today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
    out = {}
    # Archief (loopt ~2-5 dagen achter)
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
    # Recente dagen opvullen via forecast
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


def openmeteo_hourly(lat, lon, days):
    p = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
         "past_days": min(days, 92), "forecast_days": 1, "timezone": "Europe/Brussels"}
    try:
        data = http_get(OPEN_METEO_FORECAST_URL + "?" + urllib.parse.urlencode(p))
    except urllib.error.HTTPError as e:
        die(f"Open-Meteo uur mislukt (HTTP {e.code}). Antwoord: {e.read().decode('utf-8', 'ignore')}")
    out = {}
    for t, temp in zip(data.get("hourly", {}).get("time", []),
                       data.get("hourly", {}).get("temperature_2m", [])):
        if temp is not None:
            out[t[:13] + ":00"] = round(float(temp), 1)
    print(f"Open-Meteo uurdata: {len(out)} uren.")
    return out


# --------------------------------------------------------------------------
# 4. Samenvoegen en wegschrijven
# --------------------------------------------------------------------------
def build_outputs(dev, in_daily, out_daily, in_hourly, out_hourly, lat, lon):
    os.makedirs(OUT_DIR, exist_ok=True)

    daily_keys = sorted(set(in_daily) | set(out_daily))
    daily = [{"d": k, "binnen": in_daily.get(k), "buiten": out_daily.get(k)} for k in daily_keys]

    hourly_keys = sorted(set(in_hourly) | set(out_hourly))
    hourly = [{"t": k, "binnen": in_hourly.get(k), "buiten": out_hourly.get(k)} for k in hourly_keys]

    data = {
        "station": dev["station"], "indoor_name": dev["module"], "device_type": dev["type"],
        "updated": datetime.now(tz=TZ).strftime("%d-%m-%Y %H:%M"),
        "lat": str(lat), "lon": str(lon),
        "hourly_from": hourly[0]["t"] if hourly else None,
        "daily": daily, "hourly": hourly,
    }
    with open(os.path.join(OUT_DIR, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # CSV = dagdata (handig voor Excel)
    with open(os.path.join(OUT_DIR, "data.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["datum", "binnen_gem_C", "buiten_gem_C"])
        w.writeheader()
        for row in daily:
            w.writerow({"datum": row["d"],
                        "binnen_gem_C": "" if row["binnen"] is None else row["binnen"],
                        "buiten_gem_C": "" if row["buiten"] is None else row["buiten"]})

    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(INDEX_HTML)

    print(f"Klaar: {len(daily)} dagen, {len(hourly)} uren -> public/index.html, data.json, data.csv.")


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Temperatuur: binnen vs. buiten (Cockerillkaai)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 0; padding: 24px; color: #1a1a1a; background: #fafafa; }
  .wrap { max-width: 1040px; margin: 0 auto; }
  .head { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  h1 { font-size: 19px; margin: 0; }
  .badge { color: #8a8a8a; font-size: 12px; }
  .controls { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin: 16px 0 8px; }
  .seg button { border: 1px solid #d0d0d0; background: #fff; padding: 6px 12px; cursor: pointer;
                font-size: 13px; }
  .seg button:first-child { border-radius: 6px 0 0 6px; }
  .seg button:last-child { border-radius: 0 6px 6px 0; }
  .seg button + button { border-left: none; }
  .seg button.active { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  select, input[type=date] { padding: 6px 8px; font-size: 13px; border: 1px solid #d0d0d0;
                             border-radius: 6px; background: #fff; }
  .note { color: #a15c00; font-size: 12px; min-height: 16px; margin: 2px 0 8px; }
  .card { background: #fff; border: 1px solid #e5e5e5; border-radius: 10px; padding: 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
  .foot { color: #888; font-size: 12px; margin-top: 16px; }
  a { color: #2563eb; }
  label { font-size: 13px; color: #555; }
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <h1 id="title">Binnen vs. buiten</h1>
    <span class="badge" id="updated"></span>
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
  <div class="card"><canvas id="grafiek" height="120"></canvas></div>
  <div class="foot">
    Buitentemperatuur: Open-Meteo (<span id="coord"></span>).
    &nbsp;|&nbsp; <a href="data.csv" download>Download dagdata (CSV)</a>
  </div>
</div>
<script>
let DATA = null, chart = null;
const $ = id => document.getElementById(id);
const LS = { get:(k,d)=>localStorage.getItem('ck_'+k)||d, set:(k,v)=>localStorage.setItem('ck_'+k,v) };

fetch('data.json', {cache:'no-store'})
  .then(r => r.json())
  .then(d => { DATA = d; boot(); })
  .catch(e => { $('note').textContent = 'Kon data niet laden: ' + e; });

function boot(){
  $('title').textContent = 'Binnentemperatuur (' + DATA.station + ' – ' + DATA.indoor_name + ') vs. buiten Cockerillkaai';
  $('coord').textContent = DATA.lat + ', ' + DATA.lon;
  $('updated').textContent = 'bijgewerkt: ' + DATA.updated;

  let g = LS.get('g', 'dag');
  document.querySelectorAll('#gran button').forEach(b => {
    b.classList.toggle('active', b.dataset.g === g);
    b.onclick = () => { setGran(b.dataset.g); };
  });
  $('period').value = LS.get('period', '30');
  $('from').value = LS.get('from', '');
  $('to').value = LS.get('to', '');
  $('period').onchange = () => { LS.set('period', $('period').value); toggleCustom(); render(); };
  $('from').onchange = () => { LS.set('from', $('from').value); render(); };
  $('to').onchange = () => { LS.set('to', $('to').value); render(); };
  toggleCustom();
  render();
}

function setGran(g){
  LS.set('g', g);
  document.querySelectorAll('#gran button').forEach(b => b.classList.toggle('active', b.dataset.g === g));
  render();
}
function toggleCustom(){
  $('customRange').style.display = $('period').value === 'custom' ? 'inline' : 'none';
}

function periodBounds(){
  const p = $('period').value;
  if (p === 'custom') return { start: $('from').value || '0000-01-01', end: $('to').value || '9999-12-31' };
  if (p === 'all')    return { start: '0000-01-01', end: '9999-12-31' };
  const days = parseInt(p, 10);
  const s = new Date(Date.now() - days*86400000);
  return { start: s.toISOString().slice(0,10), end: '9999-12-31' };
}

function avg(a){ return a.length ? Math.round(a.reduce((x,y)=>x+y,0)/a.length*10)/10 : null; }

function isoWeek(ds){
  const d = new Date(ds + 'T00:00:00Z');
  const day = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - day + 3);
  const firstThu = new Date(Date.UTC(d.getUTCFullYear(), 0, 4));
  const week = 1 + Math.round(((d - firstThu)/86400000 - 3 + ((firstThu.getUTCDay()+6)%7))/7);
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
  return {
    labels: keys.map(k => m.get(k).label),
    binnen: keys.map(k => avg(m.get(k).b)),
    buiten: keys.map(k => avg(m.get(k).o)),
  };
}

function buildSeries(){
  const { start, end } = periodBounds();
  const g = LS.get('g', 'dag');
  $('note').textContent = '';

  if (g === 'uur'){
    const pts = DATA.hourly.filter(p => p.t.slice(0,10) >= start && p.t.slice(0,10) <= end);
    if (DATA.hourly_from && start < DATA.hourly_from.slice(0,10)){
      $('note').textContent = 'Uurweergave is beschikbaar vanaf ' + DATA.hourly_from.slice(0,10) +
        '. Voor oudere periodes: kies Dag, Week of Maand.';
    }
    return {
      labels: pts.map(p => p.t.slice(8,10)+'-'+p.t.slice(5,7)+' '+p.t.slice(11,16)),
      binnen: pts.map(p => p.binnen),
      buiten: pts.map(p => p.buiten),
    };
  }

  const pts = DATA.daily.filter(p => p.d >= start && p.d <= end);
  if (g === 'dag')
    return { labels: pts.map(p => p.d.slice(8,10)+'-'+p.d.slice(5,7)+'-'+p.d.slice(0,4)),
             binnen: pts.map(p => p.binnen), buiten: pts.map(p => p.buiten) };
  if (g === 'week')
    return groupDaily(pts, d => { const w = isoWeek(d); return w.year + '-W' + String(w.week).padStart(2,'0'); },
                      d => { const w = isoWeek(d); return 'wk ' + w.week + ' ' + w.year; });
  // maand
  return groupDaily(pts, d => d.slice(0,7), d => d.slice(5,7) + '-' + d.slice(0,4));
}

function render(){
  const s = buildSeries();
  const cfg = {
    type: 'line',
    data: { labels: s.labels, datasets: [
      { label: 'Binnen (Netatmo)', data: s.binnen, borderColor: '#e11d48', backgroundColor:'#e11d48',
        tension: 0.25, spanGaps: true, pointRadius: 0, borderWidth: 2 },
      { label: 'Buiten (Cockerillkaai)', data: s.buiten, borderColor: '#2563eb', backgroundColor:'#2563eb',
        tension: 0.25, spanGaps: true, pointRadius: 0, borderWidth: 2 },
    ]},
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      scales: { x: { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
                y: { title: { display: true, text: 'Temperatuur (°C)' } } },
      plugins: { legend: { position: 'top' } },
    },
  };
  if (chart){ chart.data = cfg.data; chart.update(); }
  else chart = new Chart($('grafiek'), cfg);
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
    in_hourly = netatmo_hourly_recent(access, dev["id"], hourly_days)

    start_date = min(in_daily) if in_daily else (datetime.now(tz=TZ) - timedelta(days=hourly_days)).strftime("%Y-%m-%d")
    out_daily = openmeteo_daily(lat, lon, start_date)
    out_hourly = openmeteo_hourly(lat, lon, hourly_days)

    build_outputs(dev, in_daily, out_daily, in_hourly, out_hourly, lat, lon)


if __name__ == "__main__":
    main()
