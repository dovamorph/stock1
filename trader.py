#!/usr/bin/env python3
"""
StockPilot KR — 자동매매 (trader.py)
조건: A등급 + 시장 매수 우위/강한 매수
자금: 500만원 균등 분배
청산: 손절 -10% 전량 / 익절 분할 (+10%→40%, +18%→35%, +25%→나머지)
"""
import os, json, time, datetime, requests
from zoneinfo import ZoneInfo

# ── 설정 ──────────────────────────────────────────────────────────────
BUDGET          = 5_000_000
STOP_LOSS       = -0.10
# 분할 매도: (수익률 기준, 매도 비율)
PARTIAL_SELLS   = [
    (0.10, 0.40),   # +10% 도달 시 40% 매도
    (0.18, 0.35),   # +18% 도달 시 35% 매도
    (0.25, 1.00),   # +25% 도달 시 나머지 전량 매도
]
BUY_SIGNALS     = {"강한 매수", "매수 우위"}
MAX_POSITIONS   = 5
RESULTS_FILE    = "results.json"
POSITIONS_FILE  = "positions.json"

MOCK       = os.environ.get("KIS_MOCK", "true").lower() == "true"
APP_KEY    = os.environ["KIS_APP_KEY_MOCK"]    if MOCK else os.environ["KIS_APP_KEY"]
APP_SECRET = os.environ["KIS_APP_SECRET_MOCK"] if MOCK else os.environ["KIS_APP_SECRET"]
ACCOUNT_NO = os.environ.get("KIS_ACCOUNT_MOCK", "5018662201") if MOCK else os.environ.get("KIS_ACCOUNT_NO", "44457068")
DISCORD_WH = os.environ.get("DISCORD_WEBHOOK", "")
BASE_URL   = "https://openapivts.koreainvestment.com:29443" if MOCK else "https://openapi.koreainvestment.com:9443"
KST        = ZoneInfo("Asia/Seoul")

# ── 토큰 ──────────────────────────────────────────────────────────────
def get_token():
    r = requests.post(f"{BASE_URL}/oauth2/tokenP", json={
        "grant_type": "client_credentials",
        "appkey": APP_KEY, "appsecret": APP_SECRET
    }, timeout=10)
    return r.json()["access_token"]

# ── 현재가 조회 ────────────────────────────────────────────────────────
def get_price(token, ticker):
    try:
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY, "appsecret": APP_SECRET,
            "tr_id": "FHKST01010100"
        }
        params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker}
        r = requests.get(f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                         headers=headers, params=params, timeout=15)
        d = r.json().get("output", {})
        return int(d.get("stck_prpr", 0))
    except Exception as e:
        print(f"    ⚠️ 현재가 조회 타임아웃 ({ticker}): {e}")
        return 0

# ── 주문 ──────────────────────────────────────────────────────────────
def order(token, ticker, qty, side):
    if MOCK:
        tr_id = "VTTC0802U" if side == "buy" else "VTTC0801U"
    else:
        tr_id = "TTTC0802U" if side == "buy" else "TTTC0801U"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY, "appsecret": APP_SECRET,
        "tr_id": tr_id, "content-type": "application/json"
    }
    body = {
        "CANO": ACCOUNT_NO[:8],
        "ACNT_PRDT_CD": ACCOUNT_NO[8:] if len(ACCOUNT_NO) > 8 else "01",
        "PDNO": ticker,
        "ORD_DVSN": "01",
        "ORD_QTY": str(qty),
        "ORD_UNPR": "0"
    }
    r = requests.post(f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
                      headers=headers, json=body, timeout=10)
    result = r.json()
    return result.get("rt_cd") == "0", result.get("msg1", "")

# ── positions.json ────────────────────────────────────────────────────
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"budget": BUDGET, "used": 0, "positions": {}}

def save_positions(data):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── Discord ───────────────────────────────────────────────────────────
def discord(msg):
    if not DISCORD_WH:
        return
    mode_tag = "[모의] " if MOCK else "[실전] "
    try:
        requests.post(DISCORD_WH, json={"content": mode_tag + msg}, timeout=10)
    except Exception:
        pass

