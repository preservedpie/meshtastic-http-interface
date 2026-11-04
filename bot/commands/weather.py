"""Weather command — wttr.in (current + 1–3d) and Open-Meteo (7d)."""

import json
import urllib.request
import urllib.parse
from datetime import datetime

from .. import settings


WMO_CODES = {
    0: 'Clear', 1: 'Mostly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Rime fog',
    51: 'Light drizzle', 53: 'Drizzle', 55: 'Heavy drizzle',
    61: 'Light rain', 63: 'Rain', 65: 'Heavy rain',
    71: 'Light snow', 73: 'Snow', 75: 'Heavy snow', 77: 'Snow grains',
    80: 'Light showers', 81: 'Showers', 82: 'Heavy showers',
    85: 'Snow showers', 86: 'Heavy snow showers',
    95: 'Thunderstorm', 96: 'Thunderstorm+hail', 99: 'Thunderstorm+hail',
}


def _default_location() -> str:
    return settings.get('DEFAULT_LOCATION', 'New York') or 'New York'


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': 'MeshBot/1.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _wttr_current(location: str) -> str:
    url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
    data = _fetch_json(url)
    cur = data['current_condition'][0]
    nearest = data['nearest_area'][0]
    return (
        f"Now {nearest['areaName'][0]['value']},"
        f"{nearest['region'][0]['value']}: "
        f"{cur['weatherDesc'][0]['value']} {cur['temp_F']}F "
        f"(feels {cur['FeelsLikeF']}F) Hum:{cur['humidity']}% "
        f"Wind:{cur['windspeedMiles']}mph {cur['winddir16Point']} "
        f"Vis:{cur['visibility']}mi"
    )


def _wttr_forecast_days(location: str, num_days: int) -> str:
    url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
    data = _fetch_json(url)
    nearest = data['nearest_area'][0]
    label = f"{nearest['areaName'][0]['value']},{nearest['region'][0]['value']}"
    days = data['weather'][:num_days]

    lines = [f"Forecast {label}:"]
    for day in days:
        date = datetime.strptime(day['date'], '%Y-%m-%d')
        hi = day['maxtempF']
        lo = day['mintempF']
        desc = day['hourly'][4]['weatherDesc'][0]['value']
        chance_rain = day['hourly'][4].get('chanceofrain', '0')
        wind = day['hourly'][4]['windspeedMiles']
        lines.append(
            f"{date.strftime('%a %m/%d')}: {desc} Hi:{hi}F Lo:{lo}F "
            f"Rain:{chance_rain}% Wind:{wind}mph"
        )
    return '\n'.join(lines)


def _geocode(location: str):
    url = (f"https://geocoding-api.open-meteo.com/v1/search?"
           f"name={urllib.parse.quote(location)}&count=1&language=en&format=json")
    data = _fetch_json(url)
    results = data.get('results')
    if not results:
        raise ValueError(f"Location not found: {location}")
    r = results[0]
    label = f"{r.get('name','')},{r.get('admin1','') or r.get('country','')}"
    return r['latitude'], r['longitude'], label


def _openmeteo_7d(location: str) -> str:
    lat, lon, label = _geocode(location)
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=weathercode,temperature_2m_max,temperature_2m_min,"
        f"precipitation_probability_max,windspeed_10m_max"
        f"&temperature_unit=fahrenheit&windspeed_unit=mph"
        f"&precipitation_unit=inch&timezone=auto&forecast_days=7"
    )
    data = _fetch_json(url)
    daily = data['daily']
    lines = [f"7-Day {label}:"]
    for i, date_str in enumerate(daily['time']):
        date = datetime.strptime(date_str, '%Y-%m-%d')
        code = daily['weathercode'][i]
        desc = WMO_CODES.get(code, f"Code{code}")
        hi = daily['temperature_2m_max'][i]
        lo = daily['temperature_2m_min'][i]
        rain_pct = daily['precipitation_probability_max'][i] or 0
        wind = daily['windspeed_10m_max'][i]
        lines.append(
            f"{date.strftime('%a %m/%d')}: {desc} Hi:{hi:.0f}F Lo:{lo:.0f}F "
            f"Rain:{rain_pct:.0f}% Wind:{wind:.0f}mph"
        )
    return '\n'.join(lines)


