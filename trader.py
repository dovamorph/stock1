#!/usr/bin/env python3
"""
StockPilot KR — 자동매매 (trader.py)
장투 포트폴리오: 350만원 / A등급만 / 익절 10%·18%·25% / 손절 -10% / 시간손절 14일
단타 포트폴리오: 150만원 / A·B·C등급 / 익절 7%·10%·13% / 손절 -5% / 시간손절 5일
"""
import os, json, time, datetime, requests
from zoneinfo import ZoneInfo

# ── 장투 설정 ──────────────────────────────────────────────────────────
LONG_BUDGET        = 3_500_000
LONG_STOP_LOSS     = -0.10
LONG_TIME_STOP     = 14
LONG_MAX_POS       = 3
LONG_PARTIAL_SELLS = [
    (0.10, 0.40),
    (0.18, 0.35),
    (0.25, 1.00),
]

# ── 단타 설정 ──────────────────────────────────────────────────────────
SHORT_BUDGET        = 1_500_000
SHORT_STOP_LOSS     = -0.05
SHORT_TIME_STOP     = 5
SHORT_MAX_POS       = 3
SHORT_PARTIAL_SELLS = [
    (0.07, 0.33),
    (0.10, 0.50),
    (0.13, 1.00),
]
SHORT_RSI_MIN = 45
SHORT_RSI_MAX = 65

# ── 공통 설정 ──────────────────────────────────────────────────────────
BUY_SIGNALS    = {"강한 매수", "매수 우위"}
RESULTS_FILE   = "results.json"
POSITIONS_FILE = "positions.json"

MOCK       = os.environ.get("KIS_MOCK", "true").lower() == "true"
# 모의투자/실전 모두 실전 KIS API로 가격 조회 (모의투자 서버 포트 차단 우회)
APP_KEY    = os.environ.get("KIS_APP_KEY",    os.environ.get("KIS_APP_KEY_MOCK",    ""))
APP_SECRET = os.environ.get("KIS_APP_SECRET", os.environ.get("KIS_APP_SECRET_MOCK", ""))
ACCOUNT_NO = os.environ.get("KIS_ACCOUNT_NO", os.environ.get("KIS_ACCOUNT_MOCK",    ""))
DISCORD_WH = os.environ.get("DISCORD_WEBHOOK", "")
BASE_URL   = "https://openapi.koreainvestment.com:9443"   # 항상 실전 서버 (가격조회용)
KST        = ZoneInfo("Asia/Seoul")

# ── KIS API ────────────────────────────────────────────────────────────
def get_token():
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE_URL}/oauth2/tokenP", json={
                "grant_type": "client_credentials",
                "appkey": APP_KEY, "appsecret": APP_SECRET
            }, timeout=30)
            return r.json()["access_token"]
        except Exception as e:
            print(f"  토큰 발급 시도 {attempt+1}/3 실패: {e}")
            time.sleep(3)
    raise Exception("토큰 발급 최종 실패")

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
        cur   = int(d.get("stck_prpr", 0))   # 현재가
        open_ = int(d.get("stck_oprc", 0))   # 시가
        high  = int(d.get("stck_hgpr", 0))   # 고가
        low   = int(d.get("stck_lwpr", 0))   # 저가
        return cur, open_, high, low
    except Exception as e:
        print(f"    현재가 조회 실패 ({ticker}): {e}")
        return 0, 0, 0, 0

def is_good_candle(cur, open_, high, low):
    """
    매수 적합한 캔들인지 판단 (방법2)
    ① 상승 캔들: 현재가 >= 시가
    ② 반등 캔들: 하락 중이지만 저점 대비 20% 이상 반등
    """
    if cur <= 0 or open_ <= 0:
        return True  # 데이터 없으면 통과
    if cur >= open_:
        return True  # 상승 캔들
    rng = high - low
    if rng > 0 and (cur - low) / rng >= 0.2:
        return True  # 저점 대비 20% 이상 반등 중
    return False

