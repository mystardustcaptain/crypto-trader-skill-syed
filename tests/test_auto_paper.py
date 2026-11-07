"""tools/auto_paper.py: unattended paper simulation. Never an exchange order."""

import json
import os
import unittest

from _load import ROOT, cc, Session, load

AP = load("tools", "auto_paper")
SECRET = "k3y-that-must-never-print-9f2c"
KEY_NAMES = ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BYBIT_API_KEY", "BYBIT_API_SECRET")
FRESH = "2026-10-21 14:30"   # ten minutes after ORBX's planted New York break
STALE = "2026-10-21 15:15"   # the same break is fifty-five minutes old


def run(s, *argv):
    return s.run("tools", "auto_paper", list(argv))


def summary(out):
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError("expected one JSON line, got %r" % out)
    return json.loads(lines[0])


def ledger_path(s):
    return s.path("experiments", "syed-v1", "ledger.jsonl")


def ledger(s):
    return cc.read_jsonl(ledger_path(s))


def opens(s):
    return [e for e in ledger(s) if e.get("type") == "open"]


def closes(s):
    return [e for e in ledger(s) if e.get("type") == "close"]


class Guards(unittest.TestCase):
    def test_source_has_no_order_path_and_no_key_names(self):
        with open(os.path.join(ROOT, "tools", "auto_paper.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BYBIT_API_KEY", "BYBIT_API_SECRET",
                    "place_live", "place_paper", "keys_for", "crypto-broker/broker.py", "exchanges.py"):
            self.assertNotIn(bad, src, bad)
        self.assertNotIn("import broker", src)

    def test_refuses_live_config_and_the_live_flag_without_reading_keys(self):
        saved = dict((k, os.environ.get(k)) for k in KEY_NAMES)
        seen = []
        real_get = os.environ.get

        def spy(key, default=None):
            seen.append(key)
            return real_get(key, default)

        for k in KEY_NAMES:
            os.environ[k] = SECRET
        os.environ.get = spy
        try:
            with Session(config={"paper": False, "display_tz": "Asia/Kuala_Lumpur"}) as s:
                code, out, err = run(s)
                self.assertEqual(code, 1, out + err)
                self.assertIn("paper-only", err)
                self.assertIn('"paper" is false', err)
                self.assertNotIn(SECRET, out + err)
                self.assertFalse(os.path.exists(ledger_path(s)))
                self.assertFalse(os.path.exists(s.path("ledger.jsonl")))
                self.assertFalse(os.path.exists(s.path("cache", "venue.json")))
            with Session() as s:
                code, out, err = run(s, "--live", "--symbols", "ORBX")
                self.assertEqual(code, 1, out + err)
                self.assertIn("--live", err)
                self.assertIn("typed yes", err)
                self.assertNotIn(SECRET, out + err)
                self.assertFalse(os.path.exists(ledger_path(s)))
                self.assertFalse(os.path.exists(s.path("positions.json")))
        finally:
            os.environ.get = real_get
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertFalse(any(k in KEY_NAMES for k in seen), seen)

    def test_live_defaults_stay_and_the_paper_profile_is_looser_on_the_day(self):
        self.assertEqual(cc.DEFAULTS["max_daily_loss_usd"], 30)
        self.assertEqual(cc.DEFAULTS["max_trades_per_day"], 4)
        self.assertEqual(cc.DEFAULTS["max_loss_per_trade_usd"], 10)
        self.assertIsNone(cc.DEFAULTS["risk_day_tz"])
        profile = cc.DEFAULTS["paper_profile"]
        self.assertEqual(profile["experiment"], "syed-v1 paper")
        self.assertEqual(profile["rules"], "syed-v1")
        self.assertEqual(profile["start_equity_usd"], 1000)
        self.assertEqual(profile["max_daily_loss_usd"], 100)
        self.assertGreaterEqual(profile["max_trades_per_day"], 1000000)
        self.assertEqual(profile["risk_day_tz"], "UTC")
        self.assertEqual(profile["max_open_positions"], 2)
        self.assertEqual(profile["max_cost_r"], 0.2)
        self.assertEqual(AP.risk_budget_usd(1000, profile), 10)
        self.assertEqual(AP.risk_budget_usd(500, profile), 5)
        self.assertEqual(AP.risk_budget_usd(2500, profile), 10)
        self.assertEqual(AP.risk_budget_usd(0, profile), 0)
        self.assertEqual(AP.risk_budget_usd(-20, profile), 0)

    def test_risk_day_is_independent_of_the_display_clock(self):
        close_at = cc.parse_when("2026-10-21 20:00")
        # 2026-10-22 01:00 UTC is 09:00 in Kuala Lumpur, so 20:00 UTC the previous calendar day
        # is still "today" on that clock and already "yesterday" in UTC.
        with Session(now="2026-10-22 01:00", config={"display_tz": "Asia/Kuala_Lumpur"}) as s:
            cc.append_jsonl(s.path("ledger.jsonl"), {"type": "close", "at": close_at, "pnl_usd": -12})
            self.assertAlmostEqual(cc.today_stats()["realized"], -12)
            self.assertIn("UTC+8", cc.fmt_local(cc.now_ms()))
        with Session(now="2026-10-22 01:00", config={"display_tz": "Asia/Kuala_Lumpur", "risk_day_tz": "UTC"}) as s:
            cc.append_jsonl(s.path("ledger.jsonl"), {"type": "close", "at": close_at, "pnl_usd": -12})
            self.assertAlmostEqual(cc.today_stats()["realized"], 0)
            self.assertEqual(cc.risk_day_zone(), "UTC")
            self.assertIn("UTC+8", cc.fmt_local(cc.now_ms()))
        # The other way around: 02:00 UTC is today in UTC and yesterday in Kuala Lumpur.
        early = cc.parse_when("2026-10-21 02:00")
        with Session(now="2026-10-21 18:00", config={"display_tz": "Asia/Kuala_Lumpur"}) as s:
            cc.append_jsonl(s.path("ledger.jsonl"), {"type": "close", "at": early, "pnl_usd": -12})
            self.assertAlmostEqual(cc.today_stats()["realized"], 0)
        with Session(now="2026-10-21 18:00", config={"display_tz": "Asia/Kuala_Lumpur", "risk_day_tz": "UTC"}) as s:
            cc.append_jsonl(s.path("ledger.jsonl"), {"type": "close", "at": early, "pnl_usd": -12})
            self.assertAlmostEqual(cc.today_stats()["realized"], -12)


class SignalsAndFills(unittest.TestCase):
    def test_stale_rule_matches_the_entry_timer_and_the_scheduler_bar(self):
        now = cc.parse_when(STALE)
        orb = {"setup": "orb", "side": "long", "stop_ref": 21.1, "status": "holding",
               "at": cc.parse_when("2026-10-21 14:20"), "coin": "ORBX"}
        self.assertEqual(AP.skip_reason(orb, now, 15), "stale")
        self.assertIsNone(AP.skip_reason(orb, cc.parse_when(FRESH), 15))
        extended = {"setup": "breakout", "side": "long", "stop_ref": 1, "status": "extended",
                    "at": now, "coin": "BRKX"}
        self.assertEqual(AP.skip_reason(extended, now, 15), "stale")
        self.assertEqual(AP.skip_reason({"setup": "rs", "side": "long", "at": now, "coin": "SOL"}, now, 15), "ranking")
        self.assertEqual(AP.skip_reason({"setup": "squeeze", "side": "both", "at": now, "coin": "SQZX"}, now, 15), "no_side")

    def test_a_bar_from_before_the_fill_or_after_now_cannot_close_the_trade(self):
        opened = 1_000_000
        now = opened + 180000
        pos = {"side": "long", "qty": 2, "entry": 100, "stop": 90, "target": 120, "liq": 50,
               "opened_at": opened, "exit_by": opened + 86400000, "margin": 50,
               "costs": {"taker": 0.0005, "maker": 0.0002, "slippage": 0.0001}}
        before = [opened - 60000, 100, 100, 80, 90, 0, 0]          # would have stopped out, but it is before the fill
        quiet = [opened, 100, 101, 99, 100, 0, 0]
        future = [now, 100, 100, 80, 90, 0, 0]                    # opens at `now`, so it has not closed
        closed, _paid = AP.walk_position(pos, [before, quiet, future], [], now)
        self.assertIsNone(closed)
        stop_bar = [opened + 60000, 100, 101, 89, 95, 0, 0]
        later_target = [opened + 120000, 100, 130, 99, 110, 0, 0]
        closed, _paid = AP.walk_position(pos, [before, stop_bar, later_target], [], now)
        self.assertEqual(closed[1], "stop")
        self.assertEqual(closed[2], stop_bar[0] + 60000)   # the stop bar's close, not the later target bar
        self.assertNotEqual(closed[0], later_target[2])
        self.assertGreaterEqual(closed[2], opened)
        self.assertLessEqual(closed[2], now)

    def test_fill_matches_the_broker_book_walk_without_placing(self):
        broker = load("crypto-broker", "broker")
        saved = (broker.place, broker.place_paper, broker.place_live)
        calls = []

        def boom(*_a, **_k):
            calls.append("called")
            raise AssertionError("order path called")

        broker.place = broker.place_paper = broker.place_live = boom
        try:
            with Session(now=FRESH) as s:
                venue = cc.get_venue("binance")
                market = venue.market("ORBX")
                ours, _note = AP.market_fill(venue, market, "long", 10)
                theirs, _note2 = broker.paper_market_fill(venue, market, "buy", 10)
                self.assertEqual(ours, theirs)
                self.assertFalse(os.path.exists(s.path("ledger.jsonl")))
            self.assertEqual(calls, [])
        finally:
            broker.place, broker.place_paper, broker.place_live = saved


class Runner(unittest.TestCase):
    def test_fresh_orb_is_logged_once_and_a_later_run_closes_it(self):
        with Session(now=FRESH, config={"session": "ny", "display_tz": "Asia/Kuala_Lumpur"}) as s:
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
                self.assertIn(out.strip(), fh.read())
            first = summary(out)
            self.assertGreaterEqual(first["new"], 1, out)
            self.assertLessEqual(first["open"], 2)
            self.assertEqual(first["rules"], "syed-v1")
            self.assertEqual(first["experiment"], "syed-v1 paper")
            self.assertEqual(first["venue"], "binance")
            self.assertTrue(first["paper"])
            rows = opens(s)
            orb = [e for e in rows if e.get("setup") == "orb" and e.get("side") == "long"]
            self.assertEqual(len(orb), 1, rows)
            trade = orb[0]
            for key in ("entry", "stop", "target", "r", "fees", "venue", "at_utc", "rules"):
                self.assertIn(key, trade)
            self.assertEqual(trade["rules"], "syed-v1")
            self.assertEqual(trade["venue"], "binance")
            self.assertGreater(trade["fees"], 0)
            self.assertLess(trade["stop"], trade["entry"])
            self.assertGreater(trade["target"], trade["entry"])
            self.assertIn("UTC", trade["at_utc"])
            self.assertTrue(trade["simulated"])
            self.assertFalse(os.path.exists(s.path("ledger.jsonl")))
            with open(ledger_path(s), encoding="utf-8") as fh:
                text = fh.read()
            self.assertTrue(text.startswith("{"))
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            second = summary(out)
            self.assertEqual(second["new"], 0)
            self.assertEqual(second["closed"], 0)
            self.assertEqual(second["open"], first["open"])
            with open(ledger_path(s), encoding="utf-8") as fh:
                again = fh.read()
            self.assertEqual(again, text)
            headers = [e for e in ledger(s) if e.get("type") == "experiment"]
            self.assertEqual(len(headers), 1)
            self.assertEqual(headers[0]["start_equity_usd"], 1000)
            self.assertIn("Fresh simulated", headers[0]["note"])
            # More than a day later the position has to be gone, and the old lines stay put.
            os.environ["CRYPTO_NOW"] = "2026-10-22 16:00"
            os.environ["FAKE_NOW"] = "2026-10-22 16:00"
            cc.set_now("2026-10-22 16:00")
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            later = summary(out)
            self.assertGreaterEqual(later["closed"], 1, out)
            with open(ledger_path(s), encoding="utf-8") as fh:
                after = fh.read()
            self.assertTrue(after.startswith(text))
            done = [e for e in closes(s) if e.get("id") == trade["id"]]
            self.assertEqual(len(done), 1)
            closed = done[0]
            self.assertIn(closed["exit_reason"], ("stop", "target", "time limit", "liquidation"))
            self.assertEqual(closed["reason"], closed["exit_reason"])
            self.assertEqual(closed["rules"], "syed-v1")
            self.assertEqual(closed["venue"], "binance")
            self.assertEqual(closed["setup"], "orb")
            self.assertEqual(closed["side"], "long")
            self.assertGreater(closed["fees"], 0)
            self.assertGreaterEqual(closed["at"], trade["at"])
            self.assertLessEqual(closed["at"], cc.parse_when("2026-10-22 16:00"))
            self.assertIn("UTC", closed["at_utc"])
            self.assertIn("UTC", closed["opened_at_utc"])
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            self.assertEqual(len([e for e in closes(s) if e.get("id") == trade["id"]]), 1)
            with open(ledger_path(s), encoding="utf-8") as fh:
                self.assertTrue(fh.read().startswith(after))

    def test_a_stale_opening_range_is_not_taken(self):
        with Session(now=STALE, config={"session": "ny"}) as s:
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            got = summary(out)
            self.assertGreaterEqual(got["skip_reasons"].get("stale", 0), 1, out)
            self.assertFalse(any(e.get("setup") == "orb" for e in opens(s)))

    def test_paper_day_limits_are_not_the_live_ones(self):
        def seed_close(s, when, pnl, ident):
            cc.append_jsonl(ledger_path(s), {"type": "close", "id": ident, "at": cc.parse_when(when),
                                            "pnl_usd": pnl, "rules": "syed-v1", "experiment": "syed-v1 paper"})

        with Session(now=FRESH, config={"session": "ny"}) as s:
            seed_close(s, "2026-10-21 12:00", -40, "loss-40")
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            got = summary(out)
            self.assertGreaterEqual(got["new"], 1, out)
            self.assertNotIn("daily loss", got["limit_hits"])
        with Session(now=FRESH, config={"session": "ny"}) as s:
            for i, minute in enumerate(("12:00", "12:10", "12:20", "12:30")):
                at = cc.parse_when("2026-10-21 " + minute)
                ident = "trip-%d" % i
                cc.append_jsonl(ledger_path(s), {"type": "open", "id": ident, "at": at, "signal_id": "OLD|%d" % i,
                                                "coin": "ADA", "symbol": "ADAUSDT", "rules": "syed-v1"})
                cc.append_jsonl(ledger_path(s), {"type": "close", "id": ident, "at": at + 60000, "pnl_usd": 1.0,
                                                "rules": "syed-v1"})
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            got = summary(out)
            self.assertGreaterEqual(got["new"], 1, out)
            self.assertNotIn("trades", got["limit_hits"])
        with Session(now=FRESH, config={"session": "ny"}) as s:
            seed_close(s, "2026-10-21 12:00", -95, "loss-95")
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            got = summary(out)
            self.assertEqual(got["new"], 0, out)
            self.assertIn("daily loss", got["limit_hits"])

    def test_risk_shrinks_when_equity_does(self):
        with Session(now=FRESH, config={"session": "ny"}) as s:
            cc.append_jsonl(ledger_path(s), {"type": "close", "id": "yesterday",
                                            "at": cc.parse_when("2026-10-20 12:00"), "pnl_usd": -500,
                                            "rules": "syed-v1"})
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            self.assertGreaterEqual(summary(out)["new"], 1, out)
            trade = [e for e in opens(s) if e.get("setup") == "orb"][0]
            self.assertLessEqual(trade["loss_at_stop_usd"], 5.05)
            self.assertGreater(trade["loss_at_stop_usd"], 3)

    def test_binance_first_and_the_fallback_is_what_the_trade_records(self):
        with Session(now=FRESH, config={"session": "ny", "venue": "hyperliquid"}) as s:
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            self.assertEqual(summary(out)["venue"], "binance")
            self.assertTrue(all(e.get("venue") == "binance" for e in opens(s)))
        with Session(now=FRESH, config={"session": "ny"}, mode="binance_geoblock") as s:
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            self.assertEqual(summary(out)["venue"], "bybit", out)
            self.assertTrue(opens(s))
            self.assertTrue(all(e.get("venue") == "bybit" for e in opens(s)))

    def test_display_zone_does_not_move_the_experiment_day(self):
        with Session(now="2026-10-22 01:00", config={"display_tz": "Asia/Kuala_Lumpur", "session": "ny"}) as s:
            cc.append_jsonl(ledger_path(s), {"type": "note", "text": "older history stays"})
            cc.append_jsonl(ledger_path(s), {"type": "close", "id": "kl", "at": cc.parse_when("2026-10-21 20:00"),
                                            "pnl_usd": -12, "rules": "syed-v1"})
            with open(ledger_path(s), encoding="utf-8") as fh:
                before = fh.read()
            code, out, err = run(s, "--symbols", "ORBX")
            self.assertEqual(code, 0, err)
            got = summary(out)
            self.assertEqual(got["day_pnl"], 0)
            self.assertLess(got["equity"], 1000)
            with open(ledger_path(s), encoding="utf-8") as fh:
                after = fh.read()
            self.assertTrue(after.startswith(before))
            self.assertIn("older history stays", after)


if __name__ == "__main__":
    unittest.main()
