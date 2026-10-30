"""공식 NH 명세에 따른 모의전용 조회·주문 어댑터.

정본: https://www.nhplug.com/openapi-docs/krstock/openapi.json
주문은 절대로 재시도하지 않습니다. HTTP 200·접수는 체결을 의미하지 않습니다.
"""
import datetime as dt
import time
import re

import broker


class SafetyError(RuntimeError):
    pass


def message_ok(data):
    message = data.get("message") or data
    if not isinstance(message, dict):
        return False
    text = str(message.get("rsp_msg") or "").replace(" ", "")
    negative = ("실패", "오류", "불가", "부족", "거절", "비정상", "입력하", "입력해", "않", "초과", "만료", "잘못", "없습니다만")
    return bool(text) and not any(w in text for w in negative) and any(
        w in text for w in ("정상", "완료", "조회되", "조회하였", "조회했", "접수", "마지막자료", "내역이없", "자료가없", "내역없", "자료없"))


class NHMock:
    def __init__(self):
        self.account_no = None
        self._last = 0.0

    def guard(self):
        if not broker.MOCK or broker.BASE_URL != broker.MOCK_URL:
            raise SafetyError("실제 계좌 설정을 감지해 중단했습니다. 이 실행기는 모의투자 전용입니다")

    def post(self, path, payload, *, common=False, single=False):
        self.guard()
        out, seen, cursor, flag = [], set(), None, None
        for _ in range(100):
            time.sleep(max(0, 1.1 - (time.monotonic() - self._last)))
            self._last = time.monotonic()
            broker.os.environ["NHPLUG_BASE_URL"] = broker.LIVE_URL if common else broker.MOCK_URL
            try:
                data, meta = broker.call(path, payload, cts=cursor, cts_flag=flag, want_meta=True, timeout=15)
            finally:
                broker.os.environ["NHPLUG_BASE_URL"] = broker.MOCK_URL
            if not isinstance(data, dict) or not message_ok(data):
                # 서버 원문은 키/계좌가 섞일 수 있어 화면·로그에 그대로 보내지 않습니다.
                source = data.get("message") or data if isinstance(data, dict) else {}
                text = str(source.get("rsp_msg") or "") if isinstance(source, dict) else ""
                safe = re.sub(r"[^가-힣 ㄱ-ㅎㅏ-ㅣ.,()]+", "", text)[:160]
                raise SafetyError(f"NH 업무 응답 확인 중단: {path} · {safe or '업무 메시지 없음'}")
            out.append(data)
            if not meta.has_next:
                if meta.cts_flag == "Y":
                    raise SafetyError("연속조회 키가 없어 완전한 응답을 확인하지 못했습니다")
                return out[0] if single else out
            if single or not meta.cts or meta.cts in seen:
                raise SafetyError("연속조회가 불완전하거나 반복되어 중단했습니다")
            seen.add(meta.cts)
            cursor, flag = meta.cts, meta.cts_flag
        raise SafetyError("연속조회 안전 한도를 초과했습니다")

    def connect(self):
        self.guard()
        pages = self.post("/n2/acctinfo", {}, common=True)
        accounts = sorted({str(r["acct_no"]).strip() for p in pages for r in p.get("Output_0", [])
                           if str(r.get("acct_type")) == "03"})
        if not accounts:
            raise SafetyError("모의 계좌를 확인할 수 없어 중단했습니다")
        # setup.check_connection → broker.account 와 동일한 정렬·선택 계약입니다.
        # 이후에는 장부의 계좌 지문이 다르면 실행기가 중단합니다.
        self.account_choice_count = len(accounts)
        self.account_no = accounts[0]
        return self.account_no

    def balance(self):
        pages = self.post("/krstock/inquiry/v1/balance", {"act_no": self.account_no, "bnc_bse_cd": "5",
            "ltg_aot_dit_cd": "9", "aet_bse": "2", "qut_dit_cd": "KRX", "aly_qut_cd": "1"})
        head = pages[0].get("Output_0")
        if not isinstance(head, dict) or "dca" not in head:
            raise SafetyError("현금을 확인하지 못했습니다")
        holdings = {}
        for page in pages:
            for row in page.get("Output_1") or []:
                code = str(row.get("iem_cd") or "").strip()
                qty = max(int(float(row.get(key) or 0)) for key in ("itg_bnc_qty", "rsdl_qty", "ny_stl_qty"))
                if qty:
                    holdings[code] = {"qty": qty, "avg": float(row.get("phs_pr") or 0),
                                      "name": str(row.get("iem_nm") or code).strip()}
        return float(head["dca"]), holdings

    def orders(self, day):
        pages = self.post("/krstock/inquiry/v1/dailyOrderExecution", {
            "act_no": self.account_no, "orr_dt": day.replace("-", ""), "orr_mkt_cd": "00", "ost_cns_dit": "0"})
        return [row for page in pages for row in (page.get("Output_0") or [])]

    def quote(self, code):
        data = self.post("/krstock/quote/v1/currentPrice", {"iem_cd": code, "market_cd": "KRX"},
                         common=True, single=True)
        out = data.get("Output_0") or {}
        price = int(float(out.get("stck_prpr") or 0))
        if price <= 0:
            raise SafetyError("현재가를 확인하지 못했습니다")
        return {"price": price, "name": str(out.get("iem_nm") or code)}

    def bars(self, code):
        # period 는 운영 시세 전용입니다. 계좌 없는 공개시세 조회이며 주문 경로 아님.
        pages = self.post("/krstock/quote/v1/period", {"iem_cd": code, "market_cd": "KRX", "gubun": "1",
            "array_cnt": "250", "maxavg": "50", "today_cls_code": "0", "view_main_yn": "Y"}, common=True)
        rows = [r for p in pages for r in (p.get("Output_1") or [])]
        return sorted({str(r["bsop_date"]): float(r["stck_prpr"]) for r in rows
                       if len(str(r.get("bsop_date", ""))) == 8 and float(r.get("stck_prpr") or 0) > 0}.items())

    def buyable(self, code, price):
        data = self.post("/krstock/inquiry/v1/buyableQuantity", {"ost_dit_cd": "1", "act_no": self.account_no,
            "iem_cd": code, "nmn_pr_tp_cd": "01", "orr_pr": price}, single=True)
        return int(float(data.get("Output_0", {}).get("csh_orr_pbl_qty") or 0))

    def sellable(self, code):
        data = self.post("/krstock/inquiry/v1/sellableQuantity", {"act_no": self.account_no,
            "iem_cd": code, "cfd_lon_cd": "00"}, single=True)
        return int(float(data.get("Output_0", {}).get("sll_pbl_qty") or 0))

    def submit(self, code, side, qty, price):
        self.guard()
        if not self.account_no or side not in ("buy", "sell") or qty <= 0 or price <= 0:
            raise SafetyError("잘못된 주문 입력")
        if side == "buy" and (qty < 2 or qty % 2):
            raise SafetyError("매수는 최소 2주·짝수만 허용합니다")
        # 단 한 번 호출. 네트워크/업무 오류라도 재전송하지 않고 장부에서 미확인 처리합니다.
        data = self.post("/krstock/order/v1/" + ("cashBuy" if side == "buy" else "cashSell"), {
            "act_no": self.account_no, "iem_cd": code, "orr_qty": int(qty), "orr_pr": int(price),
            "nmn_pr_tp_cd": "01", "orr_cnd_dit_cd": "02" if side == "buy" else "00", "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX", "sor_mkt_sli_yn": "N"}, single=True)
        order_no = data.get("Output_0", {}).get("mkt_orr_no")
        if not order_no:
            raise SafetyError("주문 접수번호가 없어 결과를 확정할 수 없습니다")
        return str(order_no)