def order(token, ticker, qty, side):
    # 모의투자: 실제 주문 없이 성공으로 처리 (가격은 실전 API로 이미 조회됨)
    if MOCK:
        return True, "모의주문 처리"
    tr_id = "TTTC0802U" if side == "buy" else "TTTC0801U"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY, "appsecret": APP_SECRET,
        "tr_id": tr_id, "content-type": "application/json"
    }
    body = {
        "CANO": ACCOUNT_NO[:8],
        "ACNT_PRDT_CD": ACCOUNT_NO[8:] if len(ACCOUNT_NO) > 8 else "01",
        "PDNO": ticker, "ORD_DVSN": "01",
        "ORD_QTY": str(qty), "ORD_UNPR": "0"
    }
    r = requests.post(f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
                      headers=headers, json=body, timeout=10)
    result = r.json()
    return result.get("rt_cd") == "0", result.get("msg1", "")

# ── positions.json ─────────────────────────────────────────────────────
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 구버전 마이그레이션
        if "positions" in data and "long" not in data:
            print("  📦 구버전 → 장투/단타 분리 마이그레이션")
            data = {
                "long":  {"budget": LONG_BUDGET,  "used": data.get("used", 0),
                          "positions": data.get("positions", {})},
                "short": {"budget": SHORT_BUDGET, "used": 0, "positions": {}},
                "trade_history": data.get("trade_history", [])
            }
        return data
    return {
        "long":  {"budget": LONG_BUDGET,  "used": 0, "positions": {}},
        "short": {"budget": SHORT_BUDGET, "used": 0, "positions": {}},
        "trade_history": []
    }

def save_positions(data):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── 거래내역 기록 ──────────────────────────────────────────────────────
def log_trade(data, ticker, pos, sell_price, sell_qty, reason, now, ptype):
    buy_price = pos["buy_price"]
    pnl       = round((sell_price - buy_price) * sell_qty)
    pnl_pct   = round((sell_price - buy_price) / buy_price * 100, 2)
    trade = {
        "ticker": ticker, "name": pos["name"], "type": ptype,
        "buy_price": buy_price, "sell_price": sell_price, "qty": sell_qty,
        "buy_date": pos.get("buy_date", ""),
        "sell_date": now.strftime("%Y%m%d"), "sell_time": now.strftime("%H:%M"),
        "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason
    }
    if "trade_history" not in data:
        data["trade_history"] = []
    data["trade_history"].insert(0, trade)
    data["trade_history"] = data["trade_history"][:50]

# ── Discord ────────────────────────────────────────────────────────────
def discord(msg):
    if not DISCORD_WH:
        return
    try:
        requests.post(DISCORD_WH, json={"content": ("[모의] " if MOCK else "[실전] ") + msg}, timeout=10)
    except Exception:
        pass

