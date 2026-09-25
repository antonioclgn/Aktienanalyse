# Aktienanalyse

Charts für Aktien und ETFs mit Kauf-/Verkaufsbalken aus kombinierbaren Indikatoren
(Fear & Greed, Smart/Dumb Money aus den COT-Daten, RSI 14, Abweichung vom gleitenden
Durchschnitt, MACD) — und eine Überwachung, die bei Treffern in der Glocke und per Mail meldet.

## Server starten

Voraussetzung: Python ist installiert (`python --version` zum Prüfen).

1. PowerShell oder Terminal im Projektordner öffnen:
   ```
   cd c:\Users\anton\programmierung\Aktienanalyse
   ```
2. Server starten:
   ```
   python server.py
   ```
3. Im Browser öffnen:
   ```
   http://127.0.0.1:8000
   ```

**Wichtig:** Die Seite funktioniert nur, wenn sie über diese Adresse aufgerufen wird — `index.html` per Doppelklick zu öffnen (Adressleiste zeigt dann `file:///...`) funktioniert nicht, da die Daten über den lokalen Server (`/api/data`) geladen werden.

## Server stoppen

Im Terminal, in dem der Server läuft, `Strg + C` drücken.

## Benachrichtigungen bei Filter-Treffern

In den Balken-Einstellungen lässt sich jede gespeicherte Variante als Filter
**überwachen**: unter „Überwachen in diesen Zeitfenstern“ die gewünschten Zeiträume
anhaken (z.B. 10 Jahre). Überwacht wird genau, was dort angehakt ist — ☆ wählt den Filter
nur aus und markiert die Häkchen, ★ beendet die Überwachung. Der Server prüft dann
alle 5 Minuten jeden überwachten Filter gegen jeden Favoriten-Wert und meldet, sobald
der Filter im gewählten Chart anschlägt. Das läuft in `server.py`, also **auch bei
geschlossenem Browser** — der Server muss dafür laufen.

Gemeldet wird nur der *Wechsel* des Zustands, nicht bei jeder Prüfung erneut. Zusätzlich
gilt nach jeder Meldung eine **Sperre von 6 Stunden** für denselben Wert im selben Filter
und Zeitfenster: Kippt ein Indikator knapp um seine Schwelle, kommt die Meldung nicht alle
paar Minuten erneut. Ein Richtungswechsel (Kauf ↔ Verkauf) durchbricht die Sperre sofort,
und die Meldungen zu länger anhaltenden Signalen bleiben davon unberührt.

Hält ein Filter an, kommt bei 1, 2, 3, 7, 14 und 24 Einheiten je **eine** weitere Meldung
(Einheit: Stunde im Tages-, Wochen- und Monats-Chart, Handelstag bei Jahr, 5 und 10
Jahren); danach ist Schluss. Gemessen wird dabei die Uhrzeit ab dem ersten Ausschlag,
nicht in Kerzen: Ein Wert, der um 12:03 anschlägt, meldet am nächsten Handelstag gegen
12:03 — und nicht zur Börseneröffnung, wo sonst alle Werte gleichzeitig melden würden.

Die Uhr läuft **nur während der Handelszeit**: nachts (23:00–07:30), an Wochenenden und an
Feiertagen steht sie still, und in dieser Zeit wird auch nichts verschickt. Ein Ausschlag
Freitag um 12:03 meldet also Montag gegen 12:03. Maßgeblich ist der **deutsche**
Handelskalender, weil hier gehandelt wird. Die Feiertage muss niemand pflegen — der Server
erkennt sie daran, dass der DAX an dem Tag keine Kerze hat. Vor Xetra-Eröffnung gilt ein
Werktag als Handelstag, auch wenn seine DAX-Kerze noch fehlt.

Die Meldungen erscheinen optional per E-Mail und immer in der Glocke oben auf der Seite.
Es läuft immer nur eine Prüfung gleichzeitig; „sofort prüfen“ während einer laufenden
Prüfung wird danach nachgeholt.

