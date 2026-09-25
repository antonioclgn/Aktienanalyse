"""Tests für server.py — nur Standardbibliothek, ohne Netz.

Aufruf im Projektordner:  python -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def local_ts(year, month, day, hour=0, minute=0):
    """Zeitstempel einer lokalen Uhrzeit — damit laufen die Tests in jeder Zeitzone."""
    return int(time.mktime((year, month, day, hour, minute, 0, 0, 0, -1)))


# Mo 2026-09-21 .. Fr 2026-09-25 sind Handelstage, 26./27. Wochenende.
WEEK = {(2026, 9, d) for d in range(21, 26)}
RSI_ONLY = {**server.DEFAULT_FILTER_SETTINGS, "useFearGreed": False, "useSmartDumb": False,
            "usePma": False, "useMacd": False, "useSdChange": False, "useRsi": True,
            "rsiBuy": 30, "rsiSell": 70}


class IndicatorTests(unittest.TestCase):
    def test_rsi_rising_prices_is_100(self):
        rsi = server.compute_rsi_series(list(range(1, 30)))
        self.assertEqual(rsi[:14], [None] * 14)
        self.assertEqual(rsi[14], 100.0)
        self.assertEqual(rsi[-1], 100.0)

    def test_rsi_alternating_prices_is_50(self):
        closes = [10, 11] * 20
        self.assertAlmostEqual(server.compute_rsi_series(closes)[-1], 50, delta=5)

    def test_moving_average(self):
        self.assertEqual(server.compute_moving_average([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        self.assertEqual(server.compute_moving_average([1], 2), [None])

    def test_ema_starts_with_sma(self):
        ema = server.compute_ema_series([2, 4, 6, 8], 3)
        self.assertEqual(ema[:3], [None, None, 4])
        self.assertAlmostEqual(ema[3], 6.0)

    def test_macd_warmup(self):
        closes = [100 + i % 7 for i in range(60)]
        macd, signal = server.compute_macd_series(closes)
        self.assertIsNone(macd[24])
        self.assertIsNotNone(macd[25])
        self.assertIsNone(signal[25 + 7])
        self.assertIsNotNone(signal[25 + 8])

    def test_percentile_rank(self):
        self.assertEqual(server.percentile_rank([1, 2, 3, 4], 2), 50.0)
        self.assertEqual(server.percentile_rank([], 2), 0.0)

    def test_sd_change_uses_weekly_reports(self):
        times, spreads = [0, 10, 20, 30], [0, 5, 20, 50]
        self.assertEqual(server.sd_change_series(times, spreads, [5, 25, 35], 2), [None, 20, 45])

    def test_cumulative_trend_skips_none(self):
        self.assertEqual(server.cumulative_trend([None, 2, None, 4]), [None, 2, 2, 3])


class SignalTests(unittest.TestCase):
    def data(self, rsi):
        return {"timestamps": list(range(100, 100 + len(rsi))), "rsi": rsi}

    def evaluate(self, rsi, settings=RSI_ONLY):
        return server.evaluate_signal_and_run(self.data(rsi), settings, [], [], [], [])

    def test_combined_needs_all_enabled_indicators(self):
        settings = {**RSI_ONLY, "useFearGreed": True, "fearGreedBuy": 25}
        data = self.data([20, 20])
        # F&G 38 > 25: RSI allein reicht nicht (der MCD-Fall vom 25.09.2026).
        self.assertEqual(server.combined_signal_series(data, settings, [0], [38], [], []), [None, None])
        self.assertEqual(server.combined_signal_series(data, settings, [0], [20], [], []),
                         ["green", "green"])

    def test_empty_sell_quorum_gives_no_red(self):
        settings = {**RSI_ONLY, "rsiForSell": False}
        self.assertEqual(server.combined_signal_series(self.data([90]), settings, [], [], [], []), [None])

    def test_run_anchor_and_begin(self):
        signal, anchor, begun = self.evaluate([50, 50, 20, 20, 20])
        self.assertEqual((signal, anchor, begun), ("green", 101, 102))

    def test_only_open_candle_has_no_begin(self):
        self.assertEqual(self.evaluate([50, 50, 20]), ("green", 101, 0))

    def test_no_signal(self):
        self.assertEqual(self.evaluate([50, 50]), (None, 0, 0))


class CalendarTests(unittest.TestCase):
    def test_open_seconds_skips_nights_and_weekends(self):
        fri_noon = local_ts(2026, 9, 25, 12)
        mon_noon = local_ts(2026, 9, 28, 12)
        self.assertEqual(server.open_seconds(fri_noon, mon_noon, None), server.OPEN_SECONDS_PER_DAY)

    def test_open_seconds_skips_holidays_from_calendar(self):
        # Mi 23.09. fehlt im Raster -> Feiertag.
        days = WEEK - {(2026, 9, 23)}
        start, end = local_ts(2026, 9, 22, 12), local_ts(2026, 9, 24, 12)
        now = local_ts(2026, 9, 26, 12)
        with mock.patch("time.time", return_value=now):
            self.assertEqual(server.open_seconds(start, end, days), server.OPEN_SECONDS_PER_DAY)

    def test_align_moves_night_to_morning(self):
        tue_night = local_ts(2026, 9, 22, 2)
        self.assertEqual(server.align_to_trading_window(tue_night, None), local_ts(2026, 9, 22, 7, 30))
        sat = local_ts(2026, 9, 26, 12)
        self.assertEqual(server.align_to_trading_window(sat, None), local_ts(2026, 9, 28, 7, 30))

    def test_milestone_needs_more_than_one_day(self):
        start = local_ts(2026, 9, 22, 12)
        self.assertFalse(server.milestone_reached(start, local_ts(2026, 9, 22, 23, 30), 1, "1y"))
        self.assertTrue(server.milestone_reached(start, local_ts(2026, 9, 23, 12, 5), 1, "1y"))

    def test_today_before_xetra_open_counts_as_trading_day(self):
        days = WEEK - {(2026, 9, 25)}          # heutige DAX-Kerze fehlt noch
        morning = local_ts(2026, 9, 25, 7, 45)
        local = time.localtime(morning)
        self.assertTrue(server.is_trading_day(local, days, now=morning))
        afternoon = local_ts(2026, 9, 25, 14)
        self.assertFalse(server.is_trading_day(local, days, now=afternoon))

    def test_weekend_never_trading_day(self):
        sat = time.localtime(local_ts(2026, 9, 26, 12))
        self.assertFalse(server.is_trading_day(sat, WEEK | {(2026, 9, 26)}))
        self.assertFalse(server.is_trading_day(sat, None))

    def test_milestone_label(self):
        self.assertEqual(server.milestone_label(1, "10y"), "1 Tag")
        self.assertEqual(server.milestone_label(7, "1y"), "7 Tagen")
        self.assertEqual(server.milestone_label(3, "5d"), "3 Stunden")


class DecideAlertTests(unittest.TestCase):
    NOW = local_ts(2026, 9, 24, 12)

    def decide(self, prev, signal="green", anchor=500, begun=0, fallback=False, now=None,
               delay=0, range_key="1y"):
        return server.decide_alert(prev, signal, anchor, begun, fallback, now or self.NOW,
                                   None, delay, range_key)

    def test_new_signal_notifies_once(self):
        state, notes = self.decide(None)
        self.assertEqual(notes, [None])
        self.assertEqual(state["notifiedSignal"], "green")
        # Nächster Lauf fünf Minuten später: dieselbe Serie, keine neue Meldung.
        state2, notes2 = self.decide(state, now=self.NOW + 300)
        self.assertEqual(notes2, [])
        self.assertEqual(state2["runSince"], state["runSince"])

    def test_milestone_after_one_trading_day(self):
        state, _ = self.decide(None)
        _, notes = self.decide(state, now=self.NOW + 86400 + 60)
        self.assertEqual(notes, [1])

    def test_known_long_run_reports_highest_milestone_only(self):
        begun = local_ts(2026, 9, 21, 11)       # seit Montag 11:00, jetzt Donnerstag 12:00
        _, notes = self.decide(None, begun=begun)
        self.assertEqual(notes, [3])

    def test_signal_flicker_within_cooldown_is_not_new(self):
        state, _ = self.decide(None)
        gone, _ = self.decide(state, signal=None, now=self.NOW + 300)
        back, notes = self.decide(gone, anchor=900, now=self.NOW + 600)
        self.assertEqual(notes, [])
        self.assertEqual(back["signal"], "green")

    def test_direction_change_breaks_cooldown(self):
        state, _ = self.decide(None)
        _, notes = self.decide(state, signal="red", anchor=700, now=self.NOW + 300)
        self.assertEqual(notes, [None])

    def test_min_delay_keeps_quiet(self):
        state, notes = self.decide(None, delay=2)
        self.assertEqual(notes, [])
        _, notes = self.decide(state, delay=2, now=local_ts(2026, 9, 28, 12, 1))   # Montag
        self.assertEqual(notes, [2])

    def test_fallback_quote_needs_confirmation(self):
        state, notes = self.decide(None, fallback=True)
        self.assertEqual(notes, [])
        self.assertEqual(state["pendingSignal"], "green")
        _, notes = self.decide(state, fallback=True, now=self.NOW + 300)
        self.assertEqual(notes, [None])

    def test_pending_from_yesterday_does_not_confirm(self):
        evening = local_ts(2026, 9, 23, 22, 55)
        state, _ = self.decide(None, fallback=True, now=evening)
        morning = local_ts(2026, 9, 24, 7, 35)
        state, notes = self.decide(state, fallback=True, now=morning)
        self.assertEqual(notes, [])
        self.assertEqual(state["pendingSince"], morning)

    def test_legacy_state_string(self):
        state, notes = self.decide("green")
        self.assertEqual(notes, [])
        self.assertEqual(state["signal"], "green")


class ConfigTests(unittest.TestCase):
    def test_sanitize_drops_bad_types_and_ranges(self):
        cfg = server.sanitize_config({
            "favorites": [{"symbol": "MCD"}, {"symbol": ""}, "x"],
            "presets": {"A": {"useRsi": True}, "B": 3},
            "watched": [
                {"name": "A", "ranges": ["1y", "bogus", 5], "minDelayDays": "2", "symbols": ["MCD", 1]},
                {"name": 7},
            ],
            "archived": "nope",
        })
        self.assertEqual(cfg["favorites"], [{"symbol": "MCD"}])
        self.assertEqual(list(cfg["presets"]), ["A"])
        self.assertEqual(cfg["watched"], [{"name": "A", "ranges": ["1y"], "minDelayDays": 2,
                                           "symbols": ["MCD"]}])
        self.assertEqual(cfg["archived"], [])

    def test_sanitize_keeps_only_known_designs(self):
        self.assertEqual(server.sanitize_config({"design": "kursblatt"})["design"], "kursblatt")
        self.assertEqual(server.sanitize_config({"design": "<script>"})["design"], "klassisch")
        self.assertEqual(server.sanitize_config({})["design"], "klassisch")

    def test_sanitize_rejects_non_list_favorites(self):
        self.assertEqual(server.sanitize_config({"favorites": {"a": 1}})["favorites"], [])

    def test_derive_watchlist_keeps_only_ticked_ranges(self):
        watchlist = server.derive_watchlist({
            "favorites": [{"symbol": "MCD"}],
            "presets": {"RSI": RSI_ONLY},
            "watched": [{"name": "RSI", "ranges": ["10y"]}, {"name": "gelöscht", "ranges": ["1y"]},
                        {"name": "Standard", "ranges": ["1y"]}],
        })
        self.assertEqual([(f["name"], f["ranges"]) for f in watchlist["filters"]],
                         [("RSI", ["10y"]), ("Standard", ["1y"])])


class DataDirTestCase(unittest.TestCase):
    """Legt alle Dateien der Überwachung in einen Temp-Ordner."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.patches = [mock.patch.object(server, name, data / getattr(server, name).name) for name in (
            "CONFIG_FILE", "WATCHLIST_FILE", "NOTIFICATIONS_FILE", "ALERT_STATE_FILE",
            "MAIL_CONFIG_FILE", "MAIL_LOG_FILE", "VENUES_FILE")]
        for p in self.patches:
            p.start()
        server._cache.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        server._cache.clear()
        self.tmp.cleanup()


