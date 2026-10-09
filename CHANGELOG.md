# Changelog

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