# ── 포트폴리오 매도 처리 ───────────────────────────────────────────────
def process_sells(token, data, ptype, partial_sells, stop_loss, time_stop, now):
    port      = data[ptype]
    positions = port["positions"]
    print(f"\n  [{ptype} 보유종목 체크] {len(positions)}개")

    for ticker, p in list(positions.items()):
        cur_price, _, _, _ = get_price(token, ticker)
        if cur_price == 0:
            print(f"    {p['name']} — 현재가 조회 실패, 스킵")
            continue

        buy_price     = p["buy_price"]
        remaining_qty = p.get("remaining_qty", p["qty"])
        sold_stage    = p.get("sold_stage", 0)
        pnl           = (cur_price - buy_price) / buy_price
        pnl_str       = f"{pnl*100:+.1f}%"

        # 손절
        if pnl <= stop_loss:
            ok, msg = order(token, ticker, remaining_qty, "sell")
            if ok:
                log_trade(data, ticker, p, cur_price, remaining_qty, "손절", now, ptype)
                port["used"] = max(0, port["used"] - p.get("amount", 0))
                del positions[ticker]
                profit = (cur_price - buy_price) * remaining_qty
                log = f"📤 **[{ptype}] 손절** {p['name']} ({pnl_str}) | {profit:+,}원"
                print(f"    ✅ {log}"); discord(log)
            else:
                print(f"    ❌ 손절 실패 {p['name']}: {msg}")
            time.sleep(0.3); continue

        # 시간손절
        try:
            buy_dt    = datetime.datetime.strptime(p.get("buy_date", "19000101"), "%Y%m%d")
            days_held = (now.replace(tzinfo=None) - buy_dt).days
        except:
            days_held = 0

        if days_held >= time_stop and pnl < 0:
            ok, msg = order(token, ticker, remaining_qty, "sell")
            if ok:
                log_trade(data, ticker, p, cur_price, remaining_qty, f"시간손절({days_held}일)", now, ptype)
                port["used"] = max(0, port["used"] - p.get("amount", 0))
                del positions[ticker]
                profit = (cur_price - buy_price) * remaining_qty
                log = f"📤 **[{ptype}] 시간손절** {p['name']} {days_held}일 ({pnl_str}) | {profit:+,}원"
                print(f"    ✅ {log}"); discord(log)
            else:
                print(f"    ❌ 시간손절 실패 {p['name']}: {msg}")
            time.sleep(0.3); continue

        # 분할 익절
        partial_done = False
        for idx, (target_pnl, sell_ratio) in enumerate(partial_sells):
            if sold_stage > idx: continue
            if pnl < target_pnl: break
            is_last  = (idx == len(partial_sells) - 1)
            sell_qty = remaining_qty if is_last else max(1, min(int(p["qty"] * sell_ratio), remaining_qty))
            if sell_qty < 1:
                sold_stage += 1; continue

            ok, msg = order(token, ticker, sell_qty, "sell")
            if ok:
                profit         = (cur_price - buy_price) * sell_qty
                remaining_qty -= sell_qty
                sold_stage     = idx + 1
                p["remaining_qty"] = remaining_qty
                p["sold_stage"]    = sold_stage
                sold_cost          = round(buy_price * sell_qty)
                port["used"]       = max(0, port["used"] - sold_cost)
                p["amount"]        = max(0, p.get("amount", 0) - sold_cost)
                log_trade(data, ticker, p, cur_price, sell_qty, f"분할매도{idx+1}차(+{int(target_pnl*100)}%)", now, ptype)
                log = (f"📤 **[{ptype}] 분할{idx+1}차** +{int(target_pnl*100)}% "
                       f"{p['name']} | {sell_qty}주 {profit:+,}원 | 잔여{remaining_qty}주")
                print(f"    ✅ {log}"); discord(log)
                partial_done = True
                if remaining_qty <= 0 or is_last:
                    port["used"] = max(0, port["used"] - p.get("amount", 0))
                    del positions[ticker]
            else:
                print(f"    ❌ 분할매도 실패 {p['name']}: {msg}")
            time.sleep(0.3); break

        if ticker in positions and not partial_done:
            stage_info = f" 분할{sold_stage}차완료" if sold_stage > 0 else ""

            # ── 장투 2차 매수 체크 ────────────────────────────────
            if ptype == "long" and not p.get("stage2_done", True):
                s1_price  = p.get("stage1_price", buy_price)
                s2_budget = p.get("stage2_budget", 0)
                trigger   = s1_price * 0.95   # 1차 대비 -5%

                if cur_price <= trigger and s2_budget >= cur_price:
                    if is_good_candle(cur_price, *([0,0,0] if True else [])):
                        # 현재가 재조회해서 캔들 확인
                        cur2, open2, high2, low2 = get_price(token, ticker)
                        if is_good_candle(cur2, open2, high2, low2) and cur2 > 0:
                            qty2 = s2_budget // cur2
                            if qty2 >= 1:
                                ok2, _ = order(token, ticker, qty2, "buy")
                                if ok2:
                                    amt2 = cur2 * qty2
                                    # 평균 매수가 재계산
                                    total_qty  = p["qty"] + qty2
                                    avg_price  = round((buy_price * p["qty"] + cur2 * qty2) / total_qty)
                                    p["qty"]          = total_qty
                                    p["remaining_qty"]= p.get("remaining_qty", p["qty"] - qty2) + qty2
                                    p["buy_price"]    = avg_price
                                    p["amount"]       = p.get("amount", 0) + amt2
                                    p["stage2_done"]  = True
                                    port["used"]     += amt2
                                    log = (f"📥 **[장투] 2차매수** {p['name']} ({ticker})\n"
                                           f"   {cur2:,}원 × {qty2}주 = {amt2:,}원\n"
                                           f"   평균단가 {avg_price:,}원 (1차 {s1_price:,}원 대비 -{(1-cur2/s1_price)*100:.1f}%)")
                                    print(f"    ✅ {log}"); discord(log)
                                    time.sleep(1.0)
                        else:
                            print(f"    {p['name']} 2차매수 대기 — 아직 반등 미확인 ({cur_price:,}원 트리거{trigger:,}원)")
                    
            stage_info2 = " [2차완료]" if p.get("stage2_done") else (" [2차대기]" if not p.get("stage2_done", True) and ptype=="long" else "")
            print(f"    {p['name']} ({ticker}) — {cur_price:,}원 ({pnl_str}){stage_info}{stage_info2} 유지")

