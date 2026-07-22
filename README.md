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

## Dateien

- `index.html` — Oberfläche
- `server.py` — lokaler Server, holt die Daten und berechnet RSI14 / 200-Tage-Durchschnitt
- `feargreed_history_2011_2020.csv` — mitgelieferte Fear-&-Greed-Historie 2011–13.07.2020. CNNs eigene API liefert selbst nichts vor dem 14.07.2020 (getestet, HTTP 500 bei früherem Startdatum); für die Zeit davor wird diese Datei genutzt (Quelle: [whit3rabbit/fear-greed-data](https://github.com/whit3rabbit/fear-greed-data), Stand beim Download geprüft gegen CNNs Live-Werte). Ab 14.07.2020 kommen alle Werte live von CNN.
