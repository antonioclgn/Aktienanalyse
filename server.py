"""Lokaler Server: liefert index.html aus und stellt unter /api/data
Fear & Greed Index, RSI14 und den 200-Tage-Durchschnitt des S&P500 bereit.
"""
import bisect
import calendar
import csv
import json
import os
import email.utils
import smtplib
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
# Standardmäßig nur lokal erreichbar. Für den Betrieb auf einem Heimserver (z.B. Pi)
# HOST auf "0.0.0.0" setzen (Umgebungsvariable AKTIENANALYSE_HOST=0.0.0.0), damit die
# Seite im Netzwerk erreichbar ist.
HOST = os.environ.get("AKTIENANALYSE_HOST", "127.0.0.1")
PORT = int(os.environ.get("AKTIENANALYSE_PORT", "8000"))
CACHE_TTL_SECONDS = 30

# Alles, was die Überwachung dauerhaft speichert (und die Mail-Zugangsdaten), liegt
# hier. Der Ordner wird bewusst NICHT über HTTP ausgeliefert (siehe do_GET).
DATA_DIR = BASE_DIR / "data"
# Geräteübergreifend synchronisiert: Favoriten, gespeicherte Filter-Varianten und
# welche davon (mit welchen Zeitfenstern) überwacht werden. Der Server ist hierfür
# die Quelle der Wahrheit; jeder Browser lädt das beim Öffnen und schreibt Änderungen zurück.
CONFIG_FILE = DATA_DIR / "config.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"       # aus config.json abgeleitet, Basis der Überwachung
NOTIFICATIONS_FILE = DATA_DIR / "notifications.json"
ALERT_STATE_FILE = DATA_DIR / "alert_state.json"   # zuletzt gemeldeter Zustand je Filter+Wert
MAIL_CONFIG_FILE = DATA_DIR / "mail_config.json"
MAIL_LOG_FILE = DATA_DIR / "mail_log.json"         # letzte Versandversuche (Erfolg/Fehlertext)
# Nur diese Dateitypen liefert der Server mit passendem Typ aus (alles andere als
# Download-Bytes). Ohne den richtigen Typ zeigt der Browser z.B. das Symbol nicht an.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
DEFAULT_PRESET_NAME = "Standard"  # muss zu index.html passen (die implizite Standard-Variante)
ALERT_INTERVAL_SECONDS = 300
MAX_NOTIFICATIONS = 200
MAX_MAIL_LOG = 20
# Nach einer Meldung ist derselbe Wert im selben Filter+Zeitfenster für diese Zeit
# gesperrt: Kippt ein Indikator am Rand um seine Schwelle (green -> none -> green),
# gäbe es sonst alle paar Minuten dieselbe Meldung erneut. Ein Richtungswechsel
# (Kauf <-> Verkauf) durchbricht die Sperre, weil das eine echte neue Information ist.
NOTIFY_COOLDOWN_SECONDS = 6 * 3600

CNN_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
# CNNs eigene API liefert ab 14.07.2020 (ihre technische Untergrenze) bis zum 21.01.2021
# nur Platzhalterdaten (konstant 50.0 mit vereinzelten unrealistischen Ausreißern nahe 0
# -> geprüft und mit einer unabhängigen Quelle verglichen). Deshalb erst ab hier vertrauen.
CNN_FEAR_GREED_EARLIEST = "2021-01-22"
# Für die Zeit davor (bis 2011, inkl. der o.g. unzuverlässigen CNN-Phase) liegt eine
# mitgelieferte, einmalig heruntergeladene Historie bei (feargreed_history_2011_2021.csv).
FEARGREED_BUNDLED_CSV = BASE_DIR / "feargreed_history_2011_2021.csv"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}"
YAHOO_CHART_URL_PERIOD = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1={period1}&period2={period2}&interval={interval}"
)
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=8&newsCount=0"
# CFTC Commitments of Traders (wöchentlich, dienstags erhoben, freitags veröffentlicht).
# "Consolidated" fasst den großen und den E-Mini-Kontrakt zusammen und ist als einzige
# S&P-500-Reihe von 2010 bis heute lückenlos durchgehend.
CFTC_COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
CFTC_SP500_MARKET = "S&P 500 Consolidated - CHICAGO MERCANTILE EXCHANGE"
COT_PERCENTILE_WINDOW = 156  # 3 Jahre Wochenberichte
COT_PERCENTILE_WARMUP = 52   # erst ab 1 Jahr Historie einen Rang ausgeben
COT_CACHE_TTL_SECONDS = 6 * 3600  # Daten ändern sich nur einmal pro Woche
DEFAULT_SYMBOL = "SXR8.DE"  # iShares Core S&P 500 UCITS ETF USD (Acc)

# Zeitraum (fürs Frontend) -> Yahoo-Interval
CHART_RANGES = {
    "1d": "5m",
    "5d": "30m",
    "1mo": "1h",
    "1y": "1d",
    "5y": "1d",
    "10y": "1d",
}
DEFAULT_CHART_RANGE = "1y"
# Beschriftung der Zeiträume in Benachrichtigungen (identisch zu den Chart-Knöpfen).
RANGE_LABELS = {
    "1d": "Tag",
    "5d": "Woche",
    "1mo": "Monat",
    "1y": "Jahr",
    "5y": "5 Jahre",
    "10y": "10 Jahre",
}

# Für diese Zeiträume wird mehr Historie geladen als angezeigt wird (via period1/period2,
# da "range=max" bei Yahoo die Granularität stillschweigend vergröbert), damit
# RSI/200er-Durchschnitt auch am Anfang des sichtbaren Zeitraums einen Wert haben.
CHART_DISPLAY_DAYS = {
    "1d": 1,
    "5d": 5,
    "1mo": 30,
    "1y": 365,
    "5y": 5 * 365,
    "10y": 10 * 365,
}
CHART_LOOKBACK_DAYS = {
    "1d": 2,    # ~14 5-Minuten-Kerzen für den RSI-Warmup (inkl. Wochenend-Puffer)
    "5d": 3,    # ~14 30-Minuten-Kerzen für den RSI-Warmup (inkl. Wochenend-Puffer)
    "1mo": 5,   # ~14 Stundenkerzen für den RSI-Warmup (inkl. Wochenend-Puffer)
    "1y": 320,  # ~200 Handelstage inkl. Wochenenden/Feiertage
    "5y": 320,  # ~200 Handelstage inkl. Wochenenden/Feiertage
    "10y": 320,  # ~200 Handelstage inkl. Wochenenden/Feiertage
}

_cache = {}


def cached(key, ttl, fetch_fn):
    now = time.monotonic()
    entry = _cache.get(key)
    if entry and now - entry[0] < ttl:
        return entry[1]
    value = fetch_fn()
    _cache[key] = (now, value)
    return value


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def fetch_json(url, extra_headers=None):
    headers = dict(BROWSER_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, headers=headers)
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def percentile_rank(values, value):
    """Anteil der Werte in `values`, die <= `value` sind, in Prozent."""
    if not values:
        return 0.0
    return 100.0 * sum(1 for v in values if v <= value) / len(values)


def compute_rsi14(closes):
    if len(closes) < 15:
        return None

    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:14]) / 14
    avg_loss = sum(losses[:14]) / 14

    for i in range(14, len(gains)):
        avg_gain = (avg_gain * 13 + gains[i]) / 14
        avg_loss = (avg_loss * 13 + losses[i]) / 14

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_rsi_series(closes, period=14):
    result = [None] * len(closes)
    if len(closes) < period + 1:
        return result

    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        result[i + 1] = rsi

    return result


def get_market_data(symbol):
    url = YAHOO_CHART_URL.format(symbol=quote(symbol, safe=""), range="1y", interval="1d")
    data = fetch_json(url)
    result = data["chart"]["result"][0]
    meta = result["meta"]
    closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]

    current_price = meta.get("regularMarketPrice", closes[-1])
    sma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
    rsi14 = compute_rsi14(closes)

    return {
        "symbol": meta.get("symbol", symbol),
        "name": meta.get("longName") or meta.get("shortName") or meta.get("symbol", symbol),
        "currency": meta.get("currency"),
        "price": current_price,
        "sma200": sma200,
        "rsi14": rsi14,
    }


# Fenster werden in HANDELSTAGEN angegeben. Alle Zeiträume liefern inzwischen
# Tageskerzen (siehe CHART_RANGES), daher entspricht ein Fenster direkt der Kerzenzahl.
MA_WINDOW_DEFAULT = 200          # Linie im Kurschart
MA_DEVIATION_WINDOW_DEFAULT = 150  # Bezug für die Moving Average Deviation
MA_WINDOW_MIN, MA_WINDOW_MAX = 5, 400
# Zwei feste EMA-Linien im oberen Kurschart (unabhängig vom MAD-Fenster).
EMA_SHORT_PERIOD = 50
EMA_LONG_PERIOD = 200