Standardmäßig kommt die erste Meldung sofort beim Ausschlag. Wer das für einen Filter zu
häufig findet, kann in den Balken-Einstellungen unter „Überwachen in diesen Zeitfenstern“
eine **Mindestdauer** einstellen (1, 2, 3, 7 oder 14 Tage) — dann bleibt es bis dahin ganz
still, und die erste Meldung kommt erst, wenn der Filter so lange ununterbrochen
ausgeschlagen ist. Gezählt wird dabei immer in echten Handelstagen, unabhängig vom
überwachten Zeitfenster. Läuft ein Signal beim ersten Blick schon länger, meldet die erste
Mail gleich mit dem passenden „seit X Tagen“-Text.

### E-Mail einrichten (optional)

`data/mail_config.example.json` nach `data/mail_config.json` kopieren und ausfüllen.
Bei Gmail/GMX/Web.de ein **App-Passwort** verwenden, nicht das Anmeldepasswort.
Ohne diese Datei läuft alles weiter, es kommt nur keine Mail. Der Ordner `data/` wird
nie über HTTP ausgeliefert und ist von der Versionsverwaltung ausgenommen — die Datei muss
also auf jedem Gerät (auch auf dem Pi) einzeln angelegt werden, und zwar **dem Benutzer
gehörend, unter dem der Server läuft** (nicht mit `sudo` anlegen, sonst darf der Dienst sie
nicht lesen und es kommt kommentarlos keine Mail).

Jeder Treffer kommt als **eigene Mail** mit Direkt-Link zum Chart. Der Betreff enthält Wert,
Zeitfenster, Filter sowie Datum und Uhrzeit — dadurch ist er immer eindeutig, und Gmail klappt
mehrere Meldungen nicht zu einem Gesprächsverlauf zusammen. Schlagen mehrere Filter gleichzeitig
an, gehen alle Mails über eine einzige SMTP-Verbindung raus.

Ob der Versand klappt, steht unten in der Glocke („Letzte Mail: … ✓/✗" mit
Fehlertext). Zum Prüfen:

- in der Glocke auf **Test-Mail** klicken, oder
- auf dem Server `python3 server.py --mail-test` ausführen (meldet den Fehler im Klartext),
- Versandprotokoll: `data/mail_log.json`, beim Dienst zusätzlich `journalctl -u aktienanalyse`.

## Tests

```
python -m unittest discover tests
```

Die Tests brauchen weder Netz noch zusätzliche Pakete. Auf dem Pi laufen sie vor jedem
Neustart automatisch (`update.sh`); schlagen sie fehl, bleibt der alte Stand aktiv.

## Betrieb auf dem Pi

`aktienanalyse.service`, `aktienanalyse-update.service` und `aktienanalyse-update.timer`
nach `/etc/systemd/system/` **kopieren** (nicht verlinken — `<pi-user>` wird dort angepasst,
und `update.sh` setzt das Repo bei jedem Update hart auf GitHub zurück), dann
`sudo systemctl daemon-reload && sudo systemctl enable --now aktienanalyse aktienanalyse-update.timer`.
`update.sh` holt jede Minute `origin/main` und startet den Dienst nur neu, wenn sich
Python-Code geändert hat.

## Dateien

- `index.html` — Oberfläche
- `server.py` — Server: holt die Daten, berechnet die Indikatoren und überwacht die Filter
- `tests/` — Tests für `server.py`
- `data/` — Laufzeitdaten der Überwachung (Watchlist, Benachrichtigungen, Zustand, Mail-Protokoll) und die Mail-Zugangsdaten
- `feargreed_history_2011_2021.csv` — mitgelieferte Fear-&-Greed-Historie 2011–21.01.2021 (Quelle: [whit3rabbit/fear-greed-data](https://github.com/whit3rabbit/fear-greed-data)). CNNs eigene API liefert davor nur Platzhalterwerte; ab 22.01.2021 kommen alle Werte live von CNN.
