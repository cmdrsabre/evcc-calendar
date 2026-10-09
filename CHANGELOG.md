# Changelog

## 0.3.0
- New tab "Einstellungen": consumption values, reserve, cap for unclear classification, charge power, trip limits and the car/train rules can be edited in the UI. Values overlay `config.yaml`, are stored in the database and can be reset. Connection data and secrets stay in `config.yaml`.
- New tab "Verlauf": the last 300 runs (including "Jetzt prüfen") with the result of each run.
- New API endpoints `GET/POST /api/settings` and `GET /api/history` (login required).
- Saving settings triggers a run in the configured mode.

## 0.2.4
- Fix: no more warning "plan is not effective in evcc" when the battery is already at or above the plan target. evcc does not report such a plan as effective until the battery drops below the target, which is expected.
- The "Ladeplan gesetzt" push now adds "Nach dem aktuellen Ladestand (xx %) ist kein weiteres Laden nötig. Der Plan greift, falls der Ladestand darunter sinkt." when the battery is already at or above the target.

## 0.2.3
- Docker image carries OCI labels (source repository, description, license), so the GitHub package is linked to the repo.
- Unraid template (`templates/evcc-calendar.xml`) and icon.
- An example `config.yaml` is created in the data folder on first start if none exists.
- Example config and tests contain only fictional data.

## 0.2.1
- Docker image published to ghcr.io (`ghcr.io/cmdrsabre/evcc-calendar`), built on version tags (`v*.*.*`).
- `docker-compose.yml` uses the published image; `--build` still builds locally.
- English documentation and comments, MIT license.

## 0.2.0
- Login with a fixed user `admin`; the password is set on first access.
- Per-appointment overrides in the UI: automatic / car / no car and charge target (5 to 100 %), also for appointments without an address.
- New UI (timeline, charge bar, light/dark, mobile), optional images.
- Car is the default and counts as clear. "Unclear" only on contradiction (car and train both in the text).
- Routes below `min_car_km` (4 km) count as walk or bike.
- Chain window `chain_window_h` is now 5 h, statuses "Folgetermin" and "Später".
- Own vehicle image at `<data folder>/personal/car.webp`: visible only when logged in, overrides the default image, listed in `.gitignore`.
- New column and display of the consumption used, including temperature.
- New config values: `min_car_km`, `manual_drive_min`; `chain_window_h` defaults to 5.

## 0.1.0
- First working version: calendar via Home Assistant, route via OpenRouteService, plan in evcc, dry run, push via Home Assistant.
