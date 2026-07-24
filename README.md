# Aktienanalyse

Zeigt für den S&P500 aktuelle Kennzahlen an:
- Fear & Greed Index (CNN)
- RSI 14
- Preis vs. 200-Tage-Durchschnitt (in %)

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

In den Balken-Einstellungen lässt sich jede gespeicherte Variante mit ☆ als Filter
**überwachen** — zusammen mit einem Zeitraum (z.B. 10 Jahre). Der Server prüft dann
alle 5 Minuten jeden überwachten Filter gegen jeden Favoriten-Wert und meldet, sobald
der Filter im gewählten Chart anschlägt. Das läuft in `server.py`, also **auch bei
geschlossenem Browser** — der Server muss dafür laufen.

Gemeldet wird nur der *Wechsel* des Zustands, nicht bei jeder Prüfung erneut. Zusätzlich
gilt nach jeder Meldung eine **Sperre von 6 Stunden** für denselben Wert im selben Filter
und Zeitfenster: Kippt ein Indikator knapp um seine Schwelle, kommt die Meldung nicht alle
paar Minuten erneut. Ein Richtungswechsel (Kauf ↔ Verkauf) durchbricht die Sperre sofort,
und die Meldungen zu länger anhaltenden Signalen bleiben davon unberührt.

Die Meldungen erscheinen als Windows-Benachrichtigung, optional per E-Mail und immer in
der Glocke oben auf der Seite.

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

## Dateien

- `index.html` — Oberfläche
- `server.py` — lokaler Server, holt die Daten, berechnet RSI14 / Durchschnitte und überwacht die Filter
- `data/` — Laufzeitdaten der Überwachung (Watchlist, Benachrichtigungen, Zustand, Mail-Protokoll) und die Mail-Zugangsdaten
- `feargreed_history_2011_2020.csv` — mitgelieferte Fear-&-Greed-Historie 2011–13.07.2020. CNNs eigene API liefert selbst nichts vor dem 14.07.2020 (getestet, HTTP 500 bei früherem Startdatum); für die Zeit davor wird diese Datei genutzt (Quelle: [whit3rabbit/fear-greed-data](https://github.com/whit3rabbit/fear-greed-data), Stand beim Download geprüft gegen CNNs Live-Werte). Ab 14.07.2020 kommen alle Werte live von CNN.
