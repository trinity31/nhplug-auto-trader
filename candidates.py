"""공개 EasyInvesting 자료만 사용한 모의투자 후보 선정. 키·계좌 접근 없음."""
import datetime as dt
import json
import math
import re
import urllib.request
from zoneinfo import ZoneInfo

ROOT = "https://easyinvesting.app"
SEOUL = ZoneInfo("Asia/Seoul")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def reference_day(now):
    """보수적인 최신성 경계. 평일 16시 전에는 전 평일, 이후에는 당일.

    휴일을 임의로 평일 거래일이라고 인정하지 않습니다. 공급처가 휴장일을 건너뛴
    경우에도 확인할 수 없으면 매수를 보류합니다. 매도 판단에는 이 값을 쓰지 않습니다.
    """
    now = now.astimezone(SEOUL)
    day = now.date()
    if now.time() < dt.time(16):
        day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day.isoformat()


def get_json(path):
    req = urllib.request.Request(ROOT + path, headers={"User-Agent": "NH-mock-strategy/1.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read(8_000_000))


def select(rs_rows, signal_data, sectors, now, per_stock=500_000, mock=True):
    if not mock:
        raise ValueError("이 후보 선정은 모의투자 전용입니다")
    if not isinstance(rs_rows, list) or not isinstance(signal_data, dict) or not isinstance(sectors, list):
        raise ValueError("후보 자료 형식을 확인하지 못했습니다")
    meta = signal_data.get("meta") or {}
    market = (meta.get("market_s") or {}).get("^KS11")
    if market not in (True, False) or not isinstance(market, bool):
        raise ValueError("한국 시장 추세 상태를 확인하지 못했습니다")
    expected = reference_day(now)
    sector_strength = {s["name"]: float(s["avg_rs"]) for s in sectors
                       if isinstance(s, dict) and s.get("name") and number(s.get("avg_rs"))
                       and number(s.get("count")) and s["count"] > 0}
    if not sector_strength:
        raise ValueError("섹터 상대강도를 확인하지 못했습니다")
    rows = signal_data.get("rows")
    if not isinstance(rows, list) or signal_data.get("total") != len(rows):
        raise ValueError("매수 신호 목록이 불완전합니다. 페이지·건수를 확인해야 합니다")
    by_code = {}
    for signal in rows:
        if signal.get("country") != "KR" or signal.get("asset") != "stock":
            continue
        code = str(signal.get("ticker", ""))
        ev = signal.get("ev")
        if number(ev) and (code not in by_code or ev > by_code[code]["ev"]):
            by_code[code] = signal
    out, seen = [], set()
    for row in rs_rows:
        code = str(row.get("ticker", ""))
        name = str(row.get("name", ""))
        price, rs, rs1 = row.get("close"), row.get("pct"), row.get("pct_1m")
        if not re.fullmatch(r"[0-9]{6}", code) or code in seen:
            continue
        if row.get("market") not in ("KOSPI", "KOSDAQ"):
            continue
        if any(word in name.upper() for word in ("스팩", "SPAC", "ETF", "ETN", "인버스", "레버리지")):
            continue
        if not all(number(v) for v in (price, rs, rs1)) or not (0 < price <= per_stock / 2 and rs >= 90 and rs1 > rs):
            continue
        sector = str(row.get("naver_sector") or "")
        strength = sector_strength.get(sector)
        if strength is None:
            continue
        if market and code not in by_code:
            continue
        date = str(row.get("asof_date") or "")
        fresh = date == expected
        if market:
            fresh = fresh and (meta.get("star", {}).get("rs_asof", {}).get("KR") == expected)
            fresh = fresh and not meta.get("warnings")
        item = {"code": code, "name": name, "rs": rs, "rs_1m": rs1,
                "sector": sector, "sector_rs": strength, "reference_price": price,
                "asof": date, "expected_asof": expected, "fresh": fresh,
                "market_on": market, "ev": by_code[code]["ev"] if market else None}
        item["reason"] = (f"{sector} 업종 평균 RS {strength:g}, 전체 RS {rs:g}, 1개월 RS {rs1:g}. "
                          + (f"시장 ON: EV {item['ev']:g} 순서" if market else "모의 시장 OFF 예외: EV 미사용"))
        out.append(item)
        seen.add(code)
    if market:
        out.sort(key=lambda r: (-r["ev"], r["code"]))
    else:
        out.sort(key=lambda r: (-r["sector_rs"], -r["rs"], -r["rs_1m"], r["code"]))
    return {"checked_at": now.isoformat(), "market_on": market, "expected_asof": expected,
            "sources": [ROOT + "/api/rs/top?limit=3000", ROOT + "/api/buy-signals?limit=1000",
                        ROOT + "/api/rs/naver-sectors"],
            "sector_definition": "네이버 업종 평균 RS가 높은 업종 우선. 소수 종목 업종은 평균 왜곡에 주의. ON에서는 EV 순서를 우선 유지",
            "candidates": out, "eligible_count": sum(r["fresh"] for r in out)}


def fetch(now, per_stock=500_000):
    from concurrent.futures import ThreadPoolExecutor
    paths = ["/api/rs/top?limit=3000", "/api/buy-signals?limit=1000", "/api/rs/naver-sectors"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows, signals, sectors = list(pool.map(get_json, paths))
    return select(rows, signals, sectors, now, per_stock)
