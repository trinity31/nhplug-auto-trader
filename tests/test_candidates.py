import datetime as dt
import unittest
from candidates import SEOUL, select, reference_day


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 26, 10, tzinfo=SEOUL)
        self.row = dict(ticker="005930", name="테스트", market="KOSPI", close=10000,
                        pct=92, pct_1m=95, asof_date="2026-10-23", naver_sector="반도체")
        self.sectors = [dict(name="반도체", avg_rs=70, count=20), dict(name="제약", avg_rs=80, count=10)]
        self.sig = dict(meta={"market_s": {"^KS11": False}}, total=0, rows=[])

    def run_select(self, rows=None):
        return select(rows or [self.row], self.sig, self.sectors, self.now)

    def test_off_allows_rs_and_ignores_ev(self):
        out = self.run_select()["candidates"]
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["ev"])
        self.assertTrue(out[0]["fresh"])

    def test_off_sector_then_rs_sort(self):
        other = dict(self.row, ticker="000660", naver_sector="제약", pct=90)
        self.assertEqual(self.run_select([self.row, other])["candidates"][0]["code"], "000660")

    def test_on_ev_sort_not_sector_sort(self):
        self.sig = {"meta": {"market_s": {"^KS11": True}, "star": {"rs_asof": {"KR": "2026-10-23"}}},
                    "total": 2, "rows": [{"country": "KR", "asset": "stock", "ticker": "005930", "ev": 20},
                                           {"country": "KR", "asset": "stock", "ticker": "000660", "ev": 10}]}
        other = dict(self.row, ticker="000660", naver_sector="제약")
        self.assertEqual(self.run_select([other, self.row])["candidates"][0]["code"], "005930")

    def test_stale_is_visible_but_not_eligible(self):
        self.now = self.now.replace(hour=18)
        out = self.run_select()
        self.assertEqual(out["eligible_count"], 0)
        self.assertFalse(out["candidates"][0]["fresh"])

    def test_invalid_unknown_and_nonstock_fail_closed(self):
        for change in [dict(pct=89), dict(pct_1m=92), dict(close=250001), dict(close=0),
                       dict(market="KONEX"), dict(name="테스트스팩"), dict(pct=float("nan"))]:
            with self.subTest(change=change):
                self.assertEqual(self.run_select([dict(self.row, **change)])["candidates"], [])
        with self.assertRaises(ValueError):
            select([self.row], self.sig, self.sectors, self.now, mock=False)
        self.sig["meta"]["market_s"] = {}
        with self.assertRaises(ValueError):
            self.run_select()

    def test_incomplete_signal_pages_are_not_accepted(self):
        self.sig["total"] = 1001
        with self.assertRaises(ValueError):
            self.run_select()

    def test_weekend_reference(self):
        self.assertEqual(reference_day(dt.datetime(2026, 10, 25, 20, tzinfo=SEOUL)), "2026-10-23")
