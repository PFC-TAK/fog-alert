#!/usr/bin/env python3
"""Evening fog check for photography, citywide.

Looks at the hourly forecast around the next sunrise for each spot in Köln
and sends a push notification via ntfy.sh listing where fog or mist is likely.

Spots and thresholds live in config.json, shared with the morning page (index.html).
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))

SPOTS = [(s["name"], s["lat"], s["lon"]) for s in CONFIG["spots"]]
TIMEZONE = CONFIG["timezone"]
MINUTES_BEFORE_SUNRISE = CONFIG["minutes_before_sunrise"]
MINUTES_AFTER_SUNRISE = CONFIG["minutes_after_sunrise"]

# Fog: fog code, visibility under the limit, or air saturated (temperature
# within spread_c of the dew point), calm and low cloud at the ground.
FOG_VISIBILITY_M = CONFIG["fog"]["visibility_m"]
FOG_SPREAD_C = CONFIG["fog"]["spread_c"]
FOG_WIND_KMH = CONFIG["fog"]["wind_kmh"]
FOG_LOW_CLOUD_PERCENT = CONFIG["fog"]["low_cloud_percent"]

# Mist: visibility under the limit, or temperature within spread_c of the
# dew point and wind at most wind_kmh.
MIST_VISIBILITY_M = CONFIG["mist"]["visibility_m"]
MIST_SPREAD_C = CONFIG["mist"]["spread_c"]
MIST_WIND_KMH = CONFIG["mist"]["wind_kmh"]

FOG_CODES = {45, 48}  # WMO weather codes: fog, depositing rime fog
API_URL = "https://api.open-meteo.com/v1/forecast"
MODEL = "icon_seamless"  # DWD ICON, uses the 2 km ICON-D2 for the next two days
NTFY_URL = "https://ntfy.sh/"
HOURLY = ["weather_code", "temperature_2m", "dew_point_2m", "wind_speed_10m", "cloud_cover_low"]


def fetch_forecasts(spots):
    """One request for all spots. Returns one forecast per spot, same order."""
    def get(hourly):
        params = {
            "latitude": ",".join(str(lat) for _, lat, _ in spots),
            "longitude": ",".join(str(lon) for _, _, lon in spots),
            "hourly": ",".join(hourly),
            "daily": "sunrise",
            "timezone": TIMEZONE,
            "forecast_days": 3,
            "models": MODEL,
        }
        url = API_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.load(resp)
        return data if isinstance(data, list) else [data]

    try:
        return get(HOURLY + ["visibility"])
    except urllib.error.HTTPError as e:
        # DWD is dropping visibility; if Open-Meteo refuses it, ask without it.
        if e.code != 400:
            raise
        return get(HOURLY)


def classify(h):
    code, vis, wind = h["code"], h["vis"], h["wind"]
    spread = None
    if h["temp"] is not None and h["dew"] is not None:
        spread = h["temp"] - h["dew"]
    calm = lambda limit: wind is not None and wind <= limit

    if code in FOG_CODES or (vis is not None and vis < FOG_VISIBILITY_M):
        return "Fog"
    if (spread is not None and spread <= FOG_SPREAD_C and calm(FOG_WIND_KMH)
            and h["low"] is not None and h["low"] >= FOG_LOW_CLOUD_PERCENT):
        return "Fog"
    if vis is not None and vis < MIST_VISIBILITY_M:
        return "Mist"
    if spread is not None and spread <= MIST_SPREAD_C and calm(MIST_WIND_KMH):
        return "Mist"
    return None


def hours_around_sunrise(data, now):
    """Return (sunrise, list of hours) for the window around the next sunrise after `now`.

    Using the next sunrise (instead of "tomorrow's date") keeps it correct
    even if the run starts late, after midnight.
    """
    sunrises = [datetime.fromisoformat(s) for s in data["daily"]["sunrise"]]
    sunrise = next(s for s in sunrises if s > now)
    start = sunrise - timedelta(minutes=MINUTES_BEFORE_SUNRISE)
    end = sunrise + timedelta(minutes=MINUTES_AFTER_SUNRISE)

    hourly = data["hourly"]
    col = lambda name, i: hourly.get(name, [None] * len(hourly["time"]))[i]
    hours = []
    for i, t in enumerate(hourly["time"]):
        when = datetime.fromisoformat(t)
        if start <= when <= end:
            h = {
                "time": when,
                "code": col("weather_code", i),
                "vis": col("visibility", i),
                "temp": col("temperature_2m", i),
                "dew": col("dew_point_2m", i),
                "wind": col("wind_speed_10m", i),
                "low": col("cloud_cover_low", i),
            }
            h["label"] = classify(h)
            hours.append(h)
    return sunrise, hours


def hm(t):
    """6:00 instead of 06:00, shorter on the watch."""
    return f"{t.hour}:{t.minute:02d}"


def group_by_grid(spots, forecasts):
    """Spots in the same forecast square get identical data, so list them together."""
    groups = {}
    for (name, _, _), data in zip(spots, forecasts):
        key = (round(data["latitude"], 3), round(data["longitude"], 3))
        if key in groups:
            groups[key]["names"].append(name)
        else:
            groups[key] = {"names": [name], "data": data}
    return list(groups.values())


def describe(group, hits):
    """One short line, e.g. 'Poller Wiesen: Fog 6-8h'."""
    names = ", ".join(group["names"])
    if not hits:
        return f"{names}: clear"
    label = "Fog" if any(h["label"] == "Fog" for h in hits) else "Mist"
    first, last = hits[0]["time"].hour, hits[-1]["time"].hour
    span = f"{first}h" if first == last else f"{first}-{last}h"
    return f"{names}: {label} {span}"


def send(topic, title, message):
    req = urllib.request.Request(
        NTFY_URL + topic,
        data=message.encode("utf-8"),
        headers={"Title": title},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def main():
    always_notify = os.environ.get("ALWAYS_NOTIFY", "").lower() == "true"
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    # Local time without timezone info, to compare with the forecast times
    now = datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)

    results = []  # (label, first hit time, line, number of spots)
    sunrise = None
    for group in group_by_grid(SPOTS, fetch_forecasts(SPOTS)):
        sunrise, hours = hours_around_sunrise(group["data"], now)
        hits = [h for h in hours if h["label"]]
        if hits or always_notify:
            label = None
            if hits:
                label = "Fog" if any(h["label"] == "Fog" for h in hits) else "Mist"
            first = hits[0]["time"] if hits else datetime.max
            results.append((label, first, describe(group, hits), len(group["names"])))

    # Fog spots first, then mist, then clear; earliest first within each
    rank = {"Fog": 0, "Mist": 1, None: 2}
    results.sort(key=lambda r: (rank[r[0]], r[1]))

    fog = [r for r in results if r[0] == "Fog"]
    mist = [r for r in results if r[0] == "Mist"]
    best = fog or mist
    if best:
        label = best[0][0]
        count = sum(r[3] for r in best)
        if count == 1:
            title = f"{label}: {best[0][2].split(':')[0]}"
        else:
            title = f"{label}: {count} spots"
    else:
        title = "No fog"

    lines = [r[2] for r in results]
    if sunrise:
        lines.append(f"Sunrise {hm(sunrise)}")
    message = "\n".join(lines)

    print(title)
    print(message)

    if not best and not always_notify:
        print("Nothing to send.")
        return
    if not topic:
        sys.exit("NTFY_TOPIC is not set.")
    send(topic, title, message)
    print("Notification sent.")


if __name__ == "__main__":
    main()
