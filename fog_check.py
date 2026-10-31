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
    hours = []
    for i, t in enumerate(hourly["time"]):
        when = datetime.fromisoformat(t)
        if start <= when <= end:
            vis = hourly["visibility"][i]
            rh = hourly["relative_humidity_2m"][i]
            wind = hourly["wind_speed_10m"][i]
            label = classify(hourly["weather_code"][i], vis, rh, wind)
            hours.append({"time": when, "label": label, "vis": vis, "rh": rh})
    return sunrise, hours


def hm(t):
    """6:00 instead of 06:00, shorter on the watch."""
    return f"{t.hour}:{t.minute:02d}"


def format_hour(h):
    """Short line for the watch, e.g. '6:00 Fog 300m 99%' (time, label, visibility, humidity)."""
    if h["vis"] is None:
        vis = "n/a"
    elif h["vis"] < 1000:
        vis = f"{round(h['vis'])}m"
    elif h["vis"] < 10000:
        vis = f"{h['vis'] / 1000:.1f}km"
    else:
        vis = f"{round(h['vis'] / 1000)}km"
    label = h["label"] or "no fog"
    return f"{hm(h['time'])} {label} {vis} {h['rh']}%"


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

    sections = []
    all_hits = []
    for name, lat, lon in SPOTS:
        sunrise, hours = hours_around_sunrise(fetch_forecast(lat, lon), now)
        hits = [h for h in hours if h["label"]]
        all_hits.extend(hits)
        shown = hours if always_notify else hits
        if shown:
            lines = [name] if len(SPOTS) > 1 else []
            lines += [f"Sunrise {hm(sunrise)}"] + [format_hour(h) for h in shown]
            sections.append("\n".join(lines))

    # Short title so it fits on a watch: "Fog 6:00", "Mist 7:00" or "No fog"
    fog_hits = [h for h in all_hits if h["label"] == "Fog"]
    first = min(fog_hits or all_hits, key=lambda h: h["time"], default=None)
    if first:
        title = f"{first['label']} {hm(first['time'])}"
    else:
        title = "No fog"
    message = "\n\n".join(sections)

    print(title)
    print(message)

    if not all_hits and not always_notify:
        print("Nothing to send.")
        return
    if not topic:
        sys.exit("NTFY_TOPIC is not set.")
    send(topic, title, message)
    print("Notification sent.")


if __name__ == "__main__":
    main()
