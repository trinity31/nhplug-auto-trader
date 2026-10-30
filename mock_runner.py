"""승인된 모의전략 실행기. 접수·체결 분리, 영속 장부, 배타 잠금, 재시도 금지."""
import calendar
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import time
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import candidates
import strategy
from mock_broker import NHMock, SafetyError

SEOUL = ZoneInfo("Asia/Seoul")
STATE = Path(__file__).parent / ".cache" / "mock-ledger.json"


def add_months(day, months):
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    month += 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def is_open(now):
    now = now.astimezone(SEOUL)
    return now.weekday() < 5 and dt.time(9) <= now.time() < dt.time(15, 20)


def fresh_state(account):
    return {"version": 1, "environment": "mock", "account_hash": hashlib.sha256(account.encode()).hexdigest(),
            "capital": strategy.CAPITAL, "cash_budget": float(strategy.CAPITAL), "positions": {}, "orders": [], "exited": {}}


def save(state, path=STATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def load(account, path=STATE):
    if not path.exists():
        return fresh_state(account)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        expected = fresh_state(account)
        if any(state.get(k) != expected[k] for k in ("version", "environment", "account_hash", "capital")):
            raise ValueError
        if not isinstance(state["positions"], dict) or not isinstance(state["orders"], list):
            raise ValueError
        if not candidates.number(state["cash_budget"]):
            raise ValueError
        return state
    except (ValueError, TypeError, KeyError):
        raise SafetyError("모의 장부 형식·계좌가 일치하지 않습니다. 초기화하거나 추정하지 않고 중단합니다") from None


def order_id(row):
    return str(row.get("itg_orr_no") or "")


def match_order(intent, rows):
    if intent.get("itg_id"):
        matches = [r for r in rows if order_id(r) == intent["itg_id"]]
    else:
        side = "매수" if intent["side"] == "buy" else "매도"
        matches = [r for r in rows if order_id(r) and order_id(r) not in intent["baseline"]
                   and str(r.get("iem_cd")) == intent["code"]
                   and side in str(r.get("sby_dit_cd_nm") or "")
                   and int(r.get("orr_qty") or 0) == intent["qty"]
                   and float(r.get("orr_pr") or 0) == intent["price"]]
    if len(matches) > 1:
        raise SafetyError("동일 조건의 주문이 여러 건이라 체결을 특정할 수 없습니다")
    return matches[0] if matches else None


def apply_fill(state, intent, row):
    """누적 체결량의 차이만 반영. 접수번호·잔고 변화로 체결을 꾸며내지 않음."""
    filled = int(row.get("tot_cns_qty") or 0)
    amount = float(row.get("cns_amt") or 0)
    previous = intent.get("applied_qty", 0)
    old_amount = intent.get("applied_amount", 0.0)
    if filled < previous or filled > intent["qty"] or amount < old_amount or (filled > 0 and amount <= 0):
        raise SafetyError("누적 체결 수량·금액이 일관되지 않습니다")
    delta, cash_delta = filled - previous, amount - old_amount
    if delta == 0 and cash_delta != 0:
        raise SafetyError("체결량 변경 없이 체결금액이 달라졌습니다")
    code = intent["code"]
    if delta:
        if intent["side"] == "buy":
            pos = state["positions"].setdefault(code, {"name": intent["name"], "qty": 0, "cost": 0.0,
                "original_qty": 0, "entry_date": intent["day"], "half_sold": 0, "half_done_date": None,
                "close_exit_day": None})
            pos["qty"] += delta
            pos["original_qty"] += delta
            pos["cost"] += cash_delta
            state["cash_budget"] -= cash_delta * (1 + strategy.COST_BUFFER)
        else:
            pos = state["positions"].get(code)
            if not pos or delta > pos["qty"]:
                raise SafetyError("매도 체결량이 관리 장부 보유량보다 큽니다")
            avg = pos["cost"] / pos["qty"]
            pos["qty"] -= delta
            pos["cost"] -= avg * delta
            state["cash_budget"] += cash_delta * (1 - strategy.COST_BUFFER)
            if intent["kind"] == "half":
                pos["half_sold"] += delta
                if pos["half_sold"] == pos["original_qty"] // 2 and pos["original_qty"] % 2 == 0:
                    pos["half_done_date"] = intent["day"]
            if not pos["qty"]:
                state["positions"].pop(code)
                state["exited"][code] = intent["day"]
    intent["applied_qty"], intent["applied_amount"] = filled, amount
    pending, cancelled = int(row.get("ny_cns_qty") or 0), int(row.get("can_qty") or 0)
    rejected = bool(str(row.get("orr_rjt_rsn_cd_nm") or "").strip())
    if filled + cancelled > intent["qty"] or pending < 0:
        raise SafetyError("체결·취소·미체결 수량이 일관되지 않습니다")
    terminal = filled == intent["qty"] or filled + cancelled == intent["qty"] or (rejected and pending == 0)
    intent["status"] = "closed" if terminal else "pending"
    intent["itg_id"] = order_id(row)


def reconcile(state, api):
    cache = {}
    for intent in state["orders"]:
        if intent["status"] == "closed":
            continue
        day = intent["day"]
        if day not in cache:
            cache[day] = api.orders(day)
        row = match_order(intent, cache[day])
        if row is not None:
            apply_fill(state, intent, row)
        else:
            intent["status"] = "unknown"
    return cache


def pending_orders(state):
    return [o for o in state["orders"] if o["status"] != "closed"]


def budget_available(state, account_cash):
    reserve = sum((o["qty"] - o.get("applied_qty", 0)) * o["price"] * (1 + strategy.COST_BUFFER)
                  for o in pending_orders(state) if o["side"] == "buy")
    invested = sum(p["cost"] for p in state["positions"].values())
    return max(0.0, min(state["cash_budget"] - reserve, strategy.CAPITAL - invested - reserve, account_cash))


def position_context(code, pos, quote, bars, now):
    avg = pos["cost"] / pos["qty"]
    half_done = bool(pos.get("half_done_date"))
    # 장중 봉은 제외합니다. 이후 회차에서 전일 확정 종가로만 판정합니다.
    completed = [(day, close) for day, close in bars if day < now.strftime("%Y%m%d")]
    if half_done:
        # 회차를 놓친 사이 이탈 후 회복했더라도 최초 확정 이탈 신호를 잃지 않습니다.
        for index in range(49, len(completed)):
            day, close = completed[index]
            formatted = dt.datetime.strptime(day, "%Y%m%d").date().isoformat()
            average = sum(c for _, c in completed[index - 49:index + 1]) / 50
            if formatted >= pos["half_done_date"] and close < average:
                pos["close_exit_day"] = pos.get("close_exit_day") or formatted
                break
    return {"code": code, "market": "kr", "held": True, "qty": pos["qty"], "avg": avg,
            "price": quote["price"], "pnl_pct": (quote["price"] / avg - 1) * 100,
            "half_done": half_done, "half_remaining": max(0, pos["original_qty"] // 2 - pos["half_sold"])
                if pos["original_qty"] % 2 == 0 else 0,
            "close_exit_due": bool(pos.get("close_exit_day") and now.date().isoformat() > pos["close_exit_day"]),
            "six_months_due": now.date() >= add_months(dt.date.fromisoformat(pos["entry_date"]), strategy.HOLD_MONTHS)}


def submit(state, api, code, name, plan, price, now, path):
    rows = api.orders(now.date().isoformat())
    intent = {"code": code, "name": name, "day": now.date().isoformat(), "side": plan["action"],
              "kind": plan["kind"], "reason": plan["reason"], "qty": plan["qty"], "price": int(price),
              "baseline": [order_id(r) for r in rows], "status": "prepared", "applied_qty": 0, "applied_amount": 0.0}
    state["orders"].append(intent)
    save(state, path)  # 장부 저장이 실패하면 주문을 절대 내지 않음
    try:
        intent["ack"] = api.submit(code, plan["action"], plan["qty"], int(price))
        intent["status"] = "pending"
    except Exception:
        intent["status"] = "unknown"  # 시간 초과여도 재전송 금지
    save(state, path)
    return intent


def run(*, preview=False, api=None, now=None, state_path=STATE, fetch_candidates=candidates.fetch):
    now = (now or dt.datetime.now(SEOUL)).astimezone(SEOUL)
    started = time.monotonic()
    api = api or NHMock()
    api.guard()
    account = api.connect()
    state = copy.deepcopy(load(account, state_path))
    if not preview:
        reconcile(state, api)
        save(state, state_path)
    account_cash, held = api.balance()
    warnings, actions, plans = [], [], []
    if getattr(api, "account_choice_count", 1) > 1:
        warnings.append("모의계좌가 여러 개라 설정 화면과 동일하게 번호순 첫 계좌 사용. 실제 계좌는 제외")
    unknown = set(held) - set(state["positions"])
    mismatch = [code for code, p in state["positions"].items() if held.get(code, {}).get("qty", 0) != p["qty"]]
    if unknown:
        warnings.append("전략 장부에 없는 기존 보유가 있어 신규매수 중단: " + ", ".join(sorted(unknown)))
    if mismatch:
        warnings.append("NH 잔고와 체결 장부의 수량 불일치: " + ", ".join(mismatch))
    blocked_codes = {o["code"] for o in pending_orders(state)} | set(mismatch)
    open_now = is_open(now)
    for code, pos in list(state["positions"].items()):
        try:
            quote = api.quote(code)
            try:
                bars = api.bars(code) if pos.get("half_done_date") else []
            except Exception:
                bars = []
                warnings.append(f"{code} 확정 일봉을 받지 못해 50일선 매도만 보류. 손절·기한은 유지")
            context = position_context(code, pos, quote, bars, now)
            plan = strategy.plan(context)
            if plan["action"] == "sell":
                item = {"종목": pos["name"], "코드": code, "수량": plan["qty"], "이유": plan["reason"], "행동": "sell"}
                plans.append(item)
                if open_now and not preview and code not in blocked_codes:
                    if api.sellable(code) < plan["qty"]:
                        warnings.append(f"{code} 매도가능 수량 부족. 수량을 추정하거나 재주문하지 않음")
                    else:
                        intent = submit(state, api, code, pos["name"], plan, quote["price"], now, state_path)
                        actions.append({**item, "상태": intent["status"], "구분": "매도접수확인중"})
        except (OSError, SafetyError):
            raise
        except Exception:
            warnings.append(f"{code} 보유 자료 조회·검증 실패. 자동 주문 보류")
    try:
        snapshot = fetch_candidates(now, strategy.BUY_AMOUNT)
    except Exception:
        snapshot = {"candidates": [], "eligible_count": 0, "market_on": None}
        warnings.append("EasyInvesting 자료를 확인하지 못해 새 매수만 보류")
    if snapshot.get("candidates") and not snapshot.get("eligible_count"):
        warnings.append("후보 RS 자료가 최신 완료 거래일 기준과 달라 새 매수 보류")
    can_buy = not unknown and not mismatch and not any(o["status"] == "unknown" for o in pending_orders(state))
    for row in snapshot["candidates"]:
        if time.monotonic() - started > 95:
            warnings.append("이번 회차 조회 시간 한도 도달. 나머지 신규매수 후보는 다음 회차에 확인")
            break
        code = row["code"]
        if not row["fresh"] or code in state["positions"] or code in held or code in blocked_codes:
            continue
        if state["exited"].get(code) == now.date().isoformat() or not can_buy:
            continue
        try:
            quote = api.quote(code)
            available = budget_available(state, account_cash)
            context = {**row, "market": "kr", "held": False, "price": quote["price"], "candidate_approved": True,
                       "budget": available, "buyable": api.buyable(code, quote["price"]),
                       "ai": {"decision": "buy", "reason": row["reason"]}}
            plan = strategy.plan(context)
            if plan["action"] != "buy":
                continue
            item = {"종목": row["name"], "코드": code, "수량": plan["qty"], "기준가": quote["price"],
                    "이유": plan["reason"], "행동": "buy"}
            plans.append(item)
            if open_now and not preview:
                intent = submit(state, api, code, row["name"], plan, quote["price"], now, state_path)
                actions.append({**item, "상태": intent["status"], "구분": "매수접수확인중"})
                blocked_codes.add(code)
                if intent["status"] == "unknown":
                    can_buy = False
            elif preview:
                # 미리보기에서도 앞 후보의 가상 예산을 예약해 1,000만원보다 큰 계획을 만들지 않음.
                state["cash_budget"] -= plan["qty"] * quote["price"] * (1 + strategy.COST_BUFFER)
        except (OSError, SafetyError):
            raise
        except Exception:
            warnings.append(f"{code} 매수 자료 검증 실패. 해당 종목 매수 보류")
    if not preview:
        reconcile(state, api)
        save(state, state_path)
        account_cash, held = api.balance()
    rows = [{"종목": p.get("name", code), "코드": code, "수량": p["qty"],
             "평균매입가": f"{p.get('avg', 0):,.0f}원"} for code, p in held.items()]
    remaining = budget_available(state, account_cash)
    pending = pending_orders(state)
    if pending:
        warnings.append(f"미체결·미확인 주문 {len(pending)}건: 해당 종목 재주문 금지. 미확인 결과는 수동 확인 필요")
    status = "장중" if open_now else "장외"
    summary = (f"모의투자 {status}. " + ("미리보기이므로 주문하지 않았습니다." if preview else
               f"이번 회차 주문 시도 {len(actions)}건. 접수와 체결은 별도로 확인합니다."))
    return {"요약": summary, "계좌": "모의투자", "장": "열림" if open_now else "닫힘", "미리보기": preview,
            "시각": now.isoformat(), "처리함": actions, "예정": plans, "경고": warnings,
            "지금": {"보유": rows, "종목수": f"{len(held)} / 별도 제한 없음", "전략가용예산": f"{remaining:,.0f}원",
                    "주문가능현금": "계좌 전체 현금 대신 전략 예산만 사용"},
            "후보": snapshot, "미확인주문": [{k: o[k] for k in ("code", "side", "qty", "status", "day")} for o in pending]}


def main(mode):
    preview = mode in ("--preview", "--account")
    if mode == "--do":
        raise SafetyError("이 전략은 재량 판단 --do를 허용하지 않습니다. 승인된 규칙 --scan만 사용합니다")
    lock = None
    try:
        if not preview:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            lock = open(STATE.with_suffix(".lock"), "a", encoding="utf-8")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(preview=preview)
        if not preview:
            save(result, STATE.with_name("mock-latest.json"))
        # 미리보기는 장부·주문·대시보드 상태를 변경하지 않습니다.
        if not preview or mode == "--account":
            import trade
            trade.remember({**result, "오류": " / ".join(result["경고"])})
        output = result
        if "--brief" in sys.argv:
            output = {k: result[k] for k in ("요약", "계좌", "장", "처리함", "경고", "미확인주문")}
            output["새매수가능후보수"] = result["후보"].get("eligible_count", 0)
            output["전략가용예산"] = result["지금"]["전략가용예산"]
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except BlockingIOError:
        print(json.dumps({"요약": "다른 회차가 실행 중이라 중복 실행하지 않았습니다"}, ensure_ascii=False))
    except Exception as exc:
        # 외부 예외 원문에는 URL·계좌·인증정보가 섞일 수 있어 그대로 출력하지 않음.
        print(json.dumps({"요약": "모의전략 실행이 안전하게 중단되었습니다. 주문 결과를 추정하거나 재시도하지 않습니다.",
                          "오류": str(exc) if isinstance(exc, SafetyError) else "연결·업무응답·장부를 확인해야 합니다",
                          "오류유형": type(exc).__name__}, ensure_ascii=False))
        raise SystemExit(1) from None
    finally:
        if lock:
            lock.close()
