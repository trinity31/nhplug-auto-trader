"""공식 명세 261015 필수 조회 항목 회귀 검사.

정본: https://www.nhplug.com/openapi-docs/krstock/openapi.json
네트워크와 계좌 접근 없이 요청 모양만 검사합니다.
"""
import unittest
from unittest.mock import patch

import broker


class CurrentSpecTests(unittest.TestCase):
    def test_balance_requests_regular_session(self):
        for function in (broker.cash, broker.holdings):
            with self.subTest(function=function.__name__):
                with patch.object(broker, "_call", return_value={}) as call:
                    function("TEST_ACCOUNT")
                    self.assertEqual(call.call_args.args[1]["aly_qut_cd"], "1")

    def test_period_requests_regular_session(self):
        with patch.object(broker, "_call", return_value={}) as call:
            self.assertEqual(broker.closes("005930"), [])
            self.assertEqual(call.call_args.args[1]["view_main_yn"], "Y")


if __name__ == "__main__":
    unittest.main()
