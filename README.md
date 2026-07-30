# campground-watcher

A small, cron-friendly watcher that polls **recreation.gov** and **Washington
State Parks** (the GoingToCamp booking system) for campsite openings near you and
fires an instant notification when a site that matches your filters becomes
available. It is designed to catch **cancellations** for otherwise sold-out
weekends and surface a ready-to-click booking link the moment a spot frees up.

It is intentionally **not** LLM-driven: it is dependency-free Python you run on
a timer. It installs nothing and uses only the Python standard library.

> **Note:** the watcher *finds and notifies* about openings. It does not book for
> you (recreation.gov and WA State Parks both require your own login to reserve).
> You get an instant link; you click and book.

## Features

- Monitors both recreation.gov campgrounds and WA State Parks in one pass.
- **Occupancy cross-check for WA parks** — WA's map API reports walk-in / host /
  non-web-bookable sites as "available"; this watcher verifies each candidate
  against the booking site's `/api/occupancy` endpoint so you only get real,
  web-bookable openings (no phantom sites).
- **Single-site, whole-stay matching** — a "Fri+Sat" weekend match means *one
  site* is open for *both* nights. It never stitches different sites/nights
  together.
- **Target-weekend filter** — only alert for the specific weekend(s) you care
  about, or watch everything.
- **Per-site deep links** — each notification includes a booking URL pre-filled
  with the dates.
- **Change-only, idempotent, de-duplicated** notifications (won't spam you when
  WA availability flaps).
- Distance filtering, rating filter (recreation.gov), group-site exclusion.
- Adaptive polling and bounded scheduled logs.

## Requirements

- Python 3.10+. There are no packages to install and no package manager is used.

## Quick start

```bash
# 1. Set your home location (used to compute distance to each campground)
export CAMPWATCH_HOME_LAT=47.6062     # your latitude
export CAMPWATCH_HOME_LON=-122.3321   # your longitude

# 2. (Optional) regenerate the candidate lists for YOUR area:
./build_candidates.py   # private default: local 90 km distance filtering
./build_wa_parks.py     # WA State Parks within a ~2h drive
#    -> these write candidates.json / wa_parks.json, which feed watch_config.json.
#    (Shipped JSON files are tuned for the Seattle area; rebuild for elsewhere.)

# 3. Create your private trip-date file (it is ignored by Git)
cp watch_targets.example.json watch_targets.json
# Edit watch_targets.json as described below.

# 4. Run the watcher once
./watch.py
# Or perform a one-off 90-day scan without changing your private trip targets
./watch.py --all-once

# 5. Read what is currently bookable
./report.py             # everything open in the next 90 days
./weekend.py            # sites open THIS coming Fri+Sat
./weekend.py 2030-08-09 2030-08-10   # a specific weekend
```

Federal discovery supports two equivalent filtering modes. Both apply the same
local 90 km validation and report distances explicitly in kilometers and miles:

```bash
./build_candidates.py --distance-filter client  # default; coordinates stay local
./build_candidates.py --distance-filter server  # faster; sends coordinates to recreation.gov
./build_candidates.py --max-distance-km 120      # optional explicit km cutoff
```

Server mode is opt-in because recreation.gov interprets its `radius` and
returned `distance` values as kilometers and receives the configured coordinates.
The generated JSON lists contain distances derived from your home location, so
review them before committing; the checked-in lists use the public Seattle example.

## Configuration

Secrets and trip dates stay out of source code. Environment settings are:

| Variable | Default | Meaning |
|---|---|---|
| `CAMPWATCH_HOME_LAT` / `CAMPWATCH_HOME_LON` | Seattle | Your home coordinates for distance filtering. |
| `CAMPWATCH_WEBHOOK_URL` | *(none)* | Where to POST notifications (see below). If unset, openings are only written to `alerts.jsonl`. |
| `CAMPWATCH_WEBHOOK_TEXT_KEY` | `content` | JSON key for the human-readable text. `content` for Discord, `text` for Slack, `message` for ntfy. |
| `CAMPWATCH_NOTIFY` | `1` | Set to `0` to disable notifications entirely. |
| `CAMPWATCH_ALLOW_PRIVATE_WEBHOOK` | `0` | Set to `1` only when intentionally posting to a private/local HTTPS server. |
| `CAMPWATCH_LOG_MAX_BYTES` | `2097152` | Rotate the scheduled log at this size. |
| `CAMPWATCH_LOG_BACKUPS` | `4` | Number of old scheduled logs to retain. |

For a scheduled job, webhook settings can instead live in the Git-ignored
`secrets/config.json` file. The runner refuses to read it unless it has mode 600:

```json
{
  "home_lat": 47.0000,
  "home_lon": -122.0000,
  "webhook_url": "https://your-webhook-here",
  "webhook_text_key": "content",
  "notify": "1"
}
```

After creating it, run `chmod 600 secrets/config.json`; keep the `secrets`
directory mode 700. The webhook URL never needs to appear in a launchd or
systemd definition.

### Target weekend(s)

