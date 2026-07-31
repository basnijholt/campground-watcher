# campground-watcher

A small, cron-friendly watcher that polls **recreation.gov** and **Washington
State Parks** (the GoingToCamp booking system) for campsite openings near you.
When a completed poll finds a new matching opening, it records it and, if you
configure a webhook, sends a notification. It is designed to catch
**cancellations** for otherwise sold-out weekends and surface the provider's
availability page as soon as the next poll observes a spot.

It is intentionally **not** LLM-driven: it is dependency-free Python you run on
a timer. It installs nothing and uses only the Python standard library.

> **Note:** the watcher *finds and notifies* about openings. It does not book for
> you (recreation.gov and WA State Parks both require your own login to reserve).
> Provider links start a booking with the displayed observed dates; they are not
> a confirmation that a particular site can be reserved.

## Features

- Monitors both recreation.gov campgrounds and WA State Parks in one pass.
- **Occupancy filter for WA parks** — WA's map API reports walk-in / host /
  other non-reservable resources as "available"; the watcher checks candidates
  against the booking site's `/api/occupancy` endpoint to remove many of those
  false positives. It is not a complete reservation-policy validation.
- **Single-site, whole-stay matching** — a "Fri+Sat" weekend match means *one
  site* is open for *both* nights. It never stitches different sites/nights
  together.
- **Target-weekend filter** — only alert for the specific weekend(s) you care
  about, or watch everything.
- **Provider booking links** — Washington can pre-fill selected stay dates at the
  facility; recreation.gov opens the campground availability page. Neither is a
  booking confirmation.
- **Change-only, idempotent, de-duplicated** notifications (won't spam you when
  WA availability flaps).
- Distance filtering, rating filter (recreation.gov), group-site exclusion.
- Adaptive polling and bounded scheduled logs.

## Requirements

- Python 3.10+. There are no packages to install and no package manager is used.

## Quick start

```bash
# 1. Set your home location (used to compute distance to each campground).
# Replace these placeholders with your own coordinates before running a rebuild.
export CAMPWATCH_HOME_LAT=YOUR_LATITUDE
export CAMPWATCH_HOME_LON=YOUR_LONGITUDE

# 2. (Optional) regenerate the candidate lists for YOUR area:
./build_candidates.py   # private default: local 90 km distance filtering
./build_wa_parks.py     # WA + Tacoma Power parks within an estimated ~2h drive
#    -> these write candidates.json / wa_parks.json, which feed watch_config.json.
#    (Shipped JSON files use a fictional demonstration origin; rebuild for your area.)
python3 watch.py --rebuild-config  # rebuild watch_config.json from those lists

# 3. Create your private trip-date file (it is ignored by Git)
cp watch_targets.example.json watch_targets.json
# Edit watch_targets.json as described below.

# 4. Run the watcher once
./watch.py
# Or perform a one-off 90-day scan without changing your private trip targets
./watch.py --all-once

# 5. Read the availability saved by the most recent scan
./report.py             # all qualifying openings in the last scan's window
./weekend.py            # saved runs covering THIS coming Fri+Sat
./weekend.py 2030-08-09 2030-08-10   # a specific weekend

# Optional live campground map (opens http://127.0.0.1:8765/)
python3 availability_map.py --open
```

Long scans checkpoint `last_state.json` atomically after every campground and
print `[completed/total]` progress. Availability requests run concurrently, but
with conservative independent caps: three recreation.gov workers and two
GoingToCamp workers. Each provider also has a shared request-start pacer; an
HTTP 429 pauses all pending workers for that provider before its next request.
While a scan is running or if it stops early, the report commands warn that
unprocessed campgrounds still show their previous results. Alert comparisons use
a separate last-complete baseline, so an interrupted incremental scan cannot
suppress an alert on the next run. Restarting the same date window and
configuration resumes from the checkpoint and skips campground IDs already
completed; a date, filter, or configuration change starts a fresh scan.

### Availability map

