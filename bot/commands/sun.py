"""Sun command — sunrise / sunset for a location."""

import urllib.request
import urllib.parse
import json
from datetime import datetime

from .. import settings


def sun_command(args: str, context: dict) -> str:
    location = args.strip() or settings.get('DEFAULT_LOCATION', 'New York')

    try:
        geo_url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
        req = urllib.request.Request(geo_url, headers={'User-Agent': 'MeshBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            geo = json.loads(resp.read().decode('utf-8'))

        nearest = geo['nearest_area'][0]
        lat = nearest['latitude']
        lon = nearest['longitude']
        area_name = nearest['areaName'][0]['value']
        country = nearest['country'][0]['value']

        sun_url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&formatted=0"
        req2 = urllib.request.Request(sun_url, headers={'User-Agent': 'MeshBot/1.0'})
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            sun = json.loads(resp2.read().decode('utf-8'))

        results = sun['results']
        rise = datetime.fromisoformat(results['sunrise'].replace('Z', '+00:00'))
        sset = datetime.fromisoformat(results['sunset'].replace('Z', '+00:00'))
        day_len = results['day_length']
        hours = day_len // 3600
        minutes = (day_len % 3600) // 60

        return "\n".join([
            f"Sun for {area_name}, {country}:",
            f"Rise: {rise.strftime('%H:%M')} UTC",
            f"Set:  {sset.strftime('%H:%M')} UTC",
            f"Day:  {hours}h {minutes}m",
        ])
    except urllib.error.URLError:
        return "Sun: Network error"
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return f"Sun: Parse error ({e})"
    except Exception as e:
        return f"Sun error: {e}"
