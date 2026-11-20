# Köln Fog Check

Evening fog alert and a morning status page for fog photography (large format, 4x5) around Köln.
Owner: Felix. He edits through the GitHub website and checks results on his iPhone and Garmin watch.

## How it runs

- **Trigger:** cron-job.org sends a `workflow_dispatch` to `.github/workflows/fog-check.yml` every day at 20:00 Europe/Berlin, using a fine-grained token (Actions read/write on this repo only). GitHub's own `schedule:` ran up to 4 hours late, so don't add it back.
- **`fog_check.py`** (Python stdlib only):
  - Makes one Open-Meteo request (`models=icon_seamless`, i.e. 2 km ICON-D2 for the next ~48 h) for every spot in `config.json`, plus the Köln/Bonn airport point.
  - Looks at hours from 90 min before to 90 min after the next sunrise and labels each Fog / Mist / clear.
  - Adds sky after sunrise: "sun" / "some sun" / "grey", from mid/high cloud.
  - Adds "rime" when a fog hour is ≤ 0 °C or has weather code 48.
  - Groups spots that land in the same forecast grid cell.
  - Sends an ntfy push only when fog/mist is expected. The topic is in the `NTFY_TOPIC` secret.
  - Then updates `history.csv`: the forecast for the airport point, plus what the DWD station there observed on past mornings (Bright Sky API). The workflow commits that file.
- **`index.html`**: static page on GitHub Pages (https://pfc-tak.github.io/fog-alert/), installed as an iPhone home-screen web app.
  - In the browser it fetches Open-Meteo and Bright Sky live, and reads `config.json` and `history.csv` (from raw.githubusercontent.com).
  - Shows two tabs (the next two mornings), a Leaflet map (1.9.4 from unpkg, OpenStreetMap tiles; CARTO now needs an API key), the observation at the airport, per-spot cards and the track record.
  - Icons: `favicon.svg` and `apple-touch-icon.png`.
- **`config.json`** is the single source of truth for spots (name, lat, lon, area) and thresholds. The script and the page both read it.

## Rules

- The fog/mist/sky/rime logic exists twice, in `fog_check.py` and in `index.html`. Change both together and keep them identical.
- Notifications are mirrored to a Garmin Instinct with a tiny screen. Keep the title short ("Fog: 3 spots", "Fog + rime: Poller Wiesen") and the lines short ("Poller Wiesen: Fog 6-8h, sun"). No emoji and no ntfy tags: they show up as garbage on the watch.
- The repo is public. Never commit the ntfy topic, the cron-job.org token or Felix's home location.
- DWD visibility on Open-Meteo is being phased out (announced for November 2026). Keep the retry-without-`visibility` on HTTP 400, in both the script and the page.
- The history step must never block or delay the alert. It runs after sending, inside try/except.
- No dependencies and no build step: plain Python in the workflow, plain HTML/JS on the page.
- Felix merges changes on GitHub himself. Keep changes small, and tell him what to look for on the phone or watch afterwards.

## Testing

- The real end-to-end test: Actions → Fog check → Run workflow, with "testing" ticked. That sends a notification listing every spot even when there's no fog, and updates `history.csv`.
- Offline: mock `fetch_forecasts`, `send` and `fetch_observations` in `fog_check.py`. Open-Meteo, ntfy and Bright Sky may be unreachable from a sandbox. Remember that `fetch_forecasts` returns the spots plus one extra entry for the airport point.
- Page: serve the folder (`python3 -m http.server`) and open it, or use Playwright with the API calls routed to fake JSON. Check it at phone width, in both dark and light mode.

## Known limits

- The forecast grid is about 2 km, so spots closer together than that get identical data.
- Real observations exist only at Köln/Bonn airport, so the track record compares only that point.
- The second-morning tab is a heads-up only. When it's more than ~45 h ahead, the data partly comes from the 7 km ICON-EU.
- The "Grüngürtel" spot sits in the outer green belt near Müngersdorf. Felix hasn't confirmed that's the stretch he meant.