# Bei diesen (untertägigen) Intervallen reicht die eigene Historie nie für einen
# 200-Tage-Durchschnitt. Stattdessen wird er separat aus Tageskursen berechnet und
# pro Kalendertag auf die untertägigen Punkte übertragen (siehe get_daily_moving_average_by_date).
INTRADAY_INTERVALS = {"5m", "30m", "1h"}
# ~200 Handelstage inkl. Wochenenden/Feiertage, plus Puffer, damit auch der Anfang
# des längsten untertägigen Anzeigezeitraums (Monat, 30 Tage) noch abgedeckt ist.
MA_DAILY_LOOKBACK_DAYS = 320 + 40


def compute_moving_average(closes, window):
    result = [None] * len(closes)
    if len(closes) < window:
        return result
    running_sum = sum(closes[:window])
    result[window - 1] = running_sum / window
    for i in range(window, len(closes)):
        running_sum += closes[i] - closes[i - window]
        result[i] = running_sum / window
    return result


def compute_ema_series(closes, period):
    result = [None] * len(closes)
    if len(closes) < period:
        return result
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    result[period - 1] = ema
    for i in range(period, len(closes)):
        ema = (closes[i] - ema) * multiplier + ema
        result[i] = ema
    return result


def compute_macd_series(closes, fast=12, slow=26, signal=9):
    """MACD-Linie (EMA(fast) - EMA(slow)) und ihre Signal-Linie (EMA(signal) der
    MACD-Linie). Die MACD-Linie hat vorne (slow - 1) None-Eintraege (Warmup der
    langsamen EMA) -> die Signal-EMA nur auf dem dichten Teil ab dem ersten
    validen MACD-Wert berechnen, sonst waere ihr SMA-Startwert falsch."""
    ema_fast = compute_ema_series(closes, fast)
    ema_slow = compute_ema_series(closes, slow)
    macd_line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]

    first_valid = next((i for i, v in enumerate(macd_line) if v is not None), None)
    signal_line = [None] * len(closes)
    if first_valid is not None:
        dense_signal = compute_ema_series(macd_line[first_valid:], signal)
        for offset, v in enumerate(dense_signal):
            signal_line[first_valid + offset] = v

    return macd_line, signal_line


