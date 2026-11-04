#!/usr/bin/env python3
"""Evening fog check for photography, citywide.

Looks at the hourly forecast around the next sunrise for each spot in Köln
and sends a push notification via ntfy.sh listing where fog or mist is likely,
whether the sun is likely to shine on it, and whether rime is possible.

It also keeps history.csv: the forecast for the airport point, and what the DWD
station there actually observed that morning, so the hit rate can be checked.

Spots and thresholds live in config.json, shared with the morning page (index.html).
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
HISTORY_PATH = HERE / "history.csv"

SPOTS = [(s["name"], s["lat"], s["lon"]) for s in CONFIG["spots"]]
OBS = CONFIG["observation"]
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

# Sky above the fog after sunrise: mid/high cloud (whichever is more) on average
# at most sun_max_percent means sun, at least grey_min_percent means grey.
SUN_MAX_PERCENT = CONFIG["sky"]["sun_max_percent"]
GREY_MIN_PERCENT = CONFIG["sky"]["grey_min_percent"]
RIME_MAX_TEMP_C = CONFIG["rime_max_temp_c"]

FOG_CODES = {45, 48}  # WMO weather codes: fog, depositing rime fog
RIME_CODE = 48
API_URL = "https://api.open-meteo.com/v1/forecast"
OBS_URL = "https://api.brightsky.dev/weather"
MODEL = "icon_seamless"  # DWD ICON, uses the 2 km ICON-D2 for the next two days
NTFY_URL = "https://ntfy.sh/"
HOURLY = ["weather_code", "temperature_2m", "dew_point_2m", "wind_speed_10m",
          "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"]
HISTORY_FIELDS = ["date", "sunrise", "forecast", "spots_fog", "spots_mist", "observed", "min_visibility_m"]


def fetch_forecasts(points):
    """One request for all points. Returns one forecast per point, same order."""
    def get(hourly):
        params = {
            "latitude": ",".join(str(lat) for _, lat, _ in points),
            "longitude": ",".join(str(lon) for _, _, lon in points),
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
            and h.get("low") is not None and h["low"] >= FOG_LOW_CLOUD_PERCENT):
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
                "mid": col("cloud_cover_mid", i),
                "high": col("cloud_cover_high", i),
            }
            h["label"] = classify(h)
            hours.append(h)
    return sunrise, hours


def sky_after_sunrise(hours, sunrise):
    """'sun', 'some sun' or 'grey' from mid/high cloud between sunrise and the end of the window."""
    covers = [max(h["mid"] or 0, h["high"] or 0) for h in hours
              if h["time"] >= sunrise.replace(minute=0) and (h["mid"] is not None or h["high"] is not None)]
    if not covers:
        return None
    avg = sum(covers) / len(covers)
    if avg <= SUN_MAX_PERCENT:
        return "sun"
    if avg >= GREY_MIN_PERCENT:
        return "grey"
    return "some sun"


def rime_likely(hits):
    return any(h["code"] == RIME_CODE or (h["temp"] is not None and h["temp"] <= RIME_MAX_TEMP_C)
               for h in hits)


def analyze(names, data, now):
    sunrise, hours = hours_around_sunrise(data, now)
    hits = [h for h in hours if h["label"]]
    label = None
    if hits:
        label = "Fog" if any(h["label"] == "Fog" for h in hits) else "Mist"
    return {
        "names": names, "sunrise": sunrise, "hours": hours, "hits": hits, "label": label,
        "sky": sky_after_sunrise(hours, sunrise) if hits else None,
        "rime": rime_likely(hits),
    }


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


def describe(row):
    """One short line, e.g. 'Poller Wiesen: Fog 6-8h, sun, rime'."""
    names = ", ".join(row["names"])
    if not row["hits"]:
        return f"{names}: clear"
    first, last = row["hits"][0]["time"].hour, row["hits"][-1]["time"].hour
    span = f"{first}h" if first == last else f"{first}-{last}h"
    extras = [x for x in (row["sky"], "rime" if row["rime"] else None) if x]
    return f"{names}: {row['label']} {span}" + "".join(", " + x for x in extras)


def send(topic, title, message):
    req = urllib.request.Request(
        NTFY_URL + topic,
        data=message.encode("utf-8"),
        headers={"Title": title},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


# --- Hit rate ------------------------------------------------------------

def fetch_observations(day, sunrise_hm):
    """Hourly DWD observations at the airport for the window around sunrise on `day`."""
    sunrise = datetime.fromisoformat(f"{day}T{sunrise_hm}")
    start = sunrise - timedelta(minutes=MINUTES_BEFORE_SUNRISE)
    end = sunrise + timedelta(minutes=MINUTES_AFTER_SUNRISE)
    params = {"lat": OBS["lat"], "lon": OBS["lon"], "tz": TIMEZONE,
              "date": start.strftime("%Y-%m-%dT%H:%M"), "last_date": end.strftime("%Y-%m-%dT%H:%M")}
    with urllib.request.urlopen(OBS_URL + "?" + urllib.parse.urlencode(params), timeout=30) as resp:
        data = json.load(resp)
    forecast_ids = {s["id"] for s in data.get("sources", []) if s.get("observation_type") == "forecast"}
    return [w for w in data.get("weather", []) if w.get("source_id") not in forecast_ids]


def observed_label(records):
    """('Fog' | 'Mist' | 'clear', lowest visibility) from DWD observation records, or (None, None)."""
    if not records:
        return None, None
    labels = []
    for w in records:
        labels.append(classify({
            "code": 45 if w.get("condition") == "fog" else None, "vis": w.get("visibility"),
            "temp": w.get("temperature"), "dew": w.get("dew_point"), "wind": w.get("wind_speed"),
        }))
    vis = [w["visibility"] for w in records if w.get("visibility") is not None]
    label = "Fog" if "Fog" in labels else "Mist" if "Mist" in labels else "clear"
    return label, (min(vis) if vis else None)


def update_history(path, now, forecast_row):
    """Record the forecast for the coming morning and fill in what was observed on recent mornings."""
    rows = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            rows = {r["date"]: r for r in csv.DictReader(f)}

    new = rows.get(forecast_row["date"], {})
    new.update(forecast_row)
    rows[forecast_row["date"]] = {k: new.get(k, "") for k in HISTORY_FIELDS}

    for day, row in rows.items():
        if row["observed"] or not row["sunrise"]:
            continue
        window_end = datetime.fromisoformat(f"{day}T{row['sunrise']}") + timedelta(minutes=MINUTES_AFTER_SUNRISE)
        if window_end > now or date.fromisoformat(day) < now.date() - timedelta(days=3):
            continue  # morning not over yet, or too old to look up
        label, min_vis = observed_label(fetch_observations(day, row["sunrise"]))
        if label:
            row["observed"] = label
            row["min_visibility_m"] = "" if min_vis is None else str(round(min_vis))

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        for day in sorted(rows):
            writer.writerow(rows[day])


# --- Main ----------------------------------------------------------------

def main():
    always_notify = os.environ.get("ALWAYS_NOTIFY", "").lower() == "true"
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    # Local time without timezone info, to compare with the forecast times
    now = datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)

    forecasts = fetch_forecasts(SPOTS + [(OBS["name"], OBS["lat"], OBS["lon"])])
    airport, forecasts = forecasts[-1], forecasts[:-1]

    rows = [analyze(g["names"], g["data"], now) for g in group_by_grid(SPOTS, forecasts)]
    sunrise = rows[0]["sunrise"]

    # Fog first, then mist, then clear; within each, sunny first, then earliest
    rank = {"Fog": 0, "Mist": 1, None: 2}
    sky_rank = {"sun": 0, "some sun": 1, None: 2, "grey": 3}
    rows.sort(key=lambda r: (rank[r["label"]], sky_rank[r["sky"]],
                             r["hits"][0]["time"] if r["hits"] else datetime.max))
    shown = [r for r in rows if r["label"] or always_notify]

    best = [r for r in rows if r["label"] == "Fog"] or [r for r in rows if r["label"] == "Mist"]
    if best:
        label = best[0]["label"] + (" + rime" if any(r["rime"] for r in best) else "")
        count = sum(len(r["names"]) for r in best)
        title = f"{label}: {best[0]['names'][0]}" if count == 1 else f"{label}: {count} spots"
    else:
        title = "No fog"

    message = "\n".join([describe(r) for r in shown] + [f"Sunrise {hm(sunrise)}"])
    print(title)
    print(message)

    if best or always_notify:
        if not topic:
            sys.exit("NTFY_TOPIC is not set.")
        send(topic, title, message)
        print("Notification sent.")
    else:
        print("Nothing to send.")

    # The hit-rate log must never get in the way of the alert itself
    try:
        a = analyze([OBS["name"]], airport, now)
        update_history(HISTORY_PATH, now, {
            "date": a["sunrise"].date().isoformat(),
            "sunrise": a["sunrise"].strftime("%H:%M"),
            "forecast": a["label"] or "clear",
            "spots_fog": str(sum(len(r["names"]) for r in rows if r["label"] == "Fog")),
            "spots_mist": str(sum(len(r["names"]) for r in rows if r["label"] == "Mist")),
        })
        print("History updated.")
    except Exception as e:  # noqa: BLE001
        print(f"History not updated: {e}")


if __name__ == "__main__":
    main()