_BAD_WTTR_KEYWORDS = (
    'thunder', 'storm', 'tornado', 'hurricane', 'blizzard',
    'heavy rain', 'heavy snow', 'freezing', 'sleet', 'hail',
    'heavy shower',
)
_ALERT_WIND_MPH = 30
_ALERT_RAIN_PCT = 60
_ALERT_SNOW_PCT = 40


def check_weather_alert(location=None):
    loc = location or _default_location()
    try:
        url = f"https://wttr.in/{urllib.parse.quote(loc)}?format=j1"
        data = _fetch_json(url)
        nearest = data['nearest_area'][0]
        label = f"{nearest['areaName'][0]['value']},{nearest['region'][0]['value']}"
        today = data['weather'][0]
        hi = today['maxtempF']
        lo = today['mintempF']

        alerts: list = []
        for hour in today['hourly']:
            desc = hour['weatherDesc'][0]['value'].lower()
            wind = int(hour.get('windspeedMiles', 0))
            rain_pct = int(hour.get('chanceofrain', 0))
            snow_pct = int(hour.get('chanceofsnow', 0))
            thunder_pct = int(hour.get('chanceofthunder', 0))
            for kw in _BAD_WTTR_KEYWORDS:
                if kw in desc and desc.title() not in alerts:
                    alerts.append(desc.title())
                    break
            if wind >= _ALERT_WIND_MPH and f"High wind {wind}mph" not in alerts:
                alerts.append(f"High wind {wind}mph")
            if rain_pct >= _ALERT_RAIN_PCT and f"Rain {rain_pct}%" not in alerts:
                alerts.append(f"Rain {rain_pct}%")
            if snow_pct >= _ALERT_SNOW_PCT and f"Snow {snow_pct}%" not in alerts:
                alerts.append(f"Snow {snow_pct}%")
            if thunder_pct >= 40 and f"Thunder {thunder_pct}%" not in alerts:
                alerts.append(f"Thunder {thunder_pct}%")

        if not alerts:
            return None
        return f"[WX ALERT] {label} today: {', '.join(alerts)} Hi:{hi}F Lo:{lo}F"
    except Exception:
        return None


def check_weather_alert_at(lat: float, lon: float):
    """Same alert check as check_weather_alert(), but for a node's own
    reported GPS position instead of the fixed DEFAULT_LOCATION setting —
    wttr.in accepts "lat,lon" directly as a location string."""
    return check_weather_alert(f"{lat:.4f},{lon:.4f}")


def weather_command(args: str, context: dict) -> str:
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ''
    rest = parts[1] if len(parts) > 1 else ''
    default = _default_location()

    SUBCOMMANDS = ('tomorrow', '3d', '7d')
    if sub in SUBCOMMANDS:
        location = rest.strip() or default
        mode = sub
    elif sub == 'forecast':
        location = rest.strip() or default
        mode = '3d'
    else:
        location = args.strip() or default
        mode = 'now'

    try:
        if mode == 'now':
            return _wttr_current(location)
        if mode == 'tomorrow':
            full = _wttr_forecast_days(location, 2)
            lines = full.split('\n')
            return '\n'.join([lines[0], lines[2]]) if len(lines) >= 3 else full
        if mode == '3d':
            return _wttr_forecast_days(location, 3)
        if mode == '7d':
            return _openmeteo_7d(location)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"Location not found: {location}"
        return f"Weather error: {e.code}"
    except urllib.error.URLError:
        return "Weather: Network error"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Weather error: {e}"
