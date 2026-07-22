"""Lokaler Server: liefert index.html aus und stellt unter /api/data
Fear & Greed Index, RSI14 und den 200-Tage-Durchschnitt des S&P500 bereit.
"""
import bisect
import calendar
import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
PORT = 8000
CACHE_TTL_SECONDS = 30

CNN_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
# CNNs eigene API liefert selbst mit explizitem, weiter zurückliegendem Startdatum
# keine Daten vor diesem Zeitpunkt (getestet: Anfragen mit früherem Datum -> HTTP 500).
CNN_FEAR_GREED_EARLIEST = "2020-07-14"
# Für die Zeit davor (bis 2011) liegt eine mitgelieferte, einmalig heruntergeladene
# Historie bei (siehe feargreed_history_2011_2020.csv), da CNNs Live-API nicht weiter zurückreicht.
FEARGREED_BUNDLED_CSV = BASE_DIR / "feargreed_history_2011_2020.csv"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}"
YAHOO_CHART_URL_PERIOD = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1={period1}&period2={period2}&interval={interval}"
)
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=8&newsCount=0"
DEFAULT_SYMBOL = "SXR8.DE"  # iShares Core S&P 500 UCITS ETF USD (Acc)

# Zeitraum (fürs Frontend) -> Yahoo-Interval
CHART_RANGES = {
    "1d": "5m",
    "5d": "30m",
    "1mo": "1h",
    "1y": "1d",
    "5y": "1wk",
    "10y": "1wk",
}
DEFAULT_CHART_RANGE = "1y"

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
    "5y": 300,  # ~40 Wochen (entspricht 200 Handelstagen)
    "10y": 300,  # ~40 Wochen (entspricht 200 Handelstagen)
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


# "200 Tage" bezieht sich auf Handelstage. Bei Wochenkerzen (5y/10y) muss das
# Fenster daher in Wochen umgerechnet werden (200 Handelstage ≈ 40 Wochen),
# sonst würde dort ein 200-WOCHEN-Durchschnitt (~4 Jahre) berechnet.
MA_WINDOW_BY_INTERVAL = {
    "1wk": 40,
}
MA_WINDOW_DEFAULT = 200

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


def get_daily_moving_average_by_date(symbol):
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
    ma_series = compute_moving_average(daily_closes, MA_WINDOW_DEFAULT)

    return {
        time.strftime("%Y-%m-%d", time.gmtime(t)): ma
        for t, ma in zip(daily_timestamps, ma_series)
        if ma is not None
    }


def get_history(symbol, range_key):
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

    if interval in INTRADAY_INTERVALS:
        daily_ma_by_date = cached(
            ("daily_ma", symbol), CACHE_TTL_SECONDS, lambda: get_daily_moving_average_by_date(symbol)
        )
        sorted_dates = sorted(daily_ma_by_date)
        moving_average = []
        for t in all_timestamps:
            date_key = time.strftime("%Y-%m-%d", time.gmtime(t))
            # Vorwärts auffüllen: der letzte (z.B. noch laufende) Handelstag hat oft
            # noch keinen fertigen Tagesschlusskurs -> mit dem letzten bekannten Wert auffüllen.
            idx = bisect.bisect_right(sorted_dates, date_key) - 1
            moving_average.append(daily_ma_by_date[sorted_dates[idx]] if idx >= 0 else None)
    else:
        ma_window = MA_WINDOW_BY_INTERVAL.get(interval, MA_WINDOW_DEFAULT)
        moving_average = compute_moving_average(all_closes, ma_window)

    rsi_series = compute_rsi_series(all_closes, 14)
    price_vs_ma_pct = [
        (100 - (ma / close * 100)) if ma is not None else None
        for close, ma in zip(all_closes, moving_average)
    ]

    start_index = 0
    if display_days and all_timestamps:
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
        "rsi": rsi_series[start_index:],
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
                with ThreadPoolExecutor(max_workers=2) as pool:
                    market_future = pool.submit(
                        cached, ("market", symbol), CACHE_TTL_SECONDS, lambda: get_market_data(symbol)
                    )
                    fear_greed_future = pool.submit(
                        cached, "fear_greed", CACHE_TTL_SECONDS, get_fear_greed
                    )
                    payload = {"market": market_future.result(), "fearGreed": fear_greed_future.result()}
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
                result = cached(
                    ("history", symbol, range_key), CACHE_TTL_SECONDS, lambda: get_history(symbol, range_key)
                )
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
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

        if BASE_DIR not in file_path.parents and file_path != BASE_DIR:
            self.send_response(403)
            self.end_headers()
            return
        if not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        content_type = "text/html; charset=utf-8" if file_path.suffix == ".html" else "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Server laeuft auf http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