Copy `watch_targets.example.json` to `watch_targets.json`. The latter is ignored
by Git so future travel plans are not published. Each entry contains a label and
the **nights** (check-in dates) a stay must cover. A standard Fri→Sun booking is
the Friday night plus the Saturday night:

```json
{
  "watch_all": false,
  "weekends": [
    {"label": "summer weekend", "nights": ["2030-08-09", "2030-08-10"]}
  ]
}
```

- Empty/missing `weekends` pauses polling before any campground requests.
- `"watch_all": true` alerts on every qualifying opening.

### Notifications (webhook)

The watcher POSTs a JSON payload to `CAMPWATCH_WEBHOOK_URL` for each new opening.
Webhook URLs must use HTTPS, must resolve to public addresses by default, and
redirects are rejected. These checks prevent accidental plaintext delivery and
requests to local services.
The payload includes a human-readable text field (key configurable) plus
structured `sites` data with per-site booking URLs. This works out-of-the-box
with:

- **Discord** — use a channel webhook URL, keep `CAMPWATCH_WEBHOOK_TEXT_KEY=content`.
- **Slack** — incoming webhook URL, set `CAMPWATCH_WEBHOOK_TEXT_KEY=text`.
- **ntfy.sh** — topic URL, set `CAMPWATCH_WEBHOOK_TEXT_KEY=message`.
- Anything else that accepts a JSON POST.

## Running on a timer

Schedule `run_watch.py` every five minutes. It obtains a non-blocking native file
lock, so runs never overlap, then `watch.py` decides whether a network poll is
due:

- next trip within 7 days: every 10 minutes;
- 8–30 days away: every 30 minutes;
- more than 30 days away: every 60 minutes;
- outside the 90-day search window: one local check per day, no API polling;
- no future targets: paused, with one status line per day.

Changing `watch_targets.json` makes the next scheduler tick run immediately.
The runner keeps the current `cron.log` plus four 2 MiB backups by default. This
retains recent detail while putting a hard bound on old log storage.

### macOS (launchd)

Use the absolute path returned by `command -v python3`; Homebrew commonly uses
`/opt/homebrew/bin/python3`. A LaunchAgent can run these arguments every 300
seconds:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.local.campground-watcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/absolute/path/to/campground-watcher/run_watch.py</string>
  </array>
  <key>StartInterval</key>
  <integer>300</integer>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
```

### Linux (systemd)

Example systemd user units:

`~/.config/systemd/user/campground-watcher.service`
```ini
[Unit]
Description=Campground availability watcher (one tick)

[Service]
Type=oneshot
TimeoutStartSec=900
ExecStart=/absolute/path/to/python3 %h/campground-watcher/run_watch.py
```

`~/.config/systemd/user/campground-watcher.timer`
```ini
[Unit]
Description=Check whether the campground watcher is due every 5 min

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now campground-watcher.timer
```

The native lock prevents overlap even if a poll runs longer than the five-minute
scheduler interval.

## Files

| File | Purpose |
|---|---|
| `watch.py` | The watcher. Polls, filters, diffs against last state, notifies. |
| `campwatch_config.py` | Safely loads owner-only, Git-ignored local settings. |
| `campwatch_http.py` | Size-limited, allow-listed standard-library HTTPS clients. |
| `watch_config.json` | The list of campgrounds to watch (recreation.gov + WA). |
| `watch_targets.json` | Private, ignored trip dates (created by you). |
| `watch_targets.example.json` | Safe empty template for trip dates. |
| `build_candidates.py` | Regenerate the recreation.gov candidate list for your area. |
| `build_wa_parks.py` | Regenerate the WA State Parks list within a drive radius. |
| `discover_gtc.py` | Helper to list GoingToCamp facility/map IDs. |
| `candidates.json` / `wa_parks.json` / `gtc_campgrounds.json` | Reference data. |
| `report.py` | Print everything currently bookable, ranked by distance. |
| `weekend.py` | Print sites open for a given weekend (single-site, whole-stay). |
| `run_watch.py` | Portable overlap lock, adaptive runner, and log rotation. |
| `run_watch.sh` | Compatibility launcher that locates Python by absolute path. |
| `test_watch.py` | Tests (`./test_watch.py`). |

## Notes & limitations

- WA State Parks occasionally returns HTTP 403 (Azure WAF) for a park on a given
  run; the watcher skips that park for that tick and preserves prior state.
- recreation.gov popular campgrounds sometimes release cancellations one night at
  a time, so a single site may not always cover a full weekend — that's why the
  weekend filter requires one site to cover *all* requested nights.
- WA State Parks availability is read directly from GoingToCamp's public JSON
  endpoints and fails closed if the authoritative occupancy check is unavailable.
- GoingToCamp currently rejects Python's default user agent at its web firewall,
  so the client sends a static browser-compatible user agent. It is not a token
  or identity, but it is an upstream compatibility dependency: a WAF change can
  cause a poll to fail closed until the header is updated.

## License

MIT — see [LICENSE](LICENSE).
