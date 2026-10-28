#!/usr/bin/env python3
"""Evening fog check for photography.

Looks at the hourly forecast around tomorrow's sunrise for each spot and
sends a push notification via ntfy.sh if fog or mist looks likely.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# --- Settings you might want to change ---------------------------------

SPOTS = [
    # (name, latitude, longitude)
    ("Ehrenfeld", 50.953, 6.920),
]

TIMEZONE = "Europe/Berlin"
MINUTES_BEFORE_SUNRISE = 90
MINUTES_AFTER_SUNRISE = 90

FOG_VISIBILITY_M = 1000   # visibility below this counts as fog
MIST_VISIBILITY_M = 3000  # visibility below this counts as mist
HUMID_PERCENT = 95        # this humid ...
CALM_WIND_KMH = 7         # ... and this calm also counts as mist

# -----------------------------------------------------------------------

FOG_CODES = {45, 48}  # WMO weather codes: fog, depositing rime fog
API_URL = "https://api.open-meteo.com/v1/forecast"
NTFY_URL = "https://ntfy.sh/"


def fetch_forecast(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "weather_code,visibility,relative_humidity_2m,wind_speed_10m",
        "daily": "sunrise",
        "timezone": TIMEZONE,
        "forecast_days": 3,
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def classify(code, vis, humidity, wind):
    if code in FOG_CODES or (vis is not None and vis < FOG_VISIBILITY_M):
        return "Fog"
    if vis is not None and vis < MIST_VISIBILITY_M:
        return "Mist"
    if humidity is not None and wind is not None and humidity >= HUMID_PERCENT and wind <= CALM_WIND_KMH:
        return "Mist"
    return None


def hours_around_sunrise(data, day):
    """Return (sunrise, list of hours) for the window around sunrise on `day`."""
    daily = data["daily"]
    sunrise = datetime.fromisoformat(daily["sunrise"][daily["time"].index(day.isoformat())])
    start = sunrise - timedelta(minutes=MINUTES_BEFORE_SUNRISE)
    end = sunrise + timedelta(minutes=MINUTES_AFTER_SUNRISE)

    hourly = data["hourly"]
    hours = []
    for i, t in enumerate(hourly["time"]):
        when = datetime.fromisoformat(t)
        if start <= when <= end:
            vis = hourly["visibility"][i]
            rh = hourly["relative_humidity_2m"][i]
            wind = hourly["wind_speed_10m"][i]
            label = classify(hourly["weather_code"][i], vis, rh, wind)
            hours.append({"time": when, "label": label, "vis": vis, "rh": rh, "wind": wind})
    return sunrise, hours


def format_hour(h):
    if h["vis"] is None:
        vis = "vis n/a"
    elif h["vis"] < 1000:
        vis = f"vis {round(h['vis'])} m"
    else:
        vis = f"vis {h['vis'] / 1000:.1f} km"
    label = h["label"] or "no fog"
    return f"{h['time']:%H:%M} {label}, {vis}, {h['rh']}% humidity, wind {h['wind']} km/h"


def send(topic, title, message):
    req = urllib.request.Request(
        NTFY_URL + topic,
        data=message.encode("utf-8"),
        headers={"Title": title, "Tags": "fog"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def main():
    always_notify = os.environ.get("ALWAYS_NOTIFY", "").lower() == "true"
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    tomorrow = datetime.now(ZoneInfo(TIMEZONE)).date() + timedelta(days=1)

    sections = []
    found = set()
    for name, lat, lon in SPOTS:
        sunrise, hours = hours_around_sunrise(fetch_forecast(lat, lon), tomorrow)
        hits = [h for h in hours if h["label"]]
        found.update(h["label"] for h in hits)
        shown = hours if always_notify else hits
        if shown:
            lines = [f"{name}, sunrise {sunrise:%H:%M}"] + [format_hour(h) for h in shown]
            sections.append("\n".join(lines))

    if "Fog" in found:
        title = "Fog tomorrow morning"
    elif "Mist" in found:
        title = "Mist possible tomorrow morning"
    else:
        title = "No fog tomorrow (test run)"
    message = "\n\n".join(sections)

    print(title)
    print(message)

    if not found and not always_notify:
        print("Nothing to send.")
        return
    if not topic:
        sys.exit("NTFY_TOPIC is not set.")
    send(topic, title, message)
    print("Notification sent.")


if __name__ == "__main__":
    main()