`availability_map.py` serves an interactive, loopback-only map. While a map tab
is open, it holds one same-origin event stream to the local map server. The
server watches the atomic scan checkpoint files: a checkpoint-only change updates
the status line, while a changed availability fingerprint causes one full map-data
fetch and marker/list update. It does not repeatedly fetch unchanged availability
data. Markers represent campgrounds with qualifying availability; clicking one
opens an unpinned floating detail card with the number of distinct sites,
observed date span, distance, and provider-review links. An unpinned card closes
when you click elsewhere or open another card. Use its pin button to keep it
open; up to five cards may be pinned, and pinning a sixth automatically unpins
the oldest pinned card. Reopening an existing card keeps its position and pin
state, then brings it to the foreground. Cards can be dragged by their headers
and resized from their lower-right corners; × always closes a card. The keyboard-
focused card has a blue inset highlight; Escape closes it, arrow keys select the
nearest open card, and Page Up/Page Down scroll its availability. Press `?` for
a Gmail-style shortcut overlay. Selecting a marker or a campground in the list
opens the relevant card without changing its pin state. The
check-in-date fields update the campground list, map markers, and any open card
immediately, locally, without another provider scan. Leave stay length blank to
browse every recorded option; set it (for example, to two nights) to count only
sites whose runs cover that stay at each selected check-in date. Each scan now
records its inclusive checked-night window separately from positive availability.
The map displays that window and warns when a selection is wholly or partly
outside it: un-checked dates are always shown as unknown, never unavailable.
The 7-day, 30-day, and full-window presets set the same check-in range.
Drag an empty part of the map to pan. Hover the map and use the mouse wheel or
double-click to zoom; the +/− controls work too. When the map has keyboard focus,
the arrow keys pan, +/− (including Command +/−) zoom, and Home fits the currently
displayed campgrounds. Those map shortcuts are also available from a focused card
when they are not being used for a card shortcut.
Long availability tables scroll inside the card while keeping the campground name
and column headers visible. Results are grouped by check-in day: each compact,
observed-stay chip is a booking link and shows its nights and observed-site
count. The card also has a **Book** link for the first displayed stay. Its footer
states when the openings were observed and, when a displayed stay exceeds ten
nights, warns that the provider may not support that duration. The status
bar reports a warning whenever scan data has not changed for more than one hour.
It also reports when the local event stream disconnects and reconnects
automatically after the map server is restarted. When a scan is marked failed, or
has been running without a checkpoint for five minutes, the refresh badge becomes
actionable immediately. Click it to copy `python3 watch.py --all-once`, then run
the copied command in the project directory to refresh or resume saved
availability. A completed checkpoint never suppresses that full new scan; an
interrupted scan instead resumes safely from its partial checkpoint. Returning to
a backgrounded map tab performs one catch-up fetch in case the browser missed an
event.

For Washington State Parks, GoingToCamp supplies opaque internal resource IDs
instead of useful campsite labels. The map hides those IDs and groups identical
availability windows, showing the number of sites available for each date range.
Washington booking links can open the provider with a selected stay's dates;
recreation.gov opens the campground's availability page. Recreation.gov does
not offer a reliable row-specific deep link, so choose the shown site and dates
there.

Changing dates or a stay length uses the already-downloaded local scan result and
makes no provider request. It labels those results as **observed availability**,
not a completed reservation. Without a selected stay length, the map preserves
the observed consecutive run; with a selected date and length, it counts sites
whose saved run covers that exact stay. Neither view is a bookable-site count.

The current rules encode Washington State Parks' published 10-night-in-one-park
within-30-days limit and 90-night annual statewide limit in
`provider_rules.json`, with a link to its source. The map excludes a selected
stay longer than 10 nights, but cannot check a visitor's prior reservations, so
even a shorter WA stay is not confirmed bookable. Recreation.gov and Tacoma Power
have no configured blanket stay cap: their long runs remain visible as observed
availability, with the booking limit unknown. A campground that failed during
the latest scan is visibly marked as not rechecked; its current availability is
unknown rather than treated as newly checked.
Press Ctrl-C in the map server's terminal to stop it.

The map installs no Python package and loads no third-party JavaScript. While the
map is open, the browser requests only the visible raster tiles from OpenStreetMap
and displays the required attribution. This reveals your IP address and viewed map
region to OpenStreetMap, but the watcher does not send the availability data or
home coordinates. See the [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/).

Federal discovery supports two equivalent filtering modes. Both apply the same
local 90 km validation and report distances explicitly in kilometers and miles:

```bash
./build_candidates.py --distance-filter client  # default; coordinates stay local
./build_candidates.py --distance-filter server  # faster; sends coordinates to recreation.gov
./build_candidates.py --distance-filter drive   # OSM road-distance/time cutoff; no rec.gov proximity query
./build_wa_parks.py --distance-filter drive     # OSM road-distance/time for WA + Tacoma Power cards
./build_candidates.py --max-distance-km 120      # optional explicit km cutoff
```

