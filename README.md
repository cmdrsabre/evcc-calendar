# evcc-calendar

Sets a one-time [evcc](https://evcc.io) charging plan (target SoC and time) for your car from appointments in a Home Assistant calendar.
Every 30 minutes it reads the appointments of the next 6 days, classifies car trips, determines the route via OpenRouteService,
calculates the energy needed for the outbound and return trip and sets the plan in evcc. It starts in **dry-run** mode: nothing is written and nothing is sent.

> The web UI and messages are available in English (default) and German. Set `language: de` in `config.yaml` or change it in the UI under Settings.

## Getting started (Mac test and Unraid)
Port inside the container: 80. Published port defaults to **8180** (`HOST_PORT` in `.env`).

1. Create a data folder and copy the configuration:
   - Mac: `mkdir data && cp config.example.yaml data/config.yaml`
   - Unraid: `/mnt/user/appdata/evcc-kalender/config.yaml`
   Then review `config.yaml` (home address, vehicle name, `ha.notify_targets`).
2. `cp .env.example .env` and fill in HA_TOKEN, EVCC_API_KEY, ORS_KEY. On Unraid enable the commented values (`DATA_DIR`, `PUID=99`, `PGID=100`). Never share `.env`.
3. `docker compose up -d` pulls the published image `ghcr.io/cmdrsabre/evcc-calendar:latest`. To build it locally instead: `docker compose up -d --build`.
4. Status page: `http://localhost:8180` (Mac) or `http://<unraid-ip>:8180`. "Jetzt prüfen (Dry-Run)" shows the calculated plan without writing anything.
5. Logs: `docker compose logs -f`. Stop: `docker compose down`.
6. Once the dry run looks right over a few days, set `dry_run: false` in `config.yaml` and restart with `docker compose restart`.

Important: only **one** instance may run live. Before moving to Unraid, stop the Mac container with `docker compose down`.

## Commands
- `python -m evccplan once --config config.yaml --dry-run`: single run, JSON output, writes nothing.
- `python -m evccplan serve`: service with status page (default in the container).

## Rules in brief
- Only appointments with a time and a location. The trip always starts at home.
- Classification: whole word "Auto"/"Bahn" (car/train) in title or description, then `rules` (title words, case-insensitive), otherwise car (clear).
  "Unclear" only means a contradiction (car and train both in the text); it caps the target at `unclear_cap_soc` and sends a push.
- One-way distance below `min_car_km` (4 km): walk/bike, not a car appointment (not planned, not chained). The keyword "Auto" still forces car.
- Target = (energy for outbound and return trip + `reserve_soc`), rounded up to 5 %, at most 100 %. Only planned if above the evcc minimum.
- Departure time = appointment start − travel time − `time_buffer_min`.
- Consecutive car appointments (≤ `chain_window_h` = 5 h from return to the next departure) form a chain (status "Folgetermin"): if the time at home is not enough to recharge, the target of the first appointment is raised.
- The service only changes plans it set itself. Manual one-time plans and all repeating plans are left untouched.

## Notifications
Sent through `notify.send_message` in Home Assistant with target entities per level (`ha.notify_targets`):
confirmations ("Ladeplan gesetzt") to the app, warnings additionally to Telegram, errors additionally as an Alexa announcement.
Each message is sent only once per occasion.

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

## Versions and roadmap
The version is in `evccplan/__init__.py`, changes are in `CHANGELOG.md`. Before 1.0 the middle number rises with new features,
the last one with bug fixes. Version 1.0 would be the open-source release.

| Version | Content | Status |
|---|---|---|
| 0.1 | Read calendar, set plan in evcc, dry run, push via Home Assistant, simple status page | done |
| 0.2 | Login, per-appointment overrides (car / no car / charge target), new UI, images, Docker image on ghcr.io | done |
| 0.3 | More functions in the UI: edit rules and consumption values, history (done in 0.3.0); live run button, Bluelink consumption, filter for wrongly entered appointments (planned) | current |
| 0.4 | Google OAuth login, Home Assistant optional (calendar directly via Google, other notification channels) | planned |

## License
[MIT](LICENSE). Third-party components and notices: [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

## Unraid
A template for the Unraid Docker UI is in `templates/evcc-calendar.xml`. Until it is listed in Community Applications, copy it to
`/boot/config/plugins/dockerMan/templates-user/evcc-calendar.xml` on the server. It then shows up under Docker → Add Container → Template.
On first start an example `config.yaml` is created in the appdata folder; edit it and restart the container.
