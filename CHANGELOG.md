# Änderungen

## 0.2.0
- Anmeldung mit festem Benutzer `admin`, Passwort wird beim ersten Aufruf vergeben.
- Vorgaben je Termin in der Oberfläche: Automatisch / Auto / Kein Auto und Ladeziel (5 bis 100 %), auch für Termine ohne Adresse.
- Neue Oberfläche (Zeitleiste, Ladebalken, Hell/Dunkel, Handy), optionale Bilder.
- Auto gilt als Standard und ist klar. „Unklar“ nur noch bei Widerspruch (Auto und Bahn im Text).
- Strecken unter `min_car_km` (4 km) gelten als zu Fuß oder Rad.
- Kettenfenster `chain_window_h` jetzt 5 h, Status „Folgetermin“ und „Später“.
- Eigenes Fahrzeugbild unter `<Datenordner>/personal/car.webp`: nur eingeloggt sichtbar, überschreibt das Standardbild, steht in `.gitignore`.
- Neue Spalte und Anzeige des verwendeten Verbrauchs samt Temperatur.
- Neue Config-Werte: `min_car_km`, `manual_drive_min`; `chain_window_h` Standard 5.

## 0.1.0
- Erste lauffähige Fassung: Kalender über Home Assistant, Strecke über OpenRouteService, Plan in evcc, Dry-Run, Push über Home Assistant.
