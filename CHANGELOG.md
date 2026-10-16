# Changelog

## 0.5.0
- Run history lists only runs with something to report (plan set or removed, problems, errors). Runs without changes are no longer stored; the tab shows the time of the last run instead.
- Identical consecutive entries are merged ("3× until ...").
- Problem texts are stored with each entry and shown in the history.
- History is paged (20 per page, "Show more"). `GET /api/history` takes `limit` (1 to 100) and `offset` and returns `total` and `last`.
- Charging info inside the calendar card of each planned charge: charging window as a timeline bar (planned start to end, now, plan time), duration, maximum cost, and for the charge under way whether it is ahead of or behind the plan. The card's own bar shows the actual state of charge ("Ist") and where the plan wants it right now ("Soll") next to the target. Later, already chained charges get an estimate (window, cost) too, `charge_est` per item in `/api/status`. A plan that belongs to no calendar card (e.g. a repeating plan in evcc) gets a panel above the calendar. Uses evcc's projected start and end while the car is plugged in, otherwise an estimate from the plan time and the configured charge power. Computed from evcc `/api/state` only (new module `chargeview.py`), `GET /api/live` carries it as `charge`.
- Cost forecast: maximum grid cost of the next charge (energy from the current or assumed state of charge to the target, charging losses included, no PV share). The price always comes from evcc (`tariffGrid`, plan slot prices, or the grid forecast over the estimated window); without an evcc price nothing is estimated. Away from home the state of charge on arrival is assumed to be the reserve and shown as estimated.
- The "Ladeplan gesetzt" push now adds the maximum cost.
- New warning when evcc reports that the plan cannot be reached in time (`planOverrun`), once per plan time.
- Real cost of finished charging sessions (evcc `GET /api/sessions`, read only): energy, price, PV share and state of charge are added to the history once per session.

## 0.4.0
- Live status on the vehicle card: plugged in, charging, charge power, SoC and charged energy from evcc. Optional PV generation, grid import / feed-in and home consumption when evcc reports them. Missing values are hidden, an unreachable evcc is shown as such.
- A background thread polls only evcc `GET /api/state` (new setting `live_poll_s`, default 10 s, 5 to 300, editable in the UI). It never starts a planning run and does no geocoding or routing. The new endpoint `GET /api/live` (login required) serves the cached value only.
- Planning runs stay serialized by the runner lock for every trigger (timer, UI change, "Jetzt prüfen"); covered by a test.
- Link to the GitHub page next to the version number.
- README: roadmap updated, "Future ideas" extended.

## 0.3.0
- New tab "Einstellungen": consumption values, reserve, cap for unclear classification, charge power, trip limits and the car/train rules can be edited in the UI. Values overlay `config.yaml`, are stored in the database and can be reset. Connection data and secrets stay in `config.yaml`.
- New tab "Verlauf": the last 300 runs (including "Jetzt prüfen") with the result of each run.
- New API endpoints `GET/POST /api/settings` and `GET /api/history` (login required).
- Saving settings triggers a run in the configured mode.
- Languages: English (default) and German for the UI, push messages and API errors, chosen with `language` in `config.yaml` or in the settings tab. Texts live in `evccplan/static/locales/<lang>.json`. **Existing installations that rely on German must set `language: de`.** Logs are English only.
- The keywords "car" and "train" work like "Auto" and "Bahn".
- Docker build uses the Python base image from the Amazon ECR Public mirror (`public.ecr.aws/docker/library/python`) to avoid Docker Hub rate limits in CI. Release workflow publishes only on version tags; pre-releases never get `latest`.

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