# ── 포트폴리오 매수 처리 ───────────────────────────────────────────────
def process_buys(token, data, stocks, ptype, max_pos, budget, partial_sells,
                 grade_filter, rsi_min, rsi_max, now):
    port         = data[ptype]
    positions    = port["positions"]
    all_tickers  = set(data["long"]["positions"]) | set(data["short"]["positions"])

    slots            = max_pos - len(positions)
    remaining_budget = budget - port["used"]

    if slots <= 0:
        print(f"  [{ptype}] 최대 종목 도달 — 매수 스킵"); return
    if remaining_budget < 10_000:
        print(f"  [{ptype}] 예산 부족 ({remaining_budget:,}원) — 매수 스킵"); return

    candidates = [
        s for s in stocks
        if s.get("grade") in grade_filter
        and s["ticker"] not in all_tickers
        and rsi_min <= float(s.get("rsi", 0)) <= rsi_max
        and float(s.get("vol_trend", -999)) >= 0
        and (ptype == "short" or (
            float(s.get("ch20", 999)) <= 30
            and s.get("macd_bull") is not False
        ))
    ]

    print(f"\n  [{ptype} 매수 후보] {len(candidates)}개 (등급:{grade_filter} RSI:{rsi_min}~{rsi_max})")
    if not candidates:
        print(f"  [{ptype}] 신규 후보 없음"); return

    buy_count = min(slots, len(candidates))
    per_stock = remaining_budget // buy_count
    bought    = 0

    for stock in candidates[:buy_count]:
        ticker    = stock["ticker"]
        name      = stock["name"]
        cur_price, open_, high, low = get_price(token, ticker)
        if cur_price == 0:
            print(f"    {name} — 현재가 조회 실패, 스킵"); continue

        qty = per_stock // cur_price
        if qty < 1:
            print(f"    {name} ({ticker}) — {cur_price:,}원 수량 부족, 다음 후보로")
            buy_count = min(buy_count + 1, len(candidates))
            continue

        # ── 캔들 방향 체크 ────────────────────────────────────────
        if not is_good_candle(cur_price, open_, high, low):
            candle_info = f"현재{cur_price:,} 시가{open_:,} 저가{low:,} 고가{high:,}"
            print(f"    {name} — 하락 캔들 진입 보류 ({candle_info})")
            continue

        # ── 장투: 1차 60% 매수 ───────────────────────────────────
        if ptype == "long":
            qty1 = max(1, int(qty * 0.6))
            amt1 = cur_price * qty1
            ok, msg = None, ""
            for attempt in range(2):
                try:
                    ok, msg = order(token, ticker, qty1, "buy")
                    break
                except Exception as e:
                    print(f"    {name} 연결 오류 (시도{attempt+1}): {e}")
                    time.sleep(2)
            if ok is None:
                print(f"    {name} 연결 실패, 스킵"); time.sleep(1); continue
            if ok:
                positions[ticker] = {
                    "name": name, "grade": stock.get("grade", "?"),
                    "buy_price": cur_price, "qty": qty1, "remaining_qty": qty1,
                    "sold_stage": 0, "amount": amt1,
                    "buy_date": now.strftime("%Y%m%d"),
                    # 분할매수 정보
                    "buy_stage": 1,
                    "stage1_price": cur_price,
                    "stage1_qty": qty1,
                    "stage2_done": False,
                    "stage2_budget": per_stock - amt1   # 2차용 남은 예산
                }
                port["used"] += amt1
                bought += 1
                log = (f"📥 **[장투] 1차매수** {name} ({ticker}) {stock.get('grade','')}등급\n"
                       f"   {cur_price:,}원 × {qty1}주 = {amt1:,}원 (전체예산의 60%)\n"
                       f"   2차매수: 1차 대비 -5% + 반등 확인 시")
                print(f"    ✅ {log}"); discord(log)
            else:
                print(f"    ❌ 매수 실패 {name}: {msg}")
            time.sleep(1.0)
            continue

        # ── 단타: 단번 매수 ──────────────────────────────────────
        actual_amount = cur_price * qty
        ok, msg = None, ""
        for attempt in range(2):
            try:
                ok, msg = order(token, ticker, qty, "buy")
                break
            except Exception as e:
                print(f"    {name} 연결 오류 (시도{attempt+1}): {e}")
                time.sleep(2)
        if ok is None:
            print(f"    {name} 연결 실패, 스킵"); time.sleep(1); continue
        if ok:
            positions[ticker] = {
                "name": name, "grade": stock.get("grade", "?"),
                "buy_price": cur_price, "qty": qty, "remaining_qty": qty,
                "sold_stage": 0, "amount": actual_amount,
                "buy_date": now.strftime("%Y%m%d")
            }
            port["used"] += actual_amount
            bought += 1
            sells_str = " → ".join([f"+{int(p*100)}%({int(r*100)}%)" for p, r in partial_sells])
            log = (f"📥 **[{ptype}] 매수** {name} ({ticker}) {stock.get('grade','')}등급\n"
                   f"   {cur_price:,}원 × {qty}주 = {actual_amount:,}원\n"
                   f"   익절: {sells_str}")
            print(f"    ✅ {log}"); discord(log)
        else:
            print(f"    ❌ 매수 실패 {name}: {msg}")
        time.sleep(1.0)

    if bought > 0:
        print(f"  [{ptype}] 매수 {bought}건 | 사용 {port['used']:,}원 / {budget:,}원")

