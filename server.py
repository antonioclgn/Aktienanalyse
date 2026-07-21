"""Lokaler Server: liefert index.html aus und stellt unter /api/data
Fear & Greed Index, RSI14 und den 200-Tage-Durchschnitt des S&P500 bereit.
"""
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
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}"
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=8&newsCount=0"
DEFAULT_SYMBOL = "SXR8.DE"  # iShares Core S&P 500 UCITS ETF USD (Acc)

# Zeitraum (fürs Frontend) -> Yahoo-Interval
CHART_RANGES = {
    "1d": "5m",
    "5d": "30m",
    "1mo": "1d",
    "1y": "1d",
    "5y": "1wk",
}
DEFAULT_CHART_RANGE = "1y"

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


def get_history(symbol, range_key):
    if range_key not in CHART_RANGES:
        range_key = DEFAULT_CHART_RANGE
    interval = CHART_RANGES[range_key]

    url = YAHOO_CHART_URL.format(symbol=quote(symbol, safe=""), range=range_key, interval=interval)
    data = fetch_json(url)
    result = data["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0]["close"]

    points = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
    return {
        "timestamps": [p[0] for p in points],
        "closes": [p[1] for p in points],
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


def get_fear_greed():
    data = fetch_json(
        CNN_FEAR_GREED_URL,
        {"Referer": "https://www.cnn.com/markets/fear-and-greed"},
    )
    fg = data["fear_and_greed"]
    return {"score": fg["score"], "rating": fg["rating"], "timestamp": fg["timestamp"]}


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
