# evcc-Kalender-Ladeplanung

Setzt aus Terminen im Home-Assistant-Kalender einen einmaligen evcc-Ladeplan (Ziel-SoC und Zeit) für das Auto.
Alle 30 Minuten: Termine der nächsten 6 Tage lesen, Auto-Termine bewerten, Strecke per OpenRouteService bestimmen,
Ladebedarf für Hin- und Rückfahrt berechnen und den Plan in evcc setzen. Start im **Dry-Run**: es wird nichts geschrieben und nichts gesendet.

## Starten (Mac-Test und Unraid)
Port im Container: 80. Nach aussen standardmässig **8180** (`HOST_PORT` in `.env`).

1. Daten-Ordner anlegen und Konfiguration kopieren:
   - Mac: `mkdir data && cp config.example.yaml data/config.yaml`
   - Unraid: `/mnt/user/appdata/evcc-kalender/config.yaml`
   Dann `config.yaml` prüfen (Heimatadresse, Fahrzeugname, `ha.notify_targets`).
2. `cp .env.example .env` und HA_TOKEN, EVCC_API_KEY, ORS_KEY eintragen. Für Unraid die auskommentierten Werte (`DATA_DIR`, `PUID=99`, `PGID=100`) aktivieren. `.env` nicht weitergeben.
3. `docker compose up -d --build`
4. Statusseite: `http://localhost:8180` (Mac) bzw. `http://<unraid-ip>:8180`. „Jetzt prüfen (Dry-Run)" zeigt den berechneten Plan, ohne etwas zu schreiben.
5. Log ansehen: `docker compose logs -f`. Stoppen: `docker compose down`.
6. Passt der Dry-Run über einige Tage, in `config.yaml` `dry_run: false` setzen und mit `docker compose restart` neu starten.

Wichtig: Es darf nur **eine** Instanz scharf laufen. Vor dem Umzug auf Unraid den Mac-Container mit `docker compose down` stoppen.

## Befehle
- `python -m evccplan once --config config.yaml --dry-run`: ein Lauf, JSON-Ausgabe, schreibt nichts.
- `python -m evccplan serve`: Dienst mit Statusseite (Standard im Container).

## Regeln in Kürze
- Nur Termine mit Uhrzeit und Ort. Die Fahrt startet immer zu Hause.
- Einstufung: ganzes Wort „Auto"/„Bahn" in Titel oder Beschreibung, dann `rules` (Titelwörter, Gross-/Kleinschreibung egal), sonst Auto (klar). „Unklar“ ist nur ein Widerspruch (Auto und Bahn im Text);
  das deckelt das Ziel auf `unclear_cap_soc` und meldet das per Push.
- Einfache Strecke unter `min_car_km` (4 km): zu Fuß/Rad, kein Auto-Termin (nicht geplant, nicht verkettet). Das Stichwort „Auto“ erzwingt trotzdem Auto.
- Ziel = (Bedarf Hin- und Rückfahrt + `reserve_soc`), aufgerundet auf 5 %, höchstens 100 %. Geplant wird nur über dem evcc-Minimum.
- Abfahrtszeit = Terminbeginn − Fahrzeit − `time_buffer_min`.
- Aufeinanderfolgende Auto-Termine (≤ `chain_window_h` = 5 h von Rückkehr bis zur nächsten Abfahrt) bilden eine Kette (Status „Folgetermin“): Reicht die Zeit zu Hause zum Nachladen nicht, steigt das Ziel für den ersten Termin.
- Der Dienst ändert nur Pläne, die er selbst gesetzt hat. Manuelle Einmalpläne und alle Wiederholpläne bleiben unberührt.

