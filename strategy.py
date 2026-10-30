"""Trinity가 승인한 투더문 모의투자 원칙.

약속: 국내주식만, 총 원금 1,000만원, 거래 위험 상한 10만원(1%), 종목당 50만원.
별도 보유 종목 수 제한·누적 손실 중단 없음. 최초 매수 최소 2주·짝수, 추가 매수 금지.
EasyInvesting 전체 RS>=90, 1개월 RS>전체 RS. 강한 업종 선호.
시장 ON: 매수신호 후보의 EV 순서. 시장 OFF: 모의에서만 허용하고 EV 무시,
업종 평균 RS→종목 전체 RS→1개월 RS 순서. 자료 미확인·지연이면 새 매수 금지.
-10% 전량 손절. +40% 최초 수량 절반을 한 번 익절(실제 체결로만 완료 판정).
절반 익절 이후에만 확정 종가<SMA50이면 다음 거래일 첫 정규장 회차에 잔여 매도.
최초 체결일부터 달력 6개월이면 잔여 전량 매도. 물타기·상승 중 추가매수 모두 안 함.
시장 상태가 OFF라는 이유로 기존 보유를 매도하지 않음. 실계좌 자동주문은 금지.
실행기는 계좌·체결·예산·미체결·중복을 검증하며, 전략은 파일·네트워크에 접근하지 않음.
매시간 확인하는 지정가 주문이므로 손실상한·손절가·체결을 보장하지 않음.
"""
import math

APPROVED_MOCK = True
DYNAMIC_SYMBOLS = True
SYMBOLS = []
US_SYMBOLS = []
BUY_AMOUNT = 500_000
US_BUY_AMOUNT = 1
MAX_HOLDINGS = 0  # 승인된 동적 모의실행기에서만 0=별도 제한 없음
US_SESSIONS = ["regular"]
CAPITAL = 10_000_000
RISK_PER_TRADE = 100_000
STOP_LOSS_PCT = -10.0
TAKE_PROFIT_PCT = 40.0
HOLD_MONTHS = 6
COST_BUFFER = 0.01  # 실제 비용이 아니라 보수적 예산 유보(매수·매도 각 1%)
INSTRUCTIONS = """위 주석의 승인된 규칙만 적용한다. 재량·뉴스·LLM 판단으로 규칙을 바꾸지 않는다.
실계좌는 중단한다. 모의 시장OFF 예외에 EV를 섞지 않는다. 부분익절 완료는 체결로 확인한다.
필요한 사실이 없으면 새 매수하지 않고, 기존 보유의 손절 등 확인 가능한 매도는 유지한다."""


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def buy_quantity(price, budget, buyable):
    if not all(finite(v) and v >= 0 for v in (price, budget, buyable)) or price <= 0:
        return 0
    cap = min(BUY_AMOUNT, budget, RISK_PER_TRADE / (-STOP_LOSS_PCT / 100))
    qty = min(int(cap // (price * (1 + COST_BUFFER))), int(buyable))
    return qty - qty % 2 if qty >= 2 else 0


def plan(m):
    """실행기가 검증한 사실만 받음. qty=0은 이번에 주문하지 않음을 뜻함."""
    def answer(action, reason, qty=0, kind="hold"):
        return {"action": action, "reason": reason, "qty": qty, "kind": kind}
    if m.get("market", "kr") != "kr":
        return answer("hold", "국내 주식만 거래합니다")
    if m.get("held"):
        qty, pnl = int(m.get("qty") or 0), m.get("pnl_pct")
        if finite(pnl) and pnl <= STOP_LOSS_PCT + 1e-9:
            return answer("sell", "매수가 대비 -10% 손절", qty, "stop")
        if m.get("six_months_due"):
            return answer("sell", "최초 매수일부터 6개월 보유기한 도달", qty, "expiry")
        if m.get("half_done") and m.get("close_exit_due"):
            return answer("sell", "절반 익절 후 확정 종가가 50일선 아래: 다음 정규장 잔여 매도", qty, "ma50")
        remaining = int(m.get("half_remaining") or 0)
        if not m.get("half_done") and remaining > 0 and finite(pnl) and pnl >= TAKE_PROFIT_PCT - 1e-9:
            return answer("sell", "+40% 도달: 최초 수량의 절반 익절", min(qty, remaining), "half")
        return answer("hold", "기존 보유 유지. 추가 매수·물타기 금지")
    if not m.get("candidate_approved") or not m.get("fresh"):
        return answer("hold", "검증된 최신 RS·후보 조건이 없어 새 매수하지 않습니다")
    rs, rs1 = m.get("rs"), m.get("rs_1m")
    if not (finite(rs) and finite(rs1) and rs >= 90 and rs1 > rs):
        return answer("hold", "전체 RS 90 이상·1개월 RS 우위 조건 미충족")
    ai = m.get("ai") or {}
    if ai.get("decision") != "buy":
        return answer("hold", "검증된 규칙 판단이 없어 새 매수하지 않습니다")
    qty = buy_quantity(m.get("price"), m.get("budget", 0), m.get("buyable", 0))
    if qty < 2:
        return answer("hold", "50만원·총예산 안에서 짝수 2주를 살 수 없습니다")
    return answer("buy", str(ai.get("reason") or "검증된 RS·업종·시장 조건 충족"), qty, "entry")


def decide(m):
    p = plan(m)
    return p["action"], p["reason"]


def why_not_buy(m):
    if m.get("held"):
        return "보유 종목 추가매수 금지"
    if not m.get("candidate_approved") or not m.get("fresh"):
        return "검증된 최신 후보 자료가 없어 새 매수하지 않습니다"
    return None


def facts(m):
    out = []
    for field, label in (("rs", "전체 RS"), ("rs_1m", "1개월 RS"), ("sector_rs", "업종 평균 RS")):
        if finite(m.get(field)):
            out.append(f"{label}: {m[field]:g}")
    return out