class FileTests(DataDirTestCase):
    def test_parallel_writes_leave_valid_json(self):
        path = server.ALERT_STATE_FILE
        errors = []

        def writer(n):
            try:
                for i in range(30):
                    server.write_json_file(path, {"writer": n, "i": i, "pad": "x" * 2000})
            except Exception as exc:     # pragma: no cover - nur bei Fehlern
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn("writer", json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_corrupt_file_is_moved_aside(self):
        path = server.ALERT_STATE_FILE
        path.write_text("{kaputt", encoding="utf-8")
        with self.assertRaises(server.CorruptFileError):
            server.read_json_file(path, {}, strict=True)
        self.assertFalse(path.exists())
        self.assertEqual(len(list(path.parent.glob("alert_state.json.corrupt-*"))), 1)


def history_with_signal(symbol, range_key, ma_window):
    time.sleep(0.05)     # Abruf dauert — damit sich parallele Läufe überlappen würden
    return {"timestamps": [1, 2, 3], "rsi": [50, 20, 20], "quote": None}


class AlertCheckTests(DataDirTestCase):
    def setUp(self):
        super().setUp()
        server.write_json_file(server.WATCHLIST_FILE, server.derive_watchlist({
            "favorites": [{"symbol": "MCD", "name": "McDonald's"}, {"symbol": "OFF", "notify": False}],
            "presets": {"RSI": RSI_ONLY},
            "watched": [{"name": "RSI", "ranges": ["10y"]}],
        }))
        self.mails = []
        for p in (
            mock.patch.object(server, "get_history", history_with_signal),
            mock.patch.object(server, "german_trading_days", lambda: None),
            mock.patch.object(server, "load_sentiment", lambda: {"fg": None, "sd": None}),
            mock.patch.object(server, "dispatch_mails", self.mails.append),
        ):
            p.start()
            self.patches.append(p)

    def test_parallel_checks_notify_once(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(server.run_alert_check(force=True)))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        notes = server.read_json_file(server.NOTIFICATIONS_FILE, [])
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0]["symbol"], notes[0]["range"]), ("MCD", "10y"))
        self.assertEqual(sum(len(m) for m in self.mails), 1)

    def test_busy_check_is_queued(self):
        with server._alert_lock:
            self.assertIsNone(server.run_alert_check(force=True, wait=False))
        self.assertTrue(server._alert_recheck.is_set())
        server._alert_recheck.clear()

    def test_filter_needing_missing_sentiment_is_skipped(self):
        watchlist = server.read_json_file(server.WATCHLIST_FILE, {})
        watchlist["filters"][0]["settings"]["useFearGreed"] = True
        server.write_json_file(server.WATCHLIST_FILE, watchlist)
        self.assertEqual(server.run_alert_check(force=True), [])

    def test_corrupt_state_rebuilds_silently(self):
        server.ALERT_STATE_FILE.write_text("{kaputt", encoding="utf-8")
        self.assertEqual(server.run_alert_check(force=True), [])
        self.assertEqual(self.mails, [])
        state = server.read_json_file(server.ALERT_STATE_FILE, {})
        self.assertEqual(state["RSI|10y|MCD"]["notifiedSignal"], "green")
        # Danach normal weiter: dieselbe Serie bleibt still.
        self.assertEqual(server.run_alert_check(force=True), [])

    def test_state_of_unwatched_keys_is_pruned(self):
        server.write_json_file(server.ALERT_STATE_FILE, {"alt|1y|MCD": {"signal": "green"}})
        server.run_alert_check(force=True)
        self.assertEqual(set(server.read_json_file(server.ALERT_STATE_FILE, {})), {"RSI|10y|MCD"})


