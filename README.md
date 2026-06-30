# campground-watcher

A small, cron-friendly watcher that polls **recreation.gov** and **Washington
State Parks** (the GoingToCamp booking system) for campsite openings near you and
fires an instant notification when a site that matches your filters becomes
available. It is designed to catch **cancellations** for otherwise sold-out
weekends and surface a ready-to-click booking link the moment a spot frees up.

It is intentionally **not** LLM-driven: it is plain Python you run on a timer, so
it costs nothing to run and is fully reproducible.

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

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) — every script has a `uv` shebang and a
  PEP 723 inline dependency block, so you can run them directly
  (`./watch.py`) and uv resolves and caches the dependencies automatically.
  No manual venv or `pip install` needed.

## Quick start

```bash
# 1. Set your home location (used to compute distance to each campground)
export CAMPWATCH_HOME_LAT=47.6062     # your latitude
export CAMPWATCH_HOME_LON=-122.3321   # your longitude

# 2. (Optional) regenerate the candidate lists for YOUR area:
./build_candidates.py   # recreation.gov, rating >= 4, within ~90mi
./build_wa_parks.py     # WA State Parks within a ~2h drive
#    -> these write candidates.json / wa_parks.json, which feed watch_config.json.
#    (Shipped JSON files are tuned for the Seattle area; rebuild for elsewhere.)

# 3. Run the watcher once
./watch.py

# 4. Read what is currently bookable
./report.py             # everything open in the next 90 days
./weekend.py            # sites open THIS coming Fri+Sat
./weekend.py 2026-08-07 2026-08-08   # a specific weekend
```

## Configuration

Everything is driven by environment variables (no secrets in code):

| Variable | Default | Meaning |
|---|---|---|
| `CAMPWATCH_HOME_LAT` / `CAMPWATCH_HOME_LON` | Seattle | Your home coordinates for distance filtering. |
| `CAMPWATCH_WEBHOOK_URL` | *(none)* | Where to POST notifications (see below). If unset, openings are only written to `alerts.jsonl`. |
| `CAMPWATCH_WEBHOOK_TEXT_KEY` | `content` | JSON key for the human-readable text. `content` for Discord, `text` for Slack, `message` for ntfy. |
| `CAMPWATCH_NOTIFY` | `1` | Set to `0` to disable notifications entirely. |

### Target weekend(s)

Edit the `TARGET_WEEKENDS` list near the top of `watch.py`. Each entry is a label
plus the **nights** (check-in dates) a stay must cover. A standard Fri→Sun
booking is the Friday night + the Saturday night:

```python
TARGET_WEEKENDS = [
    ("Aug 7-9", [dt.date(2026, 8, 7), dt.date(2026, 8, 8)]),
]
```

- Empty list `[]` → watch nothing (silent).
- Set to `None` → alert on *every* opening, no weekend filter.

### Notifications (webhook)

The watcher POSTs a JSON payload to `CAMPWATCH_WEBHOOK_URL` for each new opening.
The payload includes a human-readable text field (key configurable) plus
structured `sites` data with per-site booking URLs. This works out-of-the-box
with:

- **Discord** — use a channel webhook URL, keep `CAMPWATCH_WEBHOOK_TEXT_KEY=content`.
- **Slack** — incoming webhook URL, set `CAMPWATCH_WEBHOOK_TEXT_KEY=text`.
- **ntfy.sh** — topic URL, set `CAMPWATCH_WEBHOOK_TEXT_KEY=message`.
- Anything else that accepts a JSON POST.

## Running on a timer

Use cron or a systemd timer. A single-tick runner with an overlap guard is
included as `run_watch.sh`. Example systemd user units:

`~/.config/systemd/user/campground-watcher.service`
```ini
[Unit]
Description=Campground availability watcher (one tick)

[Service]
Type=oneshot
TimeoutStartSec=900
Environment=CAMPWATCH_HOME_LAT=47.6062
Environment=CAMPWATCH_HOME_LON=-122.3321
Environment=CAMPWATCH_WEBHOOK_URL=https://your-webhook-here
ExecStart=%h/campground-watcher/run_watch.sh
```

`~/.config/systemd/user/campground-watcher.timer`
```ini
[Unit]
Description=Run campground watcher every ~15 min

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now campground-watcher.timer
```

A full WA run takes ~10-13 minutes (the per-stay occupancy cross-check is the
slow part), so a 15-minute interval avoids overlap. `run_watch.sh` also holds a
`flock` so ticks can't stack.

## Files

| File | Purpose |
|---|---|
| `watch.py` | The watcher. Polls, filters, diffs against last state, notifies. |
| `watch_config.json` | The list of campgrounds to watch (recreation.gov + WA). |
| `build_candidates.py` | Regenerate the recreation.gov candidate list for your area. |
| `build_wa_parks.py` | Regenerate the WA State Parks list within a drive radius. |
| `discover_gtc.py` | Helper to list GoingToCamp facility/map IDs. |
| `candidates.json` / `wa_parks.json` / `gtc_campgrounds.json` | Reference data. |
| `report.py` | Print everything currently bookable, ranked by distance. |
| `weekend.py` | Print sites open for a given weekend (single-site, whole-stay). |
| `run_watch.sh` | Single-tick runner with an overlap lock, for cron/systemd. |
| `test_watch.py` | Tests (`./test_watch.py`). |

## Notes & limitations

- WA State Parks occasionally returns HTTP 403 (Azure WAF) for a park on a given
  run; the watcher skips that park for that tick and preserves prior state.
- recreation.gov popular campgrounds sometimes release cancellations one night at
  a time, so a single site may not always cover a full weekend — that's why the
  weekend filter requires one site to cover *all* requested nights.
- WA State Parks availability comes via [`camply`](https://github.com/juftin/camply)'s
  GoingToCamp provider plus a local occupancy cross-check baked into `watch.py`.

## License

MIT — see [LICENSE](LICENSE).