Server mode is opt-in because recreation.gov interprets its `radius` and
returned `distance` values as kilometers and receives the configured coordinates.
Driving mode is deliberately incompatible with server mode: it fetches the
statewide catalog, then sends your home coordinate and candidate coordinates in
small batches to the public OpenStreetMap OSRM routing service to compute road
distance and the profile's traffic-free route duration. It uses no extra package,
but does disclose those coordinates and your IP address to that service; use the
default client mode if that is not acceptable.
The service is best-effort and rate-limited, so use driving mode only for an
occasional candidate-list rebuild rather than a scheduled task.
The generated JSON lists contain distances derived from your home location, so
review them before committing; the checked-in lists use the public Seattle example.
`build_wa_parks.py` estimates drive time from straight-line distance, a regional
road-distance factor, and an average speed; it is not a routing-based cutoff.
Pass `--distance-filter drive` to that command too when you want the map to show
OpenStreetMap/OSRM route times instead for every provider. The map labels OSM
times as route estimates with no live traffic; otherwise it labels the local
WA/Tacoma Power park-list estimate rather than attributing it to the parks.

## Configuration

Secrets and trip dates stay out of source code. Environment settings are:

| Variable | Default | Meaning |
|---|---|---|
| `CAMPWATCH_HOME_LAT` / `CAMPWATCH_HOME_LON` | Required | Your home coordinates for distance filtering; there is no embedded location fallback. |
| `CAMPWATCH_WEBHOOK_URL` | *(none)* | Where to POST notifications (see below). If unset, openings are only written to `alerts.jsonl`. |
| `CAMPWATCH_WEBHOOK_TEXT_KEY` | `content` | JSON key for the human-readable text. `content` for Discord, `text` for Slack, `message` for ntfy. |
| `CAMPWATCH_NOTIFY` | `1` | Set to `0` to disable notifications entirely. |
| `CAMPWATCH_ALLOW_PRIVATE_WEBHOOK` | `0` | Set to `1` only when intentionally posting to a private/local HTTPS server. |
| `CAMPWATCH_LOG_MAX_BYTES` | `2097152` | Rotate the scheduled log at this size. |
| `CAMPWATCH_LOG_BACKUPS` | `4` | Number of old scheduled logs to retain. |
| `CAMPWATCH_RECGOV_WORKERS` | `3` | Concurrent recreation.gov campground polls; bounded to 1–4. |
| `CAMPWATCH_GTC_WORKERS` | `2` | Concurrent GoingToCamp park polls; bounded to 1–3. |

The defaults are intentionally modest because neither availability provider
publishes a usable rate-limit contract for these public endpoints. If a provider
starts returning HTTP 429 or WAF errors, lower the relevant value to `1`; the
watcher preserves the last known result for that failed target and prints the
HTTP status in its progress warning. Values above the documented caps are
clamped rather than creating a high-volume scan.

### Provider rules

[`provider_rules.json`](provider_rules.json) holds the editable, non-secret
data behind regional discovery: provider display labels, known published
stay-length limits, the campground and group-site name markers, candidate
rating/distance defaults, and the local WA drive-estimation heuristic. This
keeps policy/data out of Python while retaining the provider HTTPS allowlists and
API schemas in code as a security boundary.

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
| `availability_map.py` | Loopback-only live OpenStreetMap view of available campgrounds. |
| `availability_map.html` | Static markup and styles for the loopback map page. |
| `availability_map.js` | First-party browser behavior served only by the loopback map server. |
| `campwatch_config.py` | Safely loads owner-only, Git-ignored local settings. |
| `campwatch_http.py` | Size-limited, allow-listed standard-library HTTPS clients. |
| `provider_rules.json` | Editable regional labels and filtering/drive-estimation rules. |
| `watch_config.json` | The list of campgrounds to watch (recreation.gov + WA). |
| `watch_targets.json` | Private, ignored trip dates (created by you). |
| `watch_targets.example.json` | Safe empty template for trip dates. |
| `build_candidates.py` | Regenerate the recreation.gov candidate list for your area. |
| `build_wa_parks.py` | Regenerate the WA State Parks list within a drive radius. |
| `discover_gtc.py` | Helper to list GoingToCamp facility/map IDs. |
| `candidates.json` / `wa_parks.json` / `gtc_campgrounds.json` | Reference data. |
| `report.py` | Print everything with recorded availability, ranked by distance. |
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
  endpoints and fails closed if its occupancy filter is unavailable. That filter
  is useful but does not validate every reservation rule; the configured
  Washington State Parks 10-night cap is applied separately.
- GoingToCamp currently rejects Python's default user agent at its web firewall,
  so the client sends a static browser-compatible user agent. It is not a token
  or identity, but it is an upstream compatibility dependency: a WAF change can
  cause a poll to fail closed until the header is updated.

## License

MIT — see [LICENSE](LICENSE).
