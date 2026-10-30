import copy
import datetime as dt
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import check
import mock_runner as runner

# 실제 운영에 설치한 전략을 검사합니다. staging 사본으로 통과를 꾸미지 않습니다.
PROPOSAL = Path(__file__).parents[1] / "strategy.py"
ns = {}
exec(check.validate_source(PROPOSAL.read_text(encoding="utf-8")), ns)
approved = types.SimpleNamespace(**{k: v for k, v in ns.items() if not k.startswith("__")})


class StrategyTests(unittest.TestCase):
    def test_budget_even_minimum_and_risk(self):
        self.assertEqual(approved.buy_quantity(100000, 10000000, 999), 4)
        self.assertEqual(approved.buy_quantity(250000, 10000000, 999), 0)
        self.assertEqual(approved.buy_quantity(1000, 10000000, 3), 2)
        self.assertEqual(approved.buy_quantity(0, 10000000, 999), 0)
        self.assertEqual(approved.buy_quantity(float("nan"), 10000000, 999), 0)
        self.assertEqual(approved.MAX_HOLDINGS, 0)

    def test_sell_rules_no_ai_required(self):
        base = dict(market="kr", held=True, qty=10, pnl_pct=-10, half_remaining=5)
        self.assertEqual(approved.plan(base)["qty"], 10)
        self.assertEqual(approved.plan(base)["kind"], "stop")
        self.assertEqual(approved.plan(dict(base, pnl_pct=40))["qty"], 5)
        self.assertEqual(approved.plan(dict(base, pnl_pct=45, half_remaining=3))["qty"], 3)
        self.assertEqual(approved.plan(dict(base, pnl_pct=45, half_done=True))["action"], "hold")
        self.assertEqual(approved.plan(dict(base, pnl_pct=0, close_exit_due=True))["action"], "hold")
        self.assertEqual(approved.plan(dict(base, pnl_pct=0, half_done=True, close_exit_due=True))["kind"], "ma50")
        self.assertEqual(approved.plan(dict(base, pnl_pct=0, six_months_due=True))["kind"], "expiry")

    def test_no_unvalidated_or_ai_absent_buys(self):
        base = dict(market="kr", held=False, price=10000, rs=90, rs_1m=95, fresh=True,
                    candidate_approved=True, budget=500000, buyable=100, ai={"decision": "buy"})
        self.assertEqual(approved.plan(base)["action"], "buy")
        for change in (dict(ai=None), dict(fresh=False), dict(rs_1m=90), dict(market="us"), dict(candidate_approved=False)):
            self.assertEqual(approved.plan(dict(base, **change))["action"], "hold")
        self.assertEqual(approved.plan(dict(base, held=True, qty=10, pnl_pct=-5))["action"], "hold")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.object(runner, "strategy", approved)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.state = runner.fresh_state("MOCK_TEST")
        self.now = dt.datetime(2026, 10, 26, 10, tzinfo=runner.SEOUL)

    def intent(self, side="buy", qty=10, kind="entry"):
        return dict(code="005930", name="테스트", side=side, qty=qty, kind=kind,
                    price=10000, baseline=[], day="2026-10-23", status="pending")

    def row(self, filled, amount, qty=10, pending=0, cancelled=0):
        return dict(itg_orr_no=123, tot_cns_qty=filled, cns_amt=amount,
                    ny_cns_qty=pending, can_qty=cancelled, orr_qty=qty)

    def position(self):
        intent = self.intent()
        runner.apply_fill(self.state, intent, self.row(10, 100000))
        return self.state["positions"]["005930"]

    def test_acceptance_is_not_fill_and_fills_idempotent(self):
        intent = self.intent()
        runner.apply_fill(self.state, intent, self.row(0, 0, pending=10))
        self.assertEqual(self.state["positions"], {})
        runner.apply_fill(self.state, intent, self.row(4, 40000, pending=6))
        runner.apply_fill(self.state, intent, self.row(4, 40000, pending=6))
        self.assertEqual(self.state["positions"]["005930"]["qty"], 4)
        self.assertEqual(self.state["cash_budget"], 9959600)

    def test_half_completion_requires_actual_half_fill(self):
        pos = self.position()
        intent = self.intent("sell", 5, "half")
        runner.apply_fill(self.state, intent, self.row(2, 28000, qty=5, pending=3))
        self.assertIsNone(pos["half_done_date"])
        runner.apply_fill(self.state, intent, self.row(5, 70000, qty=5))
        self.assertEqual(pos["half_done_date"], "2026-10-23")
        self.assertEqual(pos["qty"], 5)
        runner.apply_fill(self.state, intent, self.row(5, 70000, qty=5))
        self.assertEqual(pos["qty"], 5)

    def test_sma50_after_partial_and_completed_daily_only(self):
        pos = self.position()
        dates = [(self.now.date() - dt.timedelta(days=70-i)).strftime("%Y%m%d") for i in range(70)]
        bars = [(date, 10000) for date in dates]
        bars[-1] = (dates[-1], 9000)
        quote = {"price": 14000}
        self.assertFalse(runner.position_context("005930", pos, quote, bars, self.now)["close_exit_due"])
        pos["half_done_date"] = "2026-10-23"
        self.assertTrue(runner.position_context("005930", pos, quote, bars, self.now)["close_exit_due"])
        pos["close_exit_day"] = None
        bars[-1] = (dates[-1], 10000)
        bars.append(("20261026", 1))  # 장중 봉의 급락으로 매도하면 안 됨
        self.assertFalse(runner.position_context("005930", pos, quote, bars, self.now)["close_exit_due"])

    def test_six_calendar_months(self):
        self.assertEqual(runner.add_months(dt.date(2026, 8, 31), 6), dt.date(2027, 2, 28))
        pos = self.position()
        pos["entry_date"] = "2026-04-26"
        self.assertTrue(runner.position_context("005930", pos, {"price": 11000}, [], self.now)["six_months_due"])

    def test_reservations_and_budget_no_holdings_limit(self):
        intent = self.intent(qty=50)
        self.state["orders"].append(intent)
        self.assertEqual(runner.budget_available(self.state, 20_000_000), 9495000)
        self.state["cash_budget"] = -1
        self.assertEqual(runner.budget_available(self.state, 20_000_000), 0)

    def test_ambiguous_match_is_not_guessed(self):
        intent = self.intent()
        row = dict(itg_orr_no=1, iem_cd="005930", sby_dit_cd_nm="현금매수", orr_qty=10, orr_pr=10000)
        with self.assertRaises(runner.SafetyError):
            runner.match_order(intent, [row, dict(row, itg_orr_no=2)])

    def test_persistence_corruption_wrong_account_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            runner.save(self.state, path)
            self.assertEqual(runner.load("MOCK_TEST", path), self.state)
            with self.assertRaises(runner.SafetyError):
                runner.load("DIFFERENT", path)
            path.write_text("{broken")
            with self.assertRaises(runner.SafetyError):
                runner.load("MOCK_TEST", path)

    def test_submit_persists_before_send_and_never_retries_unknown(self):
        class API:
            def __init__(self): self.calls = 0
            def orders(self, day): return []
            def submit(self, *args): self.calls += 1; raise TimeoutError
        api = API()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            plan = dict(action="buy", kind="entry", reason="규칙", qty=2)
            intent = runner.submit(self.state, api, "005930", "테스트", plan, 10000, self.now, path)
            self.assertEqual(intent["status"], "unknown")
            runner.reconcile(self.state, api)
            self.assertEqual(api.calls, 1)
            self.assertEqual(runner.load("MOCK_TEST", path)["orders"][0]["status"], "unknown")

    def test_preview_no_submit_no_ledger_write_and_stale_blocks_buy(self):
        class API:
            def guard(self): pass
            def connect(self): return "MOCK_TEST"
            def balance(self): return 500_000_000, {}
            def submit(self, *args): raise AssertionError("preview ordered")
        snap = dict(candidates=[dict(code="005930", name="테스트", fresh=False)], eligible_count=0, market_on=False)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            out = runner.run(preview=True, api=API(), now=self.now, state_path=path, fetch_candidates=lambda *a: snap)
            self.assertFalse(path.exists())
            self.assertEqual(out["처리함"], [])
            self.assertTrue(out["경고"])
