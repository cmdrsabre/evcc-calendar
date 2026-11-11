# evcc-calendar

Sets a one-time [evcc](https://evcc.io) charging plan (target SoC and time) for your car from appointments in a Home Assistant calendar.
Every 30 minutes it reads the appointments of the next 6 days, classifies car trips, determines the route via OpenRouteService,
calculates the energy needed for the outbound and return trip and sets the plan in evcc. It starts in **dry-run** mode: nothing is written and nothing is sent.

> The web UI and messages are available in English (default) and German. Set `language: de` in `config.yaml` or change it in the UI under Settings.

## Install and configure
Pick one way. All three start in **dry-run** mode (nothing is written to evcc, nothing is sent).

**You need (all ways):**
- Home Assistant with a calendar entity and a [long-lived access token](https://www.home-assistant.io/docs/authentication/#your-account-profile) (HA token)
- evcc, reachable from the host, with its API key and your vehicle set up
- a free [OpenRouteService](https://openrouteservice.org) API key (ORS key)

### 1. Docker (Compose)
Requires: Docker with Compose.
1. Get the files and the data folder:
   ```
   mkdir evcc-calendar && cd evcc-calendar && mkdir data
   curl -O https://raw.githubusercontent.com/cmdrsabre/evcc-calendar/main/docker-compose.yml
   curl -o .env https://raw.githubusercontent.com/cmdrsabre/evcc-calendar/main/.env.example
   ```
2. Edit `.env`: fill in `HA_TOKEN`, `EVCC_API_KEY`, `ORS_KEY`. Never share this file. Other host port: `HOST_PORT` (default 8180).
3. `docker compose up -d` (first start creates `data/config.yaml` from the example).
4. Edit `data/config.yaml` (see [Configure](#configure)), then `docker compose restart`.
5. Open `http://localhost:8180` and set the password for the user `admin`.

Logs: `docker compose logs -f`. Update: `docker compose pull && docker compose up -d`.

### 2. Unraid app
Requires: Unraid with Docker enabled.
1. Apps → search `evcc-calendar` → Install. The app always installs the latest stable release (`latest`), never a pre-release.
2. Fill in `HA_TOKEN`, `EVCC_API_KEY`, `ORS_KEY`, then Apply.
3. Edit `/mnt/user/appdata/evcc-kalender/config.yaml` (created on first start, see [Configure](#configure)), then restart the container.
4. Open the WebUI (`http://<unraid-ip>:8180`) and set the password for the user `admin`.

To try a beta release: edit the container, set Repository to `ghcr.io/cmdrsabre/evcc-calendar:<tag>` (tags on the [package page](https://github.com/cmdrsabre/evcc-calendar/pkgs/container/evcc-calendar)), Apply. Set it back to `:latest` afterwards.

### 3. Manual (clone and run)
Requires: Python 3.12 (3.9+ runs the tests), git.
1. Clone and install:
   ```
   git clone https://github.com/cmdrsabre/evcc-calendar.git && cd evcc-calendar
   python -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   mkdir data && cp config.example.yaml data/config.yaml
   ```
2. Edit `data/config.yaml` (see [Configure](#configure)). Also set `db_path: data/state.db` and `web: {host: 127.0.0.1, port: 8180}` (port 80 needs root).
3. Set the keys in your shell (not in a file in the repo): `export HA_TOKEN=... EVCC_API_KEY=... ORS_KEY=...`
4. Try it: `python -m evccplan once --dry-run --config data/config.yaml`
5. Run it: `python -m evccplan serve --config data/config.yaml`, open `http://localhost:8180`, set the password for `admin`.

### Configure
Edit `config.yaml` (Docker/Unraid: in the data folder). The minimum:
- `home.address` (or `lat`/`lon`): where trips start
- `ha.url`, `ha.calendar` (one entity or a list of calendars), `ha.notify_targets` (notify entities per level)
- `evcc.url`, `evcc.vehicle` (name from evcc `/api/state`, e.g. `db:1`)
- `language: de` for German (default `en`); `rules` for title words like car/train

Everything else has defaults, most values can also be changed in the UI under Settings.
Go live only after the dry run looks right for a few days: set `dry_run: false` in `config.yaml` and restart.
Only **one** instance may run live.

## Commands
- `python -m evccplan once --config config.yaml --dry-run`: single run, JSON output, writes nothing.
- `python -m evccplan serve`: service with status page (default in the container).

## Rules in brief
- Only appointments with a time and a location. The trip always starts at home.
- Classification: whole word "Auto"/"Bahn" (car/train) in title or description, then `rules` (title words, case-insensitive), otherwise car (clear).
  "Unclear" only means a contradiction (car and train both in the text); it caps the target at `unclear_cap_soc` and sends a push.
- One-way distance below `min_car_km` (4 km): walk/bike, not a car appointment (not planned, not chained). The keyword "Auto" still forces car.
- Target = (energy for outbound and return trip + `reserve_soc`), rounded up to 5 %, at most 100 %. Only planned if above the evcc minimum.
- Departure time = appointment start − travel time − `time_buffer_min`. The charge deadline is earlier by `charge_buffer_min` (3), plus `cold_buffer_min` (12) below `temp_threshold_c`, so the target is reached slightly early.
- Overlapping car appointments are reported as a conflict: the first one is planned with enough charge for the longer of the overlapping trips.
- Consecutive car appointments (≤ `chain_window_h` = 5 h from return to the next departure) form a chain (status "Folgetermin"): if the time at home is not enough to recharge, the target of the first appointment is raised.
- The service only changes plans it set itself. Manual one-time plans and all repeating plans are left untouched.

## Notifications
Sent through `notify.send_message` in Home Assistant with target entities per level (`ha.notify_targets`):
confirmations ("Ladeplan gesetzt") to the app, warnings additionally to Telegram, errors additionally as an Alexa announcement.
Each target gets its own call; a push counts as sent when at least one target accepted it. Text for entities whose id contains `telegram` is escaped for MarkdownV2 (HA's default parse mode there).
Each message is sent only once per occasion.
Once a minute the service checks the cached live status from evcc (no planning run, no extra requests) and sends:
- a **warning** 10 minutes before the planned charge start if the car is not plugged in,
- an **info** when the car comes back below the assumed charge (the reserve) and charging therefore has to start at least 10 minutes earlier (a higher charge level sends nothing),
- a **warning** when the plan can no longer be reached in time.

If a push fails, the same message is tried again only after 5 minutes. Overlapping car appointments are planned for the longest of the trips, with the deadline of the one that has to leave first.

## Failure handling
- **evcc not responding:** 3 attempts per run. Then a push "ALARM: evcc ist nicht erreichbar" via Home Assistant (at most every 6 h) and a red notice on the status page. The plan is not changed.
- **Calendar not readable:** 3 attempts. Then the service continues with the last read appointments and still serves evcc, with a red notice on the status page and, if possible, a push. Without a stored state (first start) the run aborts with an error.
- **Push via Home Assistant not possible:** the situation is shown in red on the status page and logged at ERROR level in the container log. An emergency channel outside Home Assistant (e.g. Unraid notifications) is not built in yet.
- **OpenRouteService not reachable:** stored addresses and routes keep being used. Everything else is not planned; one summary notice instead of many single ones.

## Tests
`pip install -r requirements-dev.txt && python -m pytest`

## Web UI (v0.2)
- On first access you set the password for the fixed user `admin` (at least 8 characters). Afterwards you log in with it.
  Forgot the password: delete `state.db`. This also resets all overrides made in the UI.
- Per appointment you choose **Automatisch / Auto / Kein Auto** (automatic / car / no car) and a **charge target** in 5 % steps. A charge target means: charge here.
  This also works for appointments without an address or with an unclear address; these use `manual_drive_min` (45 min) as travel time.
- A chosen charge target is used exactly as set, without reserve, rounding or the cap for unclear classification. If it is not enough for the trip, the card warns you.
- Every change immediately triggers a run in the configured mode. With `dry_run: true` nothing is written to evcc.
  The "Jetzt prüfen" button always only calculates (dry run).
- Overrides apply per appointment instance (UID and date) and are deleted two days after the appointment.
- Keep the network trusted: the UI is served over HTTP. For access from outside, put a reverse proxy with HTTPS in front
  (it sets `X-Forwarded-Proto: https`, and the cookie is then marked `Secure`).
- Images are optional.

### Settings and history (v0.3)
- The **Einstellungen** tab edits consumption values, reserve, charge power, trip limits and the car/train rules. Changed values are stored in the database
  and win over `config.yaml`; "Auf config.yaml zurücksetzen" removes them. URLs, tokens and keys are not editable in the UI.
- The **Verlauf** tab lists the last 300 runs with their result. Deleting `state.db` also clears settings and history.

### Next charge, cost and sessions (v0.5)
- Each planned charge in the calendar shows **when charging is planned** (timeline bar with now and plan time), its duration and cost; the bar of the card marks the
  actual state of charge and where the plan wants it, so a deviation is visible at once, plus whether charging is ahead of or behind the plan. While the car is plugged in the window comes from evcc, otherwise it is estimated from the plan time and `charge_power_kw`.
- **Maximum cost** of the next charge: grid energy (with `charge_loss_margin`) times the price from evcc. PV share is not deducted, hence "maximum".
  If evcc has no price, nothing is shown. If the car is not plugged in, the state of charge on arrival is assumed to be `reserve_soc`.
- The history lists finished charging sessions with their real cost (needs evcc's session database, read only).

## Your own vehicle image
The bundled blue car (`evccplan/static/img/car.webp`) is the default. Your own image overrides it without changing any project file:

1. Put an image named `car.webp` (alternatively `car.png`, `car.jpg`) into the `personal` folder next to the database.
   - Mac, native: `data/personal/car.webp`
   - Unraid: `/mnt/user/appdata/evcc-kalender/personal/car.webp` (the folder is mounted into the container, no restart needed)
   - elsewhere: `personal_dir` in `config.yaml`
2. Reload the page. The image appears on the vehicle card.

Rules: the login page always shows the default image. Your own image is only served after login and then replaces the default.
If your own image is missing, the default stays. The `personal/` folder (and `data/`) is in `.gitignore`, so the image never ends up in the repository.
Recommended: a cut-out car (transparent background) in side view, about 1200 pixels wide, under 8 MB. The UI does not crop or convert anything.
Later (not before 0.3): set an image per vehicle reported by evcc in the UI, with automatic cropping and format conversion.

## Languages
The language (`en` default, `de`) applies to the UI, push messages and API errors. Log output is always English. The keywords "Auto"/"Car" and "Bahn"/"Train" in an
appointment are recognised in both languages whatever the setting is. To add a language, copy `evccplan/static/locales/en.json` to `<code>.json`, translate the
values (keep the keys and the `{placeholders}`) and select it in the UI; `tests/test_i18n.py` checks that all languages have the same keys and placeholders.

## Versions
Changes in detail: [CHANGELOG.md](CHANGELOG.md). The version is in `evccplan/__init__.py`. Before 1.0 the middle number rises with new features,
the last one with bug fixes. Version 1.0 would be the open-source release.

| Version | Highlights |
|---|---|
| [0.6.1](CHANGELOG.md#061) (current) | Calendar view hides appointments without charging (toggle), push fixes: per-target delivery, Telegram MarkdownV2 escaping, no warnings for past plans |
| [0.6](CHANGELOG.md#060) | Several calendars with a name label, overlap conflicts, charge buffer (extra when cold), pushes for "not plugged in" / "starts earlier" / "plan not reachable" |
| [0.5](CHANGELOG.md#050) | Charge window, progress and cost forecast per appointment (with PV share), real cost of finished sessions, shorter run history, warning when a plan cannot be reached |
| [0.4](CHANGELOG.md#040) | Live status (plugged in, charging, power, PV / grid / home), link to GitHub |
| [0.3](CHANGELOG.md#030) | Settings and history tabs, English and German |
| [0.2](CHANGELOG.md#020) | Login, per-appointment overrides, new UI, Docker image on ghcr.io |
| [0.1](CHANGELOG.md#010) | Calendar, route, plan in evcc, dry run, push via Home Assistant |

### Future ideas
Not scheduled for any version yet:
- Live run button (a run in live mode started from the UI)
- Bluelink consumption (real consumption from the car instead of the configured values)
- Filter for wrongly entered appointments
- Google OAuth login
- Home Assistant optional (calendar directly via Google, other notification channels)
- MQTT: react to evcc topics instead of polling

## License
[MIT](LICENSE). Third-party components and notices: [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