# ── 메인 ──────────────────────────────────────────────────────────────
def main():
    now      = datetime.datetime.now(KST)
    mode_str = "🧪 모의투자" if MOCK else "💰 실전투자"
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 자동매매  {now.strftime('%Y%m%d %H:%M KST')}  [{mode_str}]")
    print(f"  장투 {LONG_BUDGET//10000}만원 | 단타 {SHORT_BUDGET//10000}만원")
    print(f"{'='*50}")

    if not os.path.exists(RESULTS_FILE):
        print("  ⚠️  results.json 없음"); return

    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        results = json.load(f)

    # 매수는 장중(09:00~15:30)에만 허용
    now_time = now.time()
    import datetime as dt
    is_market_open = dt.time(9, 0) <= now_time <= dt.time(15, 30)

    market     = results.get("market_signal", {})
    signal_raw = market.get("final_signal", results.get("signal", ""))
    rsi_14     = float(market.get("rsi_14", 50))
    kospi_ch5  = float(market.get("kospi_ch5", 0))
    can_buy    = any(s in signal_raw for s in BUY_SIGNALS)

    if can_buy and not is_market_open:
        can_buy = False
        print(f"  ⚠️ 장 마감 후 ({now_time.strftime('%H:%M')}) — 신규 매수 중단")

    if can_buy and kospi_ch5 < 0:
        can_buy = False
        print(f"  ⚠️ KOSPI 5일 하락 중 ({kospi_ch5:+.1f}%) — 신규 매수 중단")

    print(f"  시장 시그널: {signal_raw} | RSI {rsi_14:.0f} | KOSPI5일 {kospi_ch5:+.1f}%")
    print(f"  매수 가능: {'✅' if can_buy else '❌'}")

    print("\n  KIS 토큰 발급 중...")
    try:
        token = get_token()
        print("  ✅ 토큰 발급 완료")
    except Exception as e:
        print(f"  ⚠️ KIS 서버 연결 실패: {e}"); return

    data = load_positions()

    # 매도 체크
    process_sells(token, data, "long",  LONG_PARTIAL_SELLS,  LONG_STOP_LOSS,  LONG_TIME_STOP,  now)
    process_sells(token, data, "short", SHORT_PARTIAL_SELLS, SHORT_STOP_LOSS, SHORT_TIME_STOP, now)

    # 매수
    if not can_buy:
        print("\n  매수 시그널 없음 — 매도 체크만 완료")
        save_positions(data); return

    stocks = results.get("results", [])

    process_buys(token, data, stocks, "long",
                 LONG_MAX_POS, LONG_BUDGET, LONG_PARTIAL_SELLS,
                 {"A"}, 0, 65, now)

    process_buys(token, data, stocks, "short",
                 SHORT_MAX_POS, SHORT_BUDGET, SHORT_PARTIAL_SELLS,
                 {"A","B","C"}, SHORT_RSI_MIN, SHORT_RSI_MAX, now)

    save_positions(data)
    print(f"\n  💾 positions.json 저장 완료")
    print(f"\n✅ 자동매매 완료!")

if __name__ == "__main__":
    main()
