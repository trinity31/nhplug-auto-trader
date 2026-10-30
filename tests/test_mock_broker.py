import unittest
from unittest.mock import patch
from types import SimpleNamespace
import broker
from mock_broker import NHMock, SafetyError, message_ok


class MockAdapterTests(unittest.TestCase):
    def test_business_status_not_http200(self):
        for text in ("정상적으로 조회되었습니다", "주문이 접수되었습니다", "조회내역이 없습니다", "모의투자 마지막 자료입니다"):
            self.assertTrue(message_ok({"message": {"rsp_msg": text}}))
        for text in ("", "계좌번호를 입력하세요", "주문처리 불가", "조회 실패", "비정상 응답"):
            self.assertFalse(message_ok({"message": {"rsp_msg": text}}))

    def test_live_stops_before_network(self):
        with patch.object(broker, "MOCK", False), patch.object(broker, "call") as call:
            with self.assertRaises(SafetyError):
                NHMock().connect()
            call.assert_not_called()

    def test_mixed_accounts_selects_only_mock(self):
        api = NHMock()
        data = [{"Output_0": [{"acct_type": "01", "acct_no": "LIVE"}, {"acct_type": "03", "acct_no": "MOCK"}]}]
        with patch.object(api, "post", return_value=data):
            self.assertEqual(api.connect(), "MOCK")
        with patch.object(api, "post", return_value=[{"Output_0": [{"acct_type": "01", "acct_no": "LIVE"}]}]):
            with self.assertRaises(SafetyError):
                api.connect()

    def test_post_no_retries_on_order_error(self):
        api = NHMock()
        api.account_no = "TEST"
        with patch.object(broker, "call", side_effect=TimeoutError), patch("mock_broker.time.sleep"):
            with self.assertRaises(TimeoutError):
                api.submit("005930", "buy", 2, 10000)

    def test_bad_business_or_continuation_fails(self):
        api = NHMock()
        meta = SimpleNamespace(has_next=False, cts_flag="N")
        with patch.object(broker, "call", return_value=({"message": {"rsp_msg": "조회 실패"}}, meta)):
            with self.assertRaises(SafetyError):
                api.post("/n2/acctinfo", {})
        meta.cts_flag = "Y"
        with patch.object(broker, "call", return_value=({"message": {"rsp_msg": "정상 조회 완료"}}, meta)), patch("mock_broker.time.sleep"):
            with self.assertRaises(SafetyError):
                api.post("/n2/acctinfo", {})

    def test_odd_order_rejected_before_post(self):
        api = NHMock()
        api.account_no = "TEST"
        with patch.object(api, "post") as post:
            with self.assertRaises(SafetyError):
                api.submit("005930", "buy", 3, 10000)
            post.assert_not_called()