# ── 메인 ──────────────────────────────────────────────────────────────
def main():
    now = datetime.datetime.now(KST)
    mode_str = "🧪 모의투자" if MOCK else "💰 실전투자"
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 자동매매  {now.strftime('%Y%m%d %H:%M KST')}  [{mode_str}]")
    print(f"{'='*50}")

    if not os.path.exists(RESULTS_FILE):
        print("  ⚠️  results.json 없음 — screener 먼저 실행 필요")
        return

    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        results = json.load(f)

    market     = results.get("market_signal", {})
    signal_raw = market.get("final_signal", results.get("signal", ""))
    can_buy    = any(s in signal_raw for s in BUY_SIGNALS)
    print(f"  시장 시그널: {signal_raw}")
    print(f"  매수 가능: {'✅' if can_buy else '❌'}")

    print("\n  KIS 토큰 발급 중...")
    try:
        token = get_token()
        print("  ✅ 토큰 발급 완료")
    except Exception as e:
        print(f"  ⚠️ KIS 서버 연결 실패: {e}")
        return

    pos_data  = load_positions()
    positions = pos_data["positions"]

    # ── 보유 종목 체크 (분할 매도 / 손절 / 시간손절) ──────────────────
    print(f"\n  [보유 종목 체크] {len(positions)}개")

    for ticker, p in list(positions.items()):
        cur_price = get_price(token, ticker)
        if cur_price == 0:
            print(f"    {p['name']} — 현재가 조회 실패, 스킵")
            continue

        buy_price     = p["buy_price"]
        remaining_qty = p.get("remaining_qty", p["qty"])
        sold_stage    = p.get("sold_stage", 0)
        pnl           = (cur_price - buy_price) / buy_price
        pnl_str       = f"{pnl*100:+.1f}%"

        # 손절: 전량 매도
        if pnl <= STOP_LOSS:
            ok, msg = order(token, ticker, remaining_qty, "sell")
            if ok:
                profit = (cur_price - buy_price) * remaining_qty
                pos_data["used"] -= p["amount"]
                del positions[ticker]
                log = (f"📤 **손절** {p['name']} ({ticker})\n"
                       f"   매수가 {buy_price:,}원 → {cur_price:,}원 ({pnl_str}) | 손익 {profit:+,}원")
                print(f"    ✅ {log}")
                discord(log)
            else:
                print(f"    ❌ 손절 실패 {p['name']}: {msg}")
            time.sleep(0.3)
            continue

        # 시간손절: 14일 경과 + 수익률 < 0
        buy_date  = datetime.datetime.strptime(p.get("buy_date", "19000101"), "%Y%m%d")
        days_held = (now.replace(tzinfo=None) - buy_date).days
        if days_held >= 14 and pnl < 0:
            ok, msg = order(token, ticker, remaining_qty, "sell")
            if ok:
                profit = (cur_price - buy_price) * remaining_qty
                pos_data["used"] -= p["amount"]
                del positions[ticker]
                log = (f"📤 **시간손절** {p['name']} ({ticker})\n"
                       f"   {days_held}일 보유 ({pnl_str}) | 손익 {profit:+,}원")
                print(f"    ✅ {log}")
                discord(log)
            else:
                print(f"    ❌ 시간손절 실패 {p['name']}: {msg}")
            time.sleep(0.3)
            continue

        # 분할 익절 체크
        partial_done = False
        for stage_idx, (target_pnl, sell_ratio) in enumerate(PARTIAL_SELLS):
            if sold_stage > stage_idx:
                continue  # 이미 완료된 단계
            if pnl < target_pnl:
                break      # 목표 수익률 미달

            # 매도 수량 계산
            if stage_idx == len(PARTIAL_SELLS) - 1:
                sell_qty = remaining_qty  # 마지막 단계: 전량
            else:
                sell_qty = max(1, int(p["qty"] * sell_ratio))
                sell_qty = min(sell_qty, remaining_qty)

            if sell_qty < 1:
                sold_stage += 1
                continue

            ok, msg = order(token, ticker, sell_qty, "sell")
            if ok:
                profit        = (cur_price - buy_price) * sell_qty
                remaining_qty -= sell_qty
                sold_stage     = stage_idx + 1
                p["remaining_qty"] = remaining_qty
                p["sold_stage"]    = sold_stage

                stage_label = f"+{int(target_pnl*100)}% 도달 ({int(sell_ratio*100)}% 매도)"
                log = (f"📤 **분할매도 {stage_idx+1}차** [{stage_label}] {p['name']} ({ticker})\n"
                       f"   {cur_price:,}원 × {sell_qty}주 | 손익 {profit:+,}원 | 잔여 {remaining_qty}주")
                print(f"    ✅ {log}")
                discord(log)
                partial_done = True

                # 마지막 단계 or 잔여 수량 없으면 포지션 종료
                if remaining_qty <= 0 or stage_idx == len(PARTIAL_SELLS) - 1:
                    pos_data["used"] -= p["amount"]
                    del positions[ticker]
            else:
                print(f"    ❌ 분할매도 실패 {p['name']}: {msg}")
            time.sleep(0.3)
            break  # 한 번에 한 단계씩만

        if ticker in positions and not partial_done:
            stage_info = f" (분할{sold_stage}차 완료)" if sold_stage > 0 else ""
            print(f"    {p['name']} ({ticker}) — {cur_price:,}원 ({pnl_str}){stage_info} 유지")

    # ── 매수 로직 ──────────────────────────────────────────────────────
    if not can_buy:
        print("\n  매수 시그널 없음 — 매도 체크만 완료")
        save_positions(pos_data)
        return

    stocks  = results.get("stocks", results.get("results", []))
    a_grade = [
        s for s in stocks
        if s.get("grade") == "A"
        and s["ticker"] not in positions
        and float(s.get("rsi", 99))        <= 65
        and float(s.get("ch20", 999))      <= 30
        and float(s.get("vol_trend", -999)) >= 0
        and s.get("macd_bull") is not False
    ]

    print(f"\n  [매수 후보] A등급: {len(a_grade)}개")

    slots = MAX_POSITIONS - len(positions)
    if slots <= 0:
        print("  최대 보유 종목 도달 — 매수 스킵")
        save_positions(pos_data)
        return

    remaining_budget = BUDGET - pos_data["used"]
    if remaining_budget < 10_000:
        print(f"  예산 부족 ({remaining_budget:,}원) — 매수 스킵")
        save_positions(pos_data)
        return

    buy_count = min(slots, len(a_grade))
    if buy_count == 0:
        print("  A등급 신규 후보 없음")
        save_positions(pos_data)
        return

    per_stock = remaining_budget // buy_count
    print(f"  슬롯: {slots}개 | 후보: {len(a_grade)}개 | 종목당 {per_stock:,}원")

    bought = 0
    for stock in a_grade[:buy_count]:
        ticker    = stock["ticker"]
        name      = stock["name"]
        cur_price = get_price(token, ticker)
        if cur_price == 0:
            print(f"    {name} — 현재가 조회 실패, 스킵")
            continue

        qty = per_stock // cur_price
        if qty < 1:
            print(f"    {name} ({ticker}) — {cur_price:,}원, 수량 부족 스킵")
            continue

        actual_amount = cur_price * qty
        ok, msg = order(token, ticker, qty, "buy")
        if ok:
            positions[ticker] = {
                "name": name, "buy_price": cur_price,
                "qty": qty, "remaining_qty": qty,
                "sold_stage": 0,
                "amount": actual_amount,
                "buy_date": now.strftime("%Y%m%d")
            }
            pos_data["used"] += actual_amount
            bought += 1
            log = (f"📥 **매수** {name} ({ticker})\n"
                   f"   {cur_price:,}원 × {qty}주 = {actual_amount:,}원 | A등급\n"
                   f"   익절 목표: +10%(40%) → +18%(35%) → +25%(전량)")
            print(f"    ✅ {log}")
            discord(log)
        else:
            print(f"    ❌ 매수 실패 {name}: {msg}")
        time.sleep(0.3)

    print(f"\n  매수 {bought}건 완료 | 총 사용 {pos_data['used']:,}원 / {BUDGET:,}원")
    save_positions(pos_data)
    print(f"  💾 positions.json 저장 완료")
    print(f"\n✅ 자동매매 완료!")

if __name__ == "__main__":
    main()