def get_daily_moving_average_by_date(symbol, window=MA_WINDOW_DEFAULT):
    period2 = int(time.time())
    period1 = period2 - MA_DAILY_LOOKBACK_DAYS * 86400
    url = YAHOO_CHART_URL_PERIOD.format(
        symbol=quote(symbol, safe=""), period1=period1, period2=period2, interval="1d"
    )
    data = fetch_json(url)
    result = data["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0]["close"]

    points = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
    daily_timestamps = [p[0] for p in points]
    daily_closes = [p[1] for p in points]
    ma_series = compute_moving_average(daily_closes, window)

    return {
        time.strftime("%Y-%m-%d", time.gmtime(t)): ma
        for t, ma in zip(daily_timestamps, ma_series)
        if ma is not None
    }


def moving_average_for_window(days, interval, symbol, all_closes, all_timestamps):
    """Gleitender Durchschnitt über `days` Handelstage, passend zum Kerzenintervall."""
    if interval in INTRADAY_INTERVALS:
        # Untertägig reicht die eigene Historie nie: aus Tageskursen berechnen und
        # pro Kalendertag auf die untertägigen Punkte übertragen.
        by_date = cached(
            ("daily_ma", symbol, days), CACHE_TTL_SECONDS,
            lambda: get_daily_moving_average_by_date(symbol, days),
        )
        sorted_dates = sorted(by_date)
        series = []
        for t in all_timestamps:
            date_key = time.strftime("%Y-%m-%d", time.gmtime(t))
            # Vorwärts auffüllen: der letzte (z.B. noch laufende) Handelstag hat oft
            # noch keinen fertigen Tagesschlusskurs -> mit dem letzten bekannten Wert auffüllen.
            idx = bisect.bisect_right(sorted_dates, date_key) - 1
            series.append(by_date[sorted_dates[idx]] if idx >= 0 else None)
        return series

    return compute_moving_average(all_closes, days)


def get_daily_ema_by_date(symbol, period):
    """Wie get_daily_moving_average_by_date, aber EMA statt SMA. Für untertägige
    Intervalle, deren eigene Historie nie für einen 200-Perioden-EMA reicht."""
    period2 = int(time.time())
    period1 = period2 - MA_DAILY_LOOKBACK_DAYS * 86400
    url = YAHOO_CHART_URL_PERIOD.format(
        symbol=quote(symbol, safe=""), period1=period1, period2=period2, interval="1d"
    )
    data = fetch_json(url)
    result = data["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0]["close"]

    points = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
    daily_timestamps = [p[0] for p in points]
    daily_closes = [p[1] for p in points]
    ema_series = compute_ema_series(daily_closes, period)

    return {
        time.strftime("%Y-%m-%d", time.gmtime(t)): ema
        for t, ema in zip(daily_timestamps, ema_series)
        if ema is not None
    }


def ema_for_window(days, interval, symbol, all_closes, all_timestamps):
    """Exponentieller gleitender Durchschnitt über `days` Handelstage, passend zum
    Kerzenintervall — spiegelt moving_average_for_window."""
    if interval in INTRADAY_INTERVALS:
        # Untertägig aus Tageskursen berechnen und pro Kalendertag übertragen.
        by_date = cached(
            ("daily_ema", symbol, days), CACHE_TTL_SECONDS,
            lambda: get_daily_ema_by_date(symbol, days),
        )
        sorted_dates = sorted(by_date)
        series = []
        for t in all_timestamps:
            date_key = time.strftime("%Y-%m-%d", time.gmtime(t))
            idx = bisect.bisect_right(sorted_dates, date_key) - 1
            series.append(by_date[sorted_dates[idx]] if idx >= 0 else None)
        return series

    return compute_ema_series(all_closes, days)


def get_history(symbol, range_key, ma_window=MA_DEVIATION_WINDOW_DEFAULT):
    if range_key not in CHART_RANGES:
        range_key = DEFAULT_CHART_RANGE
    interval = CHART_RANGES[range_key]
    display_days = CHART_DISPLAY_DAYS.get(range_key)
    lookback_days = CHART_LOOKBACK_DAYS.get(range_key)

    if display_days and lookback_days:
        period2 = int(time.time())
        period1 = period2 - (display_days + lookback_days) * 86400
        url = YAHOO_CHART_URL_PERIOD.format(
            symbol=quote(symbol, safe=""), period1=period1, period2=period2, interval=interval
        )
    else:
        url = YAHOO_CHART_URL.format(symbol=quote(symbol, safe=""), range=range_key, interval=interval)

    data = fetch_json(url)
    result = data["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0]["close"]

    points = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
    all_timestamps = [p[0] for p in points]
    all_closes = [p[1] for p in points]

    # Ein einziger gleitender Durchschnitt über das einstellbare Fenster: er wird im
    # Kurschart gezeichnet und ist zugleich der Bezug für die Abweichung darunter.
    moving_average = moving_average_for_window(
        ma_window, interval, symbol, all_closes, all_timestamps
    )

    # Zwei feste EMA-Linien für den oberen Kurschart (unabhängig vom MAD-Fenster).
    ema_short = ema_for_window(EMA_SHORT_PERIOD, interval, symbol, all_closes, all_timestamps)
    ema_long = ema_for_window(EMA_LONG_PERIOD, interval, symbol, all_closes, all_timestamps)

    rsi_series = compute_rsi_series(all_closes, 14)
    macd_line, macd_signal_line = compute_macd_series(all_closes)
    price_vs_ma_pct = [
        (100 - (ma / close * 100)) if ma is not None else None
        for close, ma in zip(all_closes, moving_average)
    ]

    start_index = 0
    if range_key == "1d" and all_timestamps:
        # Der Tages-Chart soll am Handelsstart des letzten Handelstages beginnen.
        # Ein rollierendes 24-Stunden-Fenster würde stattdessen noch den Nachmittag
        # des Vortages mitzeigen, deshalb nach Börsen-Kalendertag abschneiden.
        gmt_offset = result.get("meta", {}).get("gmtoffset") or 0
        last_day = (all_timestamps[-1] + gmt_offset) // 86400
        start_index = next(
            (i for i, t in enumerate(all_timestamps) if (t + gmt_offset) // 86400 == last_day), 0
        )
    elif display_days and all_timestamps:
        cutoff = all_timestamps[-1] - display_days * 86400
        start_index = next((i for i, t in enumerate(all_timestamps) if t >= cutoff), 0)

    price_vs_ma_pct_display = price_vs_ma_pct[start_index:]
    avg_values = [v for v in price_vs_ma_pct_display if v is not None]
    price_vs_ma_pct_avg = sum(avg_values) / len(avg_values) if avg_values else None
    price_vs_ma_pct_std = None
    if price_vs_ma_pct_avg is not None and len(avg_values) >= 2:
        variance = sum((v - price_vs_ma_pct_avg) ** 2 for v in avg_values) / (len(avg_values) - 1)
        price_vs_ma_pct_std = variance ** 0.5

    return {
        "timestamps": all_timestamps[start_index:],
        "closes": all_closes[start_index:],
        "movingAverage": moving_average[start_index:],
        "ema50": ema_short[start_index:],
        "ema200": ema_long[start_index:],
        "maWindow": ma_window,
        "rsi": rsi_series[start_index:],
        "macd": macd_line[start_index:],
        "macdSignal": macd_signal_line[start_index:],
        "priceVsMaPct": price_vs_ma_pct_display,
        "priceVsMaPctAvg": price_vs_ma_pct_avg,
        "priceVsMaPctStd": price_vs_ma_pct_std,
    }


def search_symbols(query):
    url = YAHOO_SEARCH_URL.format(query=quote(query))
    data = fetch_json(url)
    return [
        {
            "symbol": q["symbol"],
            "name": q.get("longname") or q.get("shortname") or q["symbol"],
            "exchange": q.get("exchDisp", ""),
            "type": q.get("typeDisp", ""),
        }
        for q in data.get("quotes", [])
        if "symbol" in q
    ]


def load_bundled_feargreed_history():
    if not FEARGREED_BUNDLED_CSV.is_file():
        return []
    with open(FEARGREED_BUNDLED_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [
        {"t": calendar.timegm(time.strptime(row["date"], "%Y-%m-%d")), "score": float(row["score"])}
        for row in rows
    ]


def get_smart_dumb_money():
    """Nachbau von "Smart Money vs Dumb Money" aus den öffentlichen CFTC-COT-Daten.

    Der Originalindikator (SentimenTrader) ist proprietär; seine Grundidee lässt sich
    aber aus den wöchentlichen Positionsdaten rekonstruieren:
      Smart Money = Commercials (Absicherer mit Warenbezug, gelten als informiert)
      Dumb Money  = Non-Reportables (Kleinspekulanten unterhalb der Meldegrenze)
    Beide als Nettoposition in Prozent des Open Interest, anschließend über ein
    rollierendes Fenster in einen Perzentilrang 0..100 überführt ("Confidence").
    Hoher Smart-Wert = Profis sind long positioniert (bullisch), hoher Dumb-Wert =
    Kleinanleger sind long positioniert (üblicherweise ein Warnsignal).

    Achtung: markt-weit (S&P 500), nicht pro Einzelwert — wie Fear & Greed.
    """
    url = (
        f"{CFTC_COT_URL}?market_and_exchange_names={quote(CFTC_SP500_MARKET, safe='')}"
        "&$select=report_date_as_yyyy_mm_dd,comm_positions_long_all,comm_positions_short_all,"
        "nonrept_positions_long_all,nonrept_positions_short_all,open_interest_all"
        "&$order=report_date_as_yyyy_mm_dd ASC&$limit=5000"
    ).replace(" ", "%20")
    rows = fetch_json(url)

    points = []
    for row in rows:
        try:
            open_interest = float(row["open_interest_all"])
            if open_interest <= 0:
                continue
            commercial_net = (
                float(row["comm_positions_long_all"]) - float(row["comm_positions_short_all"])
            )
            small_net = (
                float(row["nonrept_positions_long_all"]) - float(row["nonrept_positions_short_all"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        timestamp = calendar.timegm(
            time.strptime(row["report_date_as_yyyy_mm_dd"][:10], "%Y-%m-%d")
        )
        points.append((timestamp, commercial_net / open_interest * 100, small_net / open_interest * 100))

    history = []
    for i in range(COT_PERCENTILE_WARMUP, len(points)):
        window = points[max(0, i - COT_PERCENTILE_WINDOW + 1): i + 1]
        smart = percentile_rank([p[1] for p in window], points[i][1])
        dumb = percentile_rank([p[2] for p in window], points[i][2])
        history.append({
            "t": points[i][0],
            "smart": round(smart, 1),
            "dumb": round(dumb, 1),
            "spread": round(smart - dumb, 1),
        })

    latest = history[-1] if history else None
    return {
        "history": history,
        "smart": latest["smart"] if latest else None,
        "dumb": latest["dumb"] if latest else None,
        "spread": latest["spread"] if latest else None,
        "timestamp": latest["t"] if latest else None,
    }


def get_fear_greed():
    data = fetch_json(
        f"{CNN_FEAR_GREED_URL}/{CNN_FEAR_GREED_EARLIEST}",
        {"Referer": "https://www.cnn.com/markets/fear-and-greed"},
    )
    fg = data["fear_and_greed"]
    cnn_history = data.get("fear_and_greed_historical", {}).get("data", [])
    cnn_points = [{"t": int(p["x"] / 1000), "score": p["y"]} for p in cnn_history]

    bundled_points = cached(
        "feargreed_bundled_history", 24 * 3600, load_bundled_feargreed_history
    )
    earliest_cnn_t = cnn_points[0]["t"] if cnn_points else None
    combined = [p for p in bundled_points if earliest_cnn_t is None or p["t"] < earliest_cnn_t]
    combined.extend(cnn_points)

    return {
        "score": fg["score"],
        "rating": fg["rating"],
        "timestamp": fg["timestamp"],
        "history": combined,
    }


# ---------------------------------------------------------------------------
# Überwachung der Favoriten-Filter
#
# Die Seite meldet ihre Favoriten und die als Favorit markierten Filter (samt
# Zeitraum und Schwellenwerten) an /api/alerts/watchlist. Von da an prüft dieser
# Server die Kombinationen selbst weiter — auch wenn kein Browser offen ist.
# Die Signal-Logik ist dieselbe wie im Chart: ein Signal gibt es nur, wenn ALLE
# im Filter aktivierten Indikatoren am aktuellen Rand in dieselbe Richtung zeigen.
# ---------------------------------------------------------------------------
SIGNAL_LABELS = {"green": "Kaufsignal", "red": "Verkaufssignal"}
# Hält ein Filter ununterbrochen an, gilt das als staerkeres Signal. Bei diesen
# Vielfachen der Zeiteinheit des Zeitfensters kommt je EINE zusaetzliche Meldung/Mail.
# Danach ist Schluss, damit lange Phasen nicht spammen.
DURATION_MILESTONES = [1, 2, 3, 7, 14, 24]
# Gemessen wird auf der UHR ab dem ersten Ausschlag — nicht in Kerzen. Kerzen entstehen
# zur Boerseneroeffnung, dann kaeme jede Tages-Meldung um 09:00 bzw. 15:30 und alle Werte
# gleichzeitig; so kommt sie zur Uhrzeit des Ausschlags (12:03 -> naechster Tag ~12:03).
#
# Die Uhr laeuft NUR waehrend der Handelszeit: nachts (23:00-07:30), an Wochenenden und an
# Feiertagen steht sie still. Damit kommt keine Meldung, wenn sich an der Boerse ohnehin
# nichts bewegen kann — und die Uhrzeit des Ausschlags bleibt trotzdem erhalten, weil das
# Nachtfenster jeden Tag gleich lang ist. Feiertage erkennt der Server am Kerzenraster des
# Wertes selbst (siehe trading_days_from); es gibt keine Feiertagsliste zu pflegen.
QUIET_END_SECONDS = 7 * 3600 + 30 * 60      # ab 07:30 wird gemeldet
QUIET_START_SECONDS = 23 * 3600             # ab 23:00 ist Ruhe
OPEN_SECONDS_PER_DAY = QUIET_START_SECONDS - QUIET_END_SECONDS   # 15,5 Stunden Handelszeit
DURATION_UNITS = {                          # (Handelszeit-Sekunden, Einzahl, Mehrzahl im Dativ)
    "1d": (3600, "Stunde", "Stunden"),      # 5-Minuten-Kerzen -> Stunden als Einheit
    "5d": (3600, "Stunde", "Stunden"),      # 30-Minuten-Kerzen
    "1mo": (3600, "Stunde", "Stunden"),     # Stundenkerzen
    "1y": (OPEN_SECONDS_PER_DAY, "Tag", "Tagen"),          # 1 Handelstag
    "5y": (OPEN_SECONDS_PER_DAY, "Tag", "Tagen"),          # 1 Handelstag
    "10y": (OPEN_SECONDS_PER_DAY, "Tag", "Tagen"),         # 1 Handelstag
}
DEFAULT_DURATION_UNIT = (OPEN_SECONDS_PER_DAY, "Tag", "Tagen")
# Zeitfenster, deren Kerzenraster den Handelskalender weit genug abdeckt, um Feiertage
# daran zu erkennen. Der Tages-Chart liefert nur den letzten Handelstag, sonst
# entscheidet dort allein Mo-Fr.
TRADING_GRID_RANGES = ("5d", "1mo", "1y", "5y", "10y")


def milestone_label(units, range_key):
    """z.B. (7, '1y') -> '7 Tagen', (1, '10y') -> '1 Woche'."""
    _, singular, plural = DURATION_UNITS.get(range_key, DEFAULT_DURATION_UNIT)
    return f"1 {singular}" if units == 1 else f"{units} {plural}"


def milestone_seconds(range_key):
    """Laenge EINER Einheit des Zeitfensters in Werktags-Sekunden."""
    return DURATION_UNITS.get(range_key, DEFAULT_DURATION_UNIT)[0]
# ---------------------------------------------------------------------------
# Indikator-Registry
#
# Jeder Indikator ist EIN Eintrag: Aktiv-Schalter, Standard-Werte und eine Funktion,
# die pro Kerze 'green'/'red'/None liefert. Einen neuen Indikator fügt man hier (und
# spiegelbildlich in index.html) hinzu — Defaults, Kombinieren und Überwachung ergeben
# sich generisch daraus. Bestehende Filter bleiben unberührt: fehlt einem alten Filter
# der Aktiv-Schalter eines neuen Indikators, ist er per Default aus und zählt nicht mit.
# ---------------------------------------------------------------------------

def _classify(buy, sell):
    return "green" if buy else ("red" if sell else None)


def _threshold_signals(values, buy_ok, sell_ok):
    """Pro Kerze: buy_ok(v) -> green, sell_ok(v) -> red, fehlender Wert -> None."""
    return [None if v is None else _classify(buy_ok(v), sell_ok(v)) for v in values]


def _feargreed_signals(ctx, s):
    return _threshold_signals(ctx["fg"], lambda v: v <= s["fearGreedBuy"], lambda v: v >= s["fearGreedSell"])


def _smartdumb_signals(ctx, s):
    return _threshold_signals(ctx["sd"], lambda v: v >= s["smartDumbBuy"], lambda v: v <= s["smartDumbSell"])


def _rsi_signals(ctx, s):
    return _threshold_signals(ctx["rsi"], lambda v: v <= s["rsiBuy"], lambda v: v >= s["rsiSell"])


def _pma_signals(ctx, s):
    """Abweichung vs. laufendem Durchschnitt außerhalb des Bandes (± n·Std)."""
    pma, trend, std = ctx["pma"], ctx["trend"], ctx["std"]
    if std is None:
        return [None] * len(pma)  # ohne Standardabweichung kein Band -> kein Signal
    distance = std * s["pmaStdMultiplier"]
    out = []
    for i, v in enumerate(pma):
        if v is not None and i < len(trend) and trend[i] is not None:
            out.append(_classify(v < trend[i] - distance, v > trend[i] + distance))
        else:
            out.append(None)
    return out


def _macd_signals(ctx, s):
    """Histogramm (MACD-Linie minus Signallinie) gegen die Schwellenwerte - Standard
    0/0 entspricht dem klassischen bullish/bearish MACD-Kreuzsignal."""
    macd, signal = ctx["macd"], ctx["macdSignal"]
    histogram = [
        (m - sg) if (m is not None and sg is not None) else None
        for m, sg in zip(macd, signal)
    ]
    return _threshold_signals(histogram, lambda v: v >= s["macdBuy"], lambda v: v <= s["macdSell"])


INDICATORS = [
    {"key": "feargreed", "enabled_key": "useFearGreed", "signals": _feargreed_signals,
     "defaults": {"useFearGreed": True, "fearGreedBuy": 25, "fearGreedSell": 75}},
    {"key": "smartdumb", "enabled_key": "useSmartDumb", "signals": _smartdumb_signals,
     "defaults": {"useSmartDumb": True, "smartDumbBuy": 50, "smartDumbSell": -50}},
    {"key": "rsi", "enabled_key": "useRsi", "signals": _rsi_signals,
     "defaults": {"useRsi": True, "rsiBuy": 30, "rsiSell": 70}},
    {"key": "pma", "enabled_key": "usePma", "signals": _pma_signals,
     "defaults": {"usePma": True, "pmaStdMultiplier": 1, "maDeviationWindow": MA_DEVIATION_WINDOW_DEFAULT}},
    {"key": "macd", "enabled_key": "useMacd", "signals": _macd_signals,
     "defaults": {"useMacd": False, "macdBuy": 0, "macdSell": 0}},
]

# Aus der Registry abgeleitet — nicht von Hand pflegen. Muss zu index.html passen.
DEFAULT_FILTER_SETTINGS = {k: v for ind in INDICATORS for k, v in ind["defaults"].items()}
INDICATOR_ENABLED_KEY = {ind["key"]: ind["enabled_key"] for ind in INDICATORS}

_file_lock = threading.Lock()


def read_json_file(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def write_json_file(path, value):
    DATA_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def forward_filled(times, values, timestamp):
    """Letzter bekannter Wert zu einem Zeitpunkt (Fear & Greed täglich, COT wöchentlich)."""
    index = bisect.bisect_right(times, timestamp) - 1
    return values[index] if index >= 0 else None


def trading_days_from(data, range_key):
    """Handelstage (als lokale Kalendertage) aus dem Kerzenraster eines Wertes.

    Das ist der echte Handelskalender der Börse dieses Wertes — Feiertage inklusive, ohne
    eigene Feiertagsliste: an einem Tag ohne Handel liefert Yahoo keine Kerze. Taugt nur
    für Zeitfenster mit ausreichend weitem Tagesraster (TRADING_GRID_RANGES), sonst None
    — dann entscheidet allein Mo-Fr."""
    if range_key not in TRADING_GRID_RANGES:
        return None
    days = set()
    for stamp in data.get("timestamps") or []:
        local = time.localtime(stamp)
        days.add((local.tm_year, local.tm_mon, local.tm_mday))
    return days or None


def is_trading_day(local, trading_days):
    """`local` ist ein time.struct_time. Ohne bekanntes Raster gilt schlicht Mo-Fr."""
    if trading_days is None:
        return local.tm_wday < 5
    return (local.tm_year, local.tm_mon, local.tm_mday) in trading_days


def in_trading_window(timestamp, trading_days=None):
    """Läuft die Dauer-Uhr zu diesem Zeitpunkt? Handelstag und nicht Nachtruhe."""
    local = time.localtime(timestamp)
    if not is_trading_day(local, trading_days):
        return False
    daytime = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
    return QUIET_END_SECONDS <= daytime < QUIET_START_SECONDS


def align_to_trading_window(timestamp, trading_days=None):
    """Einen Zeitpunkt in die Handelszeit schieben: liegt er nachts, am Wochenende oder an
    einem Feiertag, zählt die Dauer erst ab dem Beginn des nächsten Meldefensters. Nötig,
    weil der Beginn einer Serie aus der Historie kommen kann (Kerzen-Zeitstempel liegen
    z.B. auf Montag 00:00) — ohne das Verschieben wäre „1 Tag" mal ein ganzer Handelstag
    und mal nur dessen Rest."""
    for _ in range(14):                     # genug für Wochenende plus Feiertage
        local = time.localtime(timestamp)
        daytime = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
        midnight = timestamp - daytime
        if is_trading_day(local, trading_days):
            if daytime < QUIET_END_SECONDS:
                return midnight + QUIET_END_SECONDS
            if daytime < QUIET_START_SECONDS:
                return timestamp
        timestamp = midnight + 86400 + QUIET_END_SECONDS   # Folgetag, Beginn des Fensters
    return timestamp


def milestone_reached(since, now, units, range_key, trading_days=None):
    """Ist der Dauer-Meilenstein `units` seit `since` überschritten?

    Bewusst „mehr als" statt „mindestens": genau erreicht ist eine Tages-Schwelle schon am
    Ende des Handelstages, an dem die Uhr gestartet ist (zwischen 23:00 und 07:30 vergeht
    keine Handelszeit, der Wert bleibt über Nacht stehen). Die Meldung soll aber erst am
    Folgetag zur Uhrzeit des Ausschlags kommen."""
    return open_seconds(since, now, trading_days) > units * milestone_seconds(range_key)


def open_seconds(start, end, trading_days=None):
    """Vergangene HANDELSZEIT zwischen zwei Zeitpunkten: ohne Nächte (23:00-07:30), ohne
    Wochenenden und (bei bekanntem Raster) ohne Feiertage.

    Damit bleibt die Uhrzeit des Ausschlags erhalten, weil das Nachtfenster jeden Tag
    gleich lang ist: Di 12:03 + 1 Handelstag = Mi 12:03, Fr 12:03 + 1 Handelstag = Mo 12:03.
    Durchlaufen wird kalendertageweise in Ortszeit (max. ~35 Runden für den größten
    Meilenstein)."""
    total = 0
    cursor = start
    while cursor < end:
        local = time.localtime(cursor)
        daytime = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
        midnight = cursor - daytime
        day_end = midnight + 86400          # Beginn des lokalen Folgetages
        chunk_end = min(day_end, end)
        if is_trading_day(local, trading_days):
            # Schnittmenge des Tagesstücks mit dem Meldefenster dieses Tages.
            overlap = (min(chunk_end, midnight + QUIET_START_SECONDS)
                       - max(cursor, midnight + QUIET_END_SECONDS))
            total += max(0, overlap)
        cursor = chunk_end
    return total


def cumulative_trend(series):
    """Laufender Durchschnitt series[0..i] — dieselbe Trendlinie wie im Chart."""
    result = []
    total, count = 0.0, 0
    for value in series or []:
        if value is not None:
            total += value
            count += 1
        result.append(total / count if count else None)
    return result


def _indicator_context(data, timestamps, fg_times, fg_scores, sd_times, sd_spreads):
    """Alle Reihen, die die Indikator-Signalfunktionen brauchen, einmal vorberechnet
    (pro Kerze) — Fear & Greed / Smart-Dumb vorwärts-aufgefüllt, RSI/Abweichung direkt."""
    rsi_all = data.get("rsi") or []
    price_vs_ma = data.get("priceVsMaPct") or []
    macd_all = data.get("macd") or []
    macd_signal_all = data.get("macdSignal") or []
    count = len(timestamps)
    return {
        "fg": [forward_filled(fg_times, fg_scores, t) for t in timestamps],
        "sd": [forward_filled(sd_times, sd_spreads, t) for t in timestamps],
        "rsi": [rsi_all[i] if i < len(rsi_all) else None for i in range(count)],
        "pma": [price_vs_ma[i] if i < len(price_vs_ma) else None for i in range(count)],
        "trend": cumulative_trend(price_vs_ma),
        "std": data.get("priceVsMaPctStd"),
        "macd": [macd_all[i] if i < len(macd_all) else None for i in range(count)],
        "macdSignal": [macd_signal_all[i] if i < len(macd_signal_all) else None for i in range(count)],
    }


def combined_signal_series(data, settings, fg_times, fg_scores, sd_times, sd_spreads):
    """Kombiniertes Signal ('green'/'red'/None) für JEDEN Balken der Historie.

    Ein Balken ist grün/rot nur, wenn ALLE im Filter aktivierten Indikatoren dort in
    dieselbe Richtung zeigen. Generisch über die Indikator-Registry — je Indikator
    eine Signal-Reihe, dann Kerze für Kerze zusammengeführt."""
    timestamps = data.get("timestamps") or []
    enabled = [ind for ind in INDICATORS if settings.get(ind["enabled_key"])]
    if not timestamps or not enabled:
        return []

    ctx = _indicator_context(data, timestamps, fg_times, fg_scores, sd_times, sd_spreads)
    per_indicator = [ind["signals"](ctx, settings) for ind in enabled]

    result = []
    for i in range(len(timestamps)):
        bar = [series[i] for series in per_indicator]
        if all(x == "green" for x in bar):
            result.append("green")
        elif all(x == "red" for x in bar):
            result.append("red")
        else:
            result.append(None)
    return result


def evaluate_signal(data, settings, fg_times, fg_scores, sd_times, sd_spreads):
    """'green', 'red' oder None für den letzten Datenpunkt einer Kurshistorie."""
    series = combined_signal_series(data, settings, fg_times, fg_scores, sd_times, sd_spreads)
    return series[-1] if series else None


def evaluate_signal_and_run(data, settings, fg_times, fg_scores, sd_times, sd_spreads):
    """Liefert (signal, kennung, beginn) für den letzten Balken einer Kurshistorie.

    `kennung` ist der Zeitstempel der letzten Kerze VOR der Serie gleicher Richtung. Damit
    erkennt die Überwachung dieselbe Serie wieder, wenn ein Signal kurz wegkippt und
    zurückkommt. Bewusst nicht die erste Kerze der Serie: die laufende (noch offene) Kerze
    trägt bei Yahoo die letzte Handelszeit und wandert zwischen zwei Abrufen — die Kerze
    davor ist abgeschlossen und deshalb stabil.

    `beginn` ist der Zeitstempel der ERSTEN Kerze der Serie, aber 0, wenn die Serie nur aus
    der aktuell offenen Kerze besteht. 0 heißt „kein belastbarer Beginn aus der Historie" —
    dann zählt der Aufrufer ab dem Zeitpunkt der Entdeckung. Genau so bekommt ein frischer
    Ausschlag die Uhrzeit, zu der er wirklich auftrat (12:03), und nicht die der
    Kerzen-Eröffnung (09:00). Sieht der Server dagegen eine schon länger laufende Serie zum
    ersten Mal, holt er die verstrichene Dauer aus der Historie."""
    series = combined_signal_series(data, settings, fg_times, fg_scores, sd_times, sd_spreads)
    if not series:
        return None, 0, 0
    signal = series[-1]
    if signal is None:
        return None, 0, 0
    index = len(series) - 2
    while index >= 0 and series[index] == signal:
        index -= 1
    timestamps = data.get("timestamps") or []
    # Nach der Schleife zeigt `index` genau auf die letzte Kerze, die NICHT zur Serie gehört.
    anchor = timestamps[index] if 0 <= index < len(timestamps) else 0
    started = index + 1
    # Nur die offene Kerze -> kein belastbarer Beginn (siehe oben).
    begun = timestamps[started] if started < len(timestamps) - 1 else 0
    return signal, anchor, begun


def send_windows_toast(title, body):
    """Windows-Benachrichtigung über die Bordmittel von PowerShell (ohne Zusatzmodul)."""
    if os.name != "nt":
        return
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] | Out-Null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        "$x.Item(0).AppendChild($t.CreateTextNode($env:ALERT_TITLE)) | Out-Null;"
        "$x.Item(1).AppendChild($t.CreateTextNode($env:ALERT_BODY)) | Out-Null;"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'"
        ").Show($toast)"
    )
    env = dict(os.environ, ALERT_TITLE=title, ALERT_BODY=body)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env, capture_output=True, timeout=20, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Windows-Benachrichtigung fehlgeschlagen: {exc}")


def load_mail_config():
    """Mail-Konfiguration lesen. Liefert (config, fehlertext). Wichtig ist die
    Unterscheidung: gar nicht eingerichtet, vorhanden aber nicht lesbar (typisch:
    mit sudo angelegt, der Dienstbenutzer darf nicht) oder unvollständig. Früher
    sahen alle drei Fälle gleich aus — nämlich wie „es kommt einfach keine Mail"."""
    if not MAIL_CONFIG_FILE.is_file():
        return None, "nicht eingerichtet (data/mail_config.json fehlt)"
    try:
        config = json.loads(MAIL_CONFIG_FILE.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"data/mail_config.json nicht lesbar: {exc}"
    except ValueError as exc:
        return None, f"data/mail_config.json ist kein gültiges JSON: {exc}"
    if not isinstance(config, dict):
        return None, "data/mail_config.json enthält kein Objekt"
    missing = [key for key in ("host", "to") if not config.get(key)]
    if missing:
        return None, f"data/mail_config.json unvollständig: {', '.join(missing)} fehlt"
    return config, None


def log_mail_attempt(subject, ok, error):
    """Jeden Versuch festhalten — im Log des Dienstes UND in data/mail_log.json,
    damit die Seite (Glocke) den letzten Stand anzeigen kann."""
    stamp = time.strftime("%d.%m. %H:%M:%S")
    # flush: als Dienst ist die Ausgabe gepuffert, sonst steht im journal erst viel später etwas.
    print(f"[{stamp}] Mail „{subject}“: " + ("verschickt" if ok else f"FEHLER — {error}"), flush=True)
    entry = {"ts": int(time.time()) * 1000, "ok": ok, "subject": subject, "error": error}
    with _file_lock:
        existing = read_json_file(MAIL_LOG_FILE, [])
        if not isinstance(existing, list):
            existing = []
        write_json_file(MAIL_LOG_FILE, ([entry] + existing)[:MAX_MAIL_LOG])


def build_message(config, subject, body):
    """Eine vollwertig eigenständige Nachricht. Eigene Message-ID und Datum, und
    bewusst KEIN In-Reply-To/References — sonst hängt Gmail die Mail an einen
    bestehenden Verlauf an, statt sie einzeln im Posteingang zu zeigen."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.get("from") or config.get("user")
    message["To"] = config["to"]
    message["Date"] = email.utils.formatdate(localtime=True)
    message["Message-ID"] = email.utils.make_msgid(domain="aktienanalyse.local")
    message.set_content(body)
    return message


def send_mails(items):
    """Verschickt je Eintrag (betreff, text) eine EIGENE Mail — aber alle über eine
    einzige SMTP-Verbindung, damit ein Durchlauf mit mehreren Treffern nicht als
    Anmelde-Schwall bei Gmail auffällt. Liefert (ok, fehlertext); Fehler landen im
    Protokoll und sind über /api/alerts/notifications auf der Seite sichtbar."""
    if not items:
        return True, None
    config, config_error = load_mail_config()
    if config_error:
        for subject, _ in items:
            log_mail_attempt(subject, False, config_error)
        return False, config_error

    port = int(config.get("port", 465))
    # "ssl" (üblich auf Port 465), "starttls" (üblich auf 587) oder "none".
    security = config.get("security") or ("ssl" if port == 465 else "starttls")

    def attempt():
        server = (smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP)(
            config["host"], port, timeout=30
        )
        with server:
            if security == "starttls":
                server.starttls()
            if config.get("user"):
                server.login(config["user"], config.get("password", ""))
            for subject, body in items:
                server.send_message(build_message(config, subject, body))

    error = None
    for tries_left in (1, 0):
        try:
            attempt()
            for subject, _ in items:
                log_mail_attempt(subject, True, None)
            return True, None
        except smtplib.SMTPException as exc:
            # Anmelde-/Protokollfehler wiederholen sich beim zweiten Versuch genauso.
            error = f"{type(exc).__name__}: {exc}"
            break
        except OSError as exc:
            # Netz/TLS — auf dem Pi durchaus mal vorübergehend. Einmal nachfassen.
            error = f"{type(exc).__name__}: {exc}"
            if tries_left:
                time.sleep(5)
    for subject, _ in items:
        log_mail_attempt(subject, False, error)
    return False, error


def send_mail(subject, body):
    """Einzelne Mail (Testmail) — dieselbe Strecke wie die Meldungen."""
    return send_mails([(subject, body)])


def mail_status():
    """Für die Seite: ist Mail eingerichtet und wie lief der letzte Versand?"""
    config, config_error = load_mail_config()
    log = read_json_file(MAIL_LOG_FILE, [])
    last = log[0] if isinstance(log, list) and log else None
    return {
        "configured": config is not None,
        "configError": config_error,
        "to": (config or {}).get("to"),
        "lastOk": last.get("ok") if last else None,
        "lastTs": last.get("ts") if last else None,
        "lastError": last.get("error") if last else None,
    }


def notification_subject(entry):
    """Betreff der Mail. Bewusst für JEDE Meldung eindeutig: Gmail fasst Nachrichten mit
    gleichem Betreff zu einem Gesprächsverlauf zusammen, dann klappen mehrere Signale zu
    einer Zeile zusammen. Wert, Zeitfenster, Filter und Zeitpunkt machen ihn unterscheidbar."""
    title, _ = notification_text(entry)
    label = RANGE_LABELS.get(entry["range"], entry["range"])
    stamp = time.strftime("%d.%m. %H:%M", time.localtime(entry.get("ts", 0) / 1000 or time.time()))
    return f"{title} · {label} · {entry['filter']} · {stamp}"


def notification_text(entry):
    label = RANGE_LABELS.get(entry["range"], entry["range"])
    signal = SIGNAL_LABELS.get(entry["type"], "Signal")
    name = entry.get("name") or entry["symbol"]
    duration = entry.get("sustainedLabel")
    if duration:
        title = f"Anhaltendes {signal} (seit {duration}): {name}"
        body = (
            f"Filter „{entry['filter']}“ schlägt seit {duration} ununterbrochen im "
            f"{label}-Chart an ({entry['symbol']}). Ein länger anhaltendes Signal gilt als stärker."
        )
    else:
        title = f"{signal}: {name}"
        body = f"Filter „{entry['filter']}“ schlägt im {label}-Chart an ({entry['symbol']})."
    return title, body


def _local_ip():
    """LAN-IP dieses Rechners (ohne echten Verbindungsaufbau) — für Links in der Mail."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return "127.0.0.1"


def notification_base_url():
    """Basis-URL für die Links in den Mails. In data/mail_config.json per "baseUrl"
    überschreibbar; standardmäßig die feste Adresse des Raspberry Pi."""
    config = read_json_file(MAIL_CONFIG_FILE, None) or {}
    base = (config.get("baseUrl") or "").strip().rstrip("/")
    return base or f"http://192.168.178.134:{PORT}"


def notification_link(entry):
    """Deep-Link, der die Seite öffnet und Asset, Zeitfenster und Filter vorwählt."""
    return (
        f"{notification_base_url()}/?symbol={quote(entry['symbol'], safe='')}"
        f"&range={quote(entry['range'], safe='')}"
        f"&filter={quote(entry['filter'], safe='')}"
    )


def alert_mail(entry):
    """(betreff, text) für EINE Meldung — mit Direkt-Link, der Asset, Zeitfenster und
    Filter auf der Seite vorwählt."""
    _, body = notification_text(entry)
    return notification_subject(entry), f"{body}\n\nDirekt öffnen: {notification_link(entry)}"


def send_test_mail():
    """Testmail (Knopf in der Glocke bzw. `server.py --mail-test`). Gibt den echten
    Fehlertext zurück, damit auf dem Pi sofort klar ist, woran es hakt."""
    body = (
        "Testmail der Aktienanalyse. Wenn diese Mail ankommt, funktioniert der "
        "Versand für die Filter-Meldungen genauso.\n\n"
        f"Seite öffnen: {notification_base_url()}/"
    )
    # Zeitstempel auch hier, sonst stapelt Gmail die Testmails untereinander.
    return send_mail(f"Aktienanalyse: Testmail · {time.strftime('%d.%m. %H:%M')}", body)


def normalize_state_entry(value):
    """Zustand je Filter+Zeitfenster+Wert: {signal, notifiedMilestones, notifiedSignal,
    notifiedAt, runAnchor, runSince} — welche Dauer-Meilensteine schon gemeldet wurden,
    welche Richtung zuletzt tatsächlich gemeldet wurde und wann (für die 6-Stunden-Sperre),
    die Kennung der gemeldeten Serie und der Zeitpunkt, ab dem ihre Dauer zählt (beides
    siehe evaluate_signal_and_run). Ältere Stände (reiner String, oder ohne die neueren
    Felder) werden verträglich übernommen: ohne Zeitstempel gilt die Sperre schlicht als
    abgelaufen, und runAnchor 0 trifft nie auf eine echte Kerze — dann entscheidet allein
    die Sperre wie bisher. `notifiedBars` ist der alte Name von `notifiedMilestones`; er
    wird weiter gelesen, damit schon gemeldete Meilensteine nicht erneut fällig werden."""
    empty = {"signal": "none", "notifiedMilestones": [], "notifiedSignal": "none",
             "notifiedAt": 0, "runAnchor": 0, "runSince": 0}
    if isinstance(value, dict):
        return {
            "signal": value.get("signal", "none"),
            "notifiedMilestones": (value.get("notifiedMilestones")
                                   or value.get("notifiedBars") or []),
            "notifiedSignal": value.get("notifiedSignal") or "none",
            "notifiedAt": value.get("notifiedAt") or 0,
            "runAnchor": value.get("runAnchor") or 0,
            "runSince": value.get("runSince") or 0,
        }
    if isinstance(value, str):
        return {**empty, "signal": value}
    return empty


def store_notifications(entries):
    with _file_lock:
        existing = read_json_file(NOTIFICATIONS_FILE, [])
        if not isinstance(existing, list):
            existing = []
        write_json_file(NOTIFICATIONS_FILE, (entries + existing)[:MAX_NOTIFICATIONS])


def derive_watchlist(config):
    """Aus der geräteübergreifenden Config (Favoriten, Varianten, überwachte Filter)
    die watchlist.json bauen, die die Überwachung nutzt: je überwachtem Filter die
    aufgelösten Schwellenwerte der zugehörigen Variante."""
    favorites = config.get("favorites") or []
    presets = config.get("presets") or {}
    watched = config.get("watched") or []
    filters = []
    for entry in watched:
        name = entry.get("name")
        if not name:
            continue
        if name in presets:
            settings = presets[name]
        elif name == DEFAULT_PRESET_NAME:
            settings = DEFAULT_FILTER_SETTINGS  # implizite Standard-Variante
        else:
            continue  # Variante gelöscht -> Überwachung entfällt
        filters.append({
            "name": name, "ranges": entry.get("ranges") or [], "settings": settings,
            "minDelayDays": entry.get("minDelayDays") or 0,
        })
    return {"favorites": favorites, "filters": filters, "updated": int(time.time())}


def run_alert_check(force=False):
    """Ein Durchlauf: jeder überwachte Filter gegen jeden Favoriten.

    Außerhalb der Handelszeit wird nicht geprüft und deshalb auch nichts gemeldet — ein
    Signal, das nachts oder am Wochenende entsteht, kommt beim ersten Durchlauf im
    Meldefenster. `force=True` übergeht das (Knopf „sofort prüfen" auf der Seite: wer
    ausdrücklich prüft, will auch nachts eine Antwort)."""
    watchlist = read_json_file(WATCHLIST_FILE, {}) or {}
    favorites = watchlist.get("favorites") or []
    filters = watchlist.get("filters") or []
    # Nur Favoriten mit eingeschalteter Benachrichtigung prüfen. Fehlt das Flag
    # (Altbestand), gilt es als AN — so verliert niemand bestehende Meldungen.
    favorites = [f for f in favorites if f.get("notify", True)]
    if not favorites or not filters:
        return []
    # Nachtruhe und Wochenende gelten für alle Werte gleich — dann gar nicht erst bei
    # Yahoo anfragen. Feiertage haengen am einzelnen Wert und werden unten geprüft.
    if not force and not in_trading_window(int(time.time())):
        return []

    fear_greed = cached("fear_greed", CACHE_TTL_SECONDS, get_fear_greed)
    smart_dumb = cached("smart_dumb", COT_CACHE_TTL_SECONDS, get_smart_dumb_money)
    fg_history = fear_greed.get("history") or []
    sd_history = (smart_dumb or {}).get("history") or []
    fg_times = [p["t"] for p in fg_history]
    fg_scores = [p["score"] for p in fg_history]
    sd_times = [p["t"] for p in sd_history]
    sd_spreads = [p["spread"] for p in sd_history]

    previous = read_json_file(ALERT_STATE_FILE, {}) or {}
    current = dict(previous)
    fresh = []

    for entry in filters:
        name = entry.get("name")
        if not name:
            continue
        settings = {**DEFAULT_FILTER_SETTINGS, **(entry.get("settings") or {})}
        # Mindestdauer, bevor überhaupt eine erste Meldung kommt (0 = sofort, wie bisher).
        # Immer in echten Handelstagen gemessen, unabhängig vom überwachten Zeitfenster.
        min_delay_days = entry.get("minDelayDays") or 0
        # Ein Filter kann mehrere Zeitfenster überwachen (z.B. 5 und 10 Jahre);
        # ältere Stände hatten nur ein einzelnes "range".
        raw_ranges = entry.get("ranges")
        if not isinstance(raw_ranges, list):
            raw_ranges = [entry.get("range")]
        range_keys = [r for r in raw_ranges if r in CHART_RANGES]
        if not range_keys:
            range_keys = [DEFAULT_CHART_RANGE]
        ma_window = max(MA_WINDOW_MIN, min(MA_WINDOW_MAX, int(settings["maDeviationWindow"])))

        for range_key in range_keys:
            for favorite in favorites:
                symbol = (favorite.get("symbol") or "").strip()
                if not symbol:
                    continue
                key = f"{name}|{range_key}|{symbol}"
                try:
                    data = cached(
                        ("history", symbol, range_key, ma_window), CACHE_TTL_SECONDS,
                        lambda s=symbol, r=range_key: get_history(s, r, ma_window),
                    )
                    signal, run_anchor, run_begun = evaluate_signal_and_run(
                        data, settings, fg_times, fg_scores, sd_times, sd_spreads
                    )
                except Exception as exc:
                    # Alten Zustand behalten: sonst käme die Meldung beim nächsten
                    # erfolgreichen Durchlauf ein zweites Mal.
                    print(f"Prüfung {key} fehlgeschlagen: {exc}")
                    continue

                # Handelt dieser Wert heute überhaupt? Das Kerzenraster kennt die Feiertage
                # seiner Börse. An einem geschlossenen Tag bleibt der Zustand unberührt —
                # gemeldet wird dann beim ersten Durchlauf am nächsten Handelstag.
                trading = trading_days_from(data, range_key)
                now = int(time.time())
                if not force and not in_trading_window(now, trading):
                    continue

                prev = normalize_state_entry(previous.get(key))

                def make_note(sustained_units=None):
                    note = {
                        "ts": now * 1000,
                        "symbol": symbol,
                        "name": favorite.get("name") or symbol,
                        "filter": name,
                        "range": range_key,
                        "type": signal,
                        "read": False,
                    }
                    if sustained_units is not None:
                        note["sustainedUnits"] = sustained_units
                        note["sustainedLabel"] = milestone_label(sustained_units, range_key)
                    return note

                def fire_due_milestones(already, since, first_alert=False):
                    """Meilensteine melden, die die Serie erreicht hat. Gezählt wird die
                    HANDELSZEIT seit `since` (ohne Nächte, Wochenenden und Feiertage) — so
                    kommt die Tages-Meldung zur Uhrzeit des Ausschlags und nicht zur
                    Börseneröffnung. Sind mehrere Schwellen auf einmal fällig — typisch, wenn
                    der Server eine schon laufende Serie zum ersten Mal sieht —, kommt NUR die
                    höchste: sie enthält die Aussage der kleineren. Sonst kämen mehrere Mails
                    gleichzeitig, die dasselbe sagen. `first_alert` ist die Sofortmeldung eines
                    neuen Ausschlags; sie entfällt, wenn ohnehin eine Dauer-Meldung rausgeht.

                    Ist eine Mindestdauer eingestellt (`min_delay_days`), bleibt es bis dahin
                    ganz still — auch die Sofortmeldung entfällt. Läuft die Serie beim ersten
                    Blick schon länger als die Mindestdauer, meldet die erste Mail gleich mit
                    dem passenden „seit X Tagen"-Text (über die normale Meilenstein-Logik unten)."""
                    if min_delay_days and open_seconds(since, now, trading) < min_delay_days * OPEN_SECONDS_PER_DAY:
                        return already
                    due = [m for m in DURATION_MILESTONES
                           if m not in already
                           and milestone_reached(since, now, m, range_key, trading)]
                    if due:
                        fresh.append(make_note(sustained_units=max(due)))
                    elif first_alert:
                        fresh.append(make_note())
                    return list(already) + due

                def keep_notified(state_signal, milestones, anchor=None, since=None):
                    """Zustand fortschreiben, ohne Sperr-, Meilenstein- und Dauer-Gedächtnis zu
                    verlieren. Ohne `anchor`/`since` bleiben die gemerkten Werte stehen — beim
                    Wegkippen des Signals gibt es keine neuen, und genau sie werden später zum
                    Wiedererkennen derselben Serie und für ihre Dauer gebraucht."""
                    return {
                        "signal": state_signal,
                        "notifiedMilestones": milestones,
                        "notifiedSignal": prev["notifiedSignal"],
                        "notifiedAt": prev["notifiedAt"],
                        "runAnchor": prev["runAnchor"] if anchor is None else anchor,
                        "runSince": prev["runSince"] if since is None else since,
                    }

                cooling = now - prev["notifiedAt"] < NOTIFY_COOLDOWN_SECONDS
                # Serien-Vergleich: gleiche Kennung = nachweislich dieselbe Serie. Eine ANDERE
                # heißt, die Serie hat neu begonnen — auch dann, wenn der Server die Pause
                # zwischen zwei Prüfungen gar nicht zu sehen bekam (bei kurzen Kerzen oder nach
                # einem Neustart durchaus möglich). Alte Zustände ohne runAnchor (0) lassen
                # keinen Vergleich zu; dort entscheiden wie bisher letzter Zustand und Sperre.
                same_run = bool(run_anchor) and prev["runAnchor"] == run_anchor
                restarted = bool(run_anchor) and bool(prev["runAnchor"]) and not same_run
                # Fortsetzung statt neuem Ausschlag: entweder unverändert am Ausschlagen, oder
                # dieselbe Richtung ist nach kurzem Wegkippen zurück — nachweislich dieselbe
                # Kerzen-Serie oder noch innerhalb der Sperre (Zappeln um die Schwelle).
                continues = (prev["signal"] == signal and not restarted) or (
                    prev["notifiedSignal"] == signal and (cooling or same_run)
                )
                # Ab wann die Dauer zählt. Fortsetzung: die gemerkte Uhr weiterlaufen lassen;
                # Altbestand ohne runSince fällt auf den Zeitpunkt der ersten Meldung zurück,
                # ganz alte Stände auf jetzt — so wird beim Deployen nichts rückwirkend fällig.
                resumed_since = align_to_trading_window(
                    prev["runSince"] or prev["notifiedAt"] or now, trading
                )
                # Neuer Ausschlag: Beginn aus der Historie, sonst die Minute der Entdeckung.
                # Letzteres ist der Normalfall und sorgt für den Bezug zur Uhrzeit (12:03).
                fresh_since = align_to_trading_window(run_begun or now, trading)

                if not signal:
                    # Kein Ausschlag mehr. Die Sperre muss trotzdem stehen bleiben, sonst
                    # wäre jedes kurze Wegkippen des Signals wieder ein "neuer" Ausschlag.
                    current[key] = keep_notified("none", prev["notifiedMilestones"])
                elif continues:
                    # Nur neu erreichte Dauer-Schwellen melden — das Gedächtnis bleibt
                    # erhalten, also kommt nichts doppelt.
                    current[key] = keep_notified(
                        signal,
                        fire_due_milestones(prev["notifiedMilestones"], resumed_since),
                        run_anchor,
                        resumed_since,
                    )
                else:
                    # Neuer bzw. erstmals gesehener Ausschlag (auch Richtungswechsel Kauf<->Verkauf,
                    # der die Sperre bewusst durchbricht): sofort melden. Läuft die Serie laut
                    # Historie schon länger, sagt die EINE Meldung das gleich mit ("Anhaltendes
                    # Kaufsignal (seit 2 Tagen)"), statt mehrere Mails zu schicken.
                    current[key] = {
                        "signal": signal,
                        "notifiedMilestones": fire_due_milestones(
                            [], fresh_since, first_alert=True
                        ),
                        "notifiedSignal": signal,
                        "notifiedAt": now,
                        "runAnchor": run_anchor,
                        "runSince": fresh_since,
                    }

    write_json_file(ALERT_STATE_FILE, current)
    if fresh:
        store_notifications(fresh)
        for item in fresh:
            send_windows_toast(*notification_text(item))
        # Je Treffer eine eigene Mail (eindeutiger Betreff, damit Gmail sie nicht zu
        # einem Verlauf zusammenklappt) — alle über eine SMTP-Verbindung.
        send_mails([alert_mail(item) for item in fresh])
    return fresh


def alert_loop():
    while True:
        try:
            run_alert_check()
        except Exception as exc:
            print(f"Überwachung fehlgeschlagen: {exc}")
        time.sleep(ALERT_INTERVAL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/data":
            symbol = query.get("symbol", [DEFAULT_SYMBOL])[0].strip() or DEFAULT_SYMBOL
            try:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    market_future = pool.submit(
                        cached, ("market", symbol), CACHE_TTL_SECONDS, lambda: get_market_data(symbol)
                    )
                    fear_greed_future = pool.submit(
                        cached, "fear_greed", CACHE_TTL_SECONDS, get_fear_greed
                    )
                    smart_dumb_future = pool.submit(
                        cached, "smart_dumb", COT_CACHE_TTL_SECONDS, get_smart_dumb_money
                    )
                    payload = {
                        "market": market_future.result(),
                        "fearGreed": fear_greed_future.result(),
                        "smartDumbMoney": smart_dumb_future.result(),
                    }
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        if parsed.path == "/api/history":
            symbol = query.get("symbol", [DEFAULT_SYMBOL])[0].strip() or DEFAULT_SYMBOL
            range_key = query.get("range", [DEFAULT_CHART_RANGE])[0].strip()
            if range_key not in CHART_RANGES:
                range_key = DEFAULT_CHART_RANGE
            try:
                ma_window = int(query.get("maWindow", [MA_DEVIATION_WINDOW_DEFAULT])[0])
            except ValueError:
                ma_window = MA_DEVIATION_WINDOW_DEFAULT
            ma_window = max(MA_WINDOW_MIN, min(MA_WINDOW_MAX, ma_window))
            try:
                result = cached(
                    ("history", symbol, range_key, ma_window), CACHE_TTL_SECONDS,
                    lambda: get_history(symbol, range_key, ma_window),
                )
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        if parsed.path == "/api/config":
            # Leeres Objekt, solange nichts gespeichert wurde -> der erste Browser mit
            # Daten sät die Config (siehe pullConfig in index.html).
            self._send_json(read_json_file(CONFIG_FILE, {}))
            return

        if parsed.path == "/api/alerts/notifications":
            status = mail_status()
            self._send_json({
                "notifications": read_json_file(NOTIFICATIONS_FILE, []),
                "watchlist": read_json_file(WATCHLIST_FILE, {}),
                "mailConfigured": status["configured"],
                "mailStatus": status,
                "intervalSeconds": ALERT_INTERVAL_SECONDS,
                "cooldownSeconds": NOTIFY_COOLDOWN_SECONDS,
            })
            return

        if parsed.path == "/api/search":
            search_term = query.get("q", [""])[0].strip()
            if len(search_term) < 2:
                self._send_json({"results": []})
                return
            try:
                results = cached(
                    ("search", search_term.lower()), CACHE_TTL_SECONDS, lambda: search_symbols(search_term)
                )
                self._send_json({"results": results})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        path = parsed.path
        if path == "/":
            path = "/index.html"
        file_path = (BASE_DIR / path.lstrip("/")).resolve()

        # data/ enthält u.a. die Mail-Zugangsdaten und wird nie ausgeliefert.
        if BASE_DIR not in file_path.parents and file_path != BASE_DIR:
            self.send_response(403)
            self.end_headers()
            return
        if file_path == DATA_DIR or DATA_DIR in file_path.parents:
            self.send_response(403)
            self.end_headers()
            return
        if not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        content_type = CONTENT_TYPES.get(file_path.suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Immer die aktuelle Datei ausliefern: sonst zeigt der Browser (besonders am
        # Handy) nach einem Update noch die zwischengespeicherte alte Seite.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def do_POST(self):
        parsed = urlparse(self.path)

        # Geräteübergreifende Config (Favoriten, Varianten, überwachte Filter,
        # Indikator-Reihenfolge, archivierte Indikatoren, aktive Einstellungen). Der
        # Browser schreibt hier bei jeder Änderung; daraus wird zugleich die
        # watchlist.json für die Überwachung abgeleitet.
        if parsed.path == "/api/config":
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                self._send_json({"error": "ungültige Daten"}, status=400)
                return
            config = {
                "favorites": payload.get("favorites") or [],
                "presets": payload.get("presets") or {},
                "watched": payload.get("watched") or [],
                "indicatorOrder": payload.get("indicatorOrder") or [],
                "archived": payload.get("archived") or [],
                "activeSettings": payload.get("activeSettings") or {},
                "updated": int(time.time()),
            }
            write_json_file(CONFIG_FILE, config)
            write_json_file(WATCHLIST_FILE, derive_watchlist(config))
            self._send_json({"ok": True})
            return

        # (Alt-Endpunkt, von der Seite nicht mehr genutzt: direkte Watchlist.)
        if parsed.path == "/api/alerts/watchlist":
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                self._send_json({"error": "ungültige Daten"}, status=400)
                return
            write_json_file(WATCHLIST_FILE, {
                "favorites": payload.get("favorites") or [],
                "filters": payload.get("filters") or [],
                "updated": int(time.time()),
            })
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/alerts/notifications/read":
            with _file_lock:
                entries = read_json_file(NOTIFICATIONS_FILE, [])
                write_json_file(NOTIFICATIONS_FILE, [{**e, "read": True} for e in entries])
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/alerts/notifications/clear":
            with _file_lock:
                write_json_file(NOTIFICATIONS_FILE, [])
            self._send_json({"ok": True})
            return

        # Testmail aus der Glocke heraus: meldet den echten Fehler zurück, statt still
        # zu scheitern — damit auf dem Pi ohne SSH sichtbar wird, woran es hakt.
        if parsed.path == "/api/alerts/mail-test":
            ok, error = send_test_mail()
            self._send_json({"ok": ok, "error": error})
            return

        # Sofort prüfen (z.B. direkt nach dem Markieren eines Filters). Bewusst mit force:
        # wer hier ausdrücklich prüft, will auch außerhalb der Handelszeit eine Antwort.
        if parsed.path == "/api/alerts/check":
            try:
                fresh = run_alert_check(force=True)
                self._send_json({"new": len(fresh)})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        self.send_response(404)
        self.end_headers()


def mail_setup_line():
    """Eine Zeile fuer das Log: Empfaenger und Server, oder warum keine Mail kommt."""
    config, error = load_mail_config()
    if error:
        return f"Mailversand: {error}"
    return f"Mailversand an {config['to']} ueber {config['host']}:{config.get('port', 465)}"


def main():
    if "--mail-test" in sys.argv:
        print(mail_setup_line())
        ok, error = send_test_mail()
        print("Testmail verschickt." if ok else f"Testmail fehlgeschlagen: {error}")
        return 0 if ok else 1

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=alert_loop, daemon=True).start()
    watched = len((read_json_file(WATCHLIST_FILE, {}) or {}).get("filters") or [])
    shown_host = "127.0.0.1" if HOST in ("127.0.0.1", "0.0.0.0") else HOST
    hint = " (im Netzwerk erreichbar)" if HOST == "0.0.0.0" else ""
    print(f"Server laeuft auf http://{shown_host}:{PORT}{hint}")
    quiet_end = f"{QUIET_END_SECONDS // 3600:02d}:{QUIET_END_SECONDS % 3600 // 60:02d}"
    quiet_start = f"{QUIET_START_SECONDS // 3600:02d}:{QUIET_START_SECONDS % 3600 // 60:02d}"
    print(
        f"Ueberwachung aktiv: {watched} Filter, Pruefung alle {ALERT_INTERVAL_SECONDS // 60} Minuten,"
        f" Meldesperre {NOTIFY_COOLDOWN_SECONDS // 3600} Stunden je Signal"
    )
    print(f"Gemeldet wird nur an Handelstagen zwischen {quiet_end} und {quiet_start} Uhr")
    print(mail_setup_line())
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