class HttpTests(DataDirTestCase):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def test_only_page_files_are_served(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/icon.png")[0], 200)
        for path in ("/server.py", "/.git/HEAD", "/data/mail_config.json", "/../server.py",
                     "/tests/test_server.py"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_cross_origin_post_is_rejected(self):
        status, _ = self.request("POST", "/api/alerts/notifications/clear",
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/alerts/notifications/clear",
                                 headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_config_requires_json_content_type(self):
        body = json.dumps({"favorites": [{"symbol": "MCD"}]})
        status, _ = self.request("POST", "/api/config", body, {"Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        status, data = self.request("POST", "/api/config", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(server.read_json_file(server.CONFIG_FILE, {})["favorites"], [{"symbol": "MCD"}])

    def test_legacy_watchlist_endpoint_is_gone(self):
        status, _ = self.request("POST", "/api/alerts/watchlist", "{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 404)


class CacheTests(unittest.TestCase):
    def tearDown(self):
        server._cache.clear()

    def test_cache_is_bounded(self):
        server._cache.clear()
        for i in range(server.CACHE_MAX_ENTRIES + 50):
            server.cached(("k", i), 60, lambda i=i: i)
        self.assertLessEqual(len(server._cache), server.CACHE_MAX_ENTRIES)
        self.assertEqual(server.cached(("k", server.CACHE_MAX_ENTRIES + 49), 60, lambda: "neu"),
                         server.CACHE_MAX_ENTRIES + 49)


if __name__ == "__main__":
    unittest.main()