## Benachrichtigungen
Versand über `notify.send_message` in Home Assistant mit Ziel-Entitäten je Stufe (`ha.notify_targets`):
Bestätigungen („Ladeplan gesetzt") an die App, Warnungen zusätzlich an Telegram, Fehler zusätzlich als Alexa-Durchsage.
Jede Meldung geht pro Anlass nur einmal raus.

## Ausfälle
- **evcc antwortet nicht:** 3 Versuche je Lauf. Danach Push "ALARM: evcc ist nicht erreichbar" über Home Assistant (höchstens alle 6 h) und roter Hinweis auf der Statusseite. Am Plan wird nichts geändert.
- **Kalender nicht lesbar:** 3 Versuche. Danach arbeitet der Dienst mit den zuletzt gelesenen Terminen weiter und bedient evcc, mit rotem Hinweis in der Statusseite und, wenn möglich, einem Push. Ohne gespeicherten Stand (Erststart) bricht der Lauf mit Fehler ab.
- **Push über Home Assistant nicht möglich:** Die Lage steht rot in der Statusseite und mit Stufe ERROR im Container-Log. Ein Notfallkanal außerhalb von Home Assistant (z. B. Unraid-Benachrichtigung) ist noch nicht eingebaut.
- **OpenRouteService nicht erreichbar:** Gespeicherte Adressen und Strecken werden weiter genutzt. Alles andere wird nicht geplant; ein Sammelhinweis statt vieler Einzelmeldungen.

## Tests
`pip install -r requirements-dev.txt && python -m pytest`


## Oberfläche (V2)
- Beim ersten Aufruf vergibst du das Passwort für den festen Benutzer `admin` (mindestens 8 Zeichen). Danach meldest du dich damit an.
  Passwort vergessen: `state.db` löschen. Das setzt auch alle Vorgaben aus der Oberfläche zurück.
- Je Termin wählst du **Automatisch / Auto / Kein Auto** und ein **Ladeziel** in 5-%-Schritten. Ein Ladeziel heißt: hier wird geladen.
  Das gilt auch für Termine ohne Adresse oder mit unklarer Adresse. Dort wird mit `manual_drive_min` (45 min) als Fahrzeit gerechnet.
- Ein gewähltes Ladeziel gilt genau so, ohne Reserve, Rundung oder Deckel bei unklarer Einstufung. Reicht es nicht für die Fahrt, warnt die Karte.
- Jede Änderung löst sofort einen Lauf im konfigurierten Modus aus. Mit `dry_run: true` wird also nichts in evcc geschrieben.
  Der Knopf „Jetzt prüfen“ rechnet immer nur (Dry-Run).
- Vorgaben gelten je Termin-Instanz (UID und Datum) und werden zwei Tage nach dem Termin gelöscht.
- Das Netz sollte vertrauenswürdig sein: Die Oberfläche läuft über HTTP. Für Zugriff von außen gehört ein Reverse-Proxy mit HTTPS davor
  (er setzt `X-Forwarded-Proto: https`, dann wird das Cookie als `Secure` markiert).
- Bilder sind optional

## Eigenes Fahrzeugbild
Das mitgelieferte blaue Auto (`evccplan/static/img/car.webp`) ist das Standardbild. Dein eigenes Bild überschreibt es, ohne dass du Projektdateien änderst:

1. Lege ein Bild mit dem Namen `car.webp` (alternativ `car.png`, `car.jpg`) in den Ordner `personal` neben der Datenbank.
   - Mac, native: `data/personal/car.webp`
   - Unraid: `/mnt/user/appdata/evcc-kalender/personal/car.webp` (der Ordner ist in den Container gemountet, ein Neustart des Containers ist nicht nötig)
   - anderer Ort: `personal_dir` in der `config.yaml`
2. Seite neu laden. Das Bild erscheint in der Fahrzeugkarte.

Regeln: Auf der Anmeldeseite sieht man immer das Standardbild. Das eigene Bild wird nur nach der Anmeldung ausgeliefert und ersetzt dann das Standardbild.
Fehlt das eigene Bild, bleibt das Standardbild. Der Ordner `personal/` (und `data/`) steht in `.gitignore`, das Bild landet also nicht im Repository.
Empfohlen: ein freigestelltes Auto (transparenter Hintergrund) in Seitenansicht, etwa 1200 Pixel breit, unter 8 MB. Die Oberfläche schneidet oder wandelt nichts um.
Später (nicht vor 0.3): ein Bild je von evcc gemeldetem Fahrzeug in der Oberfläche festlegen, mit automatischem Zuschnitt und Formatwandlung.

## Versionen und Fahrplan
Die Version steht in `evccplan/__init__.py`, die Änderungen in `CHANGELOG.md`. Solange es vor 1.0 ist, steigt die mittlere Zahl bei neuen Funktionen,
die letzte bei Fehlerkorrekturen. Version 1.0 wäre die Veröffentlichung als Open Source.

| Version | Inhalt | Stand |
|---|---|---|
| 0.1 | Kalender lesen, Plan in evcc setzen, Dry-Run, Push über Home Assistant, einfache Statusseite | fertig |
| 0.2 | Anmeldung, Vorgaben je Termin (Auto / Kein Auto / Ladeziel), neue Oberfläche, Bilder | aktuell |
| 0.3 | Weitere Funktionen in die Oberfläche: Regeln und Verbrauchswerte bearbeiten, Verlauf, scharfer Lauf-Knopf, Bluelink-Verbräuche, Filter für falsch eingetragene Termine | geplant |
| 0.4 | Google OAuth als Anmeldung, Home Assistant optional (Kalender direkt über Google, andere Benachrichtigungskanäle) | geplant |
