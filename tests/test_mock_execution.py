"""오프라인 모의 체결 fixture. 실제 NH 주문이나 네트워크는 호출하지 않습니다."""
import copy
import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import broker
import mock_runner as runner
import strategy
import trade
from mock_broker import NHMock, SafetyError


class FakeNH:
    def __init__(self):
        self.price = 10000
        self.holdings = {}
        self.rows = []
        self.sent = 0
    def guard(self): pass
    def connect(self): return "MOCK_TEST"
    def balance(self): return 500_000_000, copy.deepcopy(self.holdings)
    def orders(self, day): return copy.deepcopy(self.rows)
    def quote(self, code): return {"price": self.price, "name": "테스트"}
    def buyable(self, code, price): return 100000
    def sellable(self, code): return self.holdings[code]["qty"]
    def bars(self, code): return []
    def submit(self, code, side, qty, price):
        self.sent += 1
        self.rows.append(dict(itg_orr_no=self.sent, iem_cd=code, sby_dit_cd_nm="매수" if side == "buy" else "매도",
            orr_qty=qty, orr_pr=price, tot_cns_qty=qty, cns_amt=qty * price, ny_cns_qty=0, can_qty=0))
        if side == "buy": self.holdings[code] = {"qty": qty, "avg": price, "name": "테스트"}
        else:
            self.holdings[code]["qty"] -= qty
            if self.holdings[code]["qty"] == 0: self.holdings.pop(code)
        return str(self.sent)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 26, 10, tzinfo=runner.SEOUL)
        self.snap = {"market_on": False, "eligible_count": 1, "candidates": [dict(
            code="005930", name="테스트", fresh=True, rs=90, rs_1m=95, sector_rs=70, reason="검증된 RS 조건")]}
    def call(self, api, path, **kwargs):
        return runner.run(api=api, now=self.now, state_path=path, fetch_candidates=lambda *a: copy.deepcopy(self.snap), **kwargs)

    def test_buy_then_half_only_once_restart_and_no_averaging(self):
        api = FakeNH()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            self.call(api, path)
            state = runner.load("MOCK_TEST", path)
            original = state["positions"]["005930"]["original_qty"]
            self.assertEqual(original % 2, 0)
            self.assertLessEqual(original * 10000 * 1.01, 500000)
            api.price = 14000
            self.now = self.now.replace(hour=11)
            self.call(api, path)
            pos = runner.load("MOCK_TEST", path)["positions"]["005930"]
            self.assertEqual(pos["qty"], original // 2)
            self.assertIsNotNone(pos["half_done_date"])
            self.now = self.now.replace(hour=12)
            self.call(api, path)
            self.assertEqual(api.sent, 2)
            api.price = 9500
            self.call(api, path)
            self.assertEqual(api.sent, 2)  # 하락했다고 추가매수 안 함

    def test_preview_fresh_does_not_submit_or_create_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            api = FakeNH()
            out = self.call(api, path, preview=True)
            self.assertTrue(out["예정"])
            self.assertEqual(api.sent, 0)
            self.assertFalse(path.exists())

    def test_source_failure_does_not_disable_known_stop(self):
        api = FakeNH()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            self.call(api, path)
            api.price = 8000
            def failed(*a): raise ConnectionError
            runner.run(api=api, now=self.now, state_path=path, fetch_candidates=failed)
            self.assertEqual(api.sent, 2)
            self.assertEqual(api.holdings, {})

    def test_unknown_external_position_blocks_buys_not_adopted(self):
        api = FakeNH()
        api.holdings["000660"] = {"qty": 2, "avg": 10000, "name": "외부보유"}
        with tempfile.TemporaryDirectory() as folder:
            out = self.call(api, Path(folder) / "state.json")
            self.assertEqual(api.sent, 0)
            self.assertIn("기존 보유", " ".join(out["경고"]))

    def test_off_hours_never_submit_even_if_fresh(self):
        self.now = self.now.replace(hour=19)
        api = FakeNH()
        with tempfile.TemporaryDirectory() as folder:
            self.call(api, Path(folder) / "state.json")
            self.assertEqual(api.sent, 0)

    def test_public_cli_routes_to_mock_and_rejects_discretionary_do(self):
        with patch.object(trade.sys, "argv", ["trade.py", "--preview"]), patch.object(runner, "main") as main:
            trade.main()
            main.assert_called_once_with("--preview")
        with self.assertRaises(SafetyError):
            runner.main("--do")

    def test_live_cli_failure_never_networks(self):
        with patch.object(broker, "MOCK", False), patch.object(broker, "call") as call, redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                runner.main("--preview")
            call.assert_not_called()

    def test_buy_uses_fok_to_prevent_odd_partial_initial_fill(self):
        api = NHMock()
        api.account_no = "TEST"
        with patch.object(api, "post", return_value={"Output_0": {"mkt_orr_no": 1}}) as post:
            api.submit("005930", "buy", 2, 10000)
            self.assertEqual(post.call_args.args[1]["orr_cnd_dit_cd"], "02")
