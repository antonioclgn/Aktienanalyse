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
