#!/usr/bin/env python3
"""
StockPilot KR — 자동매매 (trader.py) [통합 시스템]
장투·단타·모멘텀 구분 없이 여러 지표를 종합해 자동매매.

[설계]
- 예산 700만원 / 최대 7종목 / F등급 제외
- 진입: RSI 50~75 + 매도주도 아님 + 거래량 유지 + 진입점수 정렬
- 포지션: 등급별 차등 (A=150만 B=100만 C=70만 D=50만)
- 청산: 손절-7% / 익절+10%·+20% / RSI78+ / 매도주도전환 / 7일

[모듈]
- market_regime : BULL/SIDEWAYS/BEAR → 포지션배율
- defense       : 갭다운/연속손실/서킷브레이커/블랙리스트
- expiry.py     : 옵션만기일 guard
- analytics     : 포지션 사이징 참조용
"""
import os, json, time, datetime, requests
from zoneinfo import ZoneInfo

from market_regime import get_market_regime
from defense import (
    can_buy          as defense_can_buy,
    is_trading_suspended,
    record_trade_result,
    check_stock_crash,
    check_gap_down,
    check_circuit_breaker,
)

# ── 통합 매매 설정 ─────────────────────────────────────────────────────
BUDGET         = 7_000_000
MAX_POS        = 7

GRADE_AMOUNT   = {"A": 1_500_000, "B": 1_000_000, "C": 700_000, "D": 500_000}

RSI_MIN        = 50
RSI_MAX        = 75
VOL_TREND_MIN  = -10.0

STOP_LOSS      = -0.07
TP1            = 0.10
TP2            = 0.20
RSI_EXIT       = 78
MAX_DAYS       = 7

REGIME_MULT    = {"BULL": 1.0, "SIDEWAYS": 0.7, "BEAR": 0.0}

# ── 공통 설정 ─────────────────────────────────────────────────────────
RESULTS_FILE   = "results.json"
POSITIONS_FILE = "positions.json"
EXPIRY_FILE    = "expiry_result.json"

MOCK       = os.environ.get("KIS_MOCK", "true").lower() == "true"
APP_KEY    = os.environ.get("KIS_APP_KEY",    os.environ.get("KIS_APP_KEY_MOCK",    ""))
APP_SECRET = os.environ.get("KIS_APP_SECRET", os.environ.get("KIS_APP_SECRET_MOCK", ""))
ACCOUNT_NO = os.environ.get("KIS_ACCOUNT_NO", os.environ.get("KIS_ACCOUNT_MOCK",    ""))
DISCORD_WH = os.environ.get("DISCORD_WEBHOOK", "")
BASE_URL   = "https://openapi.koreainvestment.com:9443"
KST        = ZoneInfo("Asia/Seoul")

# ── KIS API ───────────────────────────────────────────────────────────
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
    """현재가 조회. 실패 시 0 반환."""
    for attempt in range(3):
        try:
            r = requests.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers={"authorization": f"Bearer {token}", "appkey": APP_KEY,
                         "appsecret": APP_SECRET, "tr_id": "FHKST01010100"},
                params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker},
                timeout=15
            )
            d = r.json().get("output", {})
            cur = int(d.get("stck_prpr", 0))
            if cur > 0:
                return cur
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
    discord(f"⚠️ 현재가 조회 실패 [{ticker}]")
    return 0

def place_order(token, ticker, qty, price, tr_id, side):
    """주문 실행. 성공 True 반환."""
    try:
        body = {
            "CANO": ACCOUNT_NO[:8], "ACNT_PRDT_CD": ACCOUNT_NO[9:],
            "PDNO": ticker, "ORD_DVSN": "01",
            "ORD_QTY": str(qty), "ORD_UNPR": str(price),
        }
        r = requests.post(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers={"authorization": f"Bearer {token}", "appkey": APP_KEY,
                     "appsecret": APP_SECRET, "tr_id": tr_id, "custtype": "P",
                     "Content-Type": "application/json"},
            json=body, timeout=15
        )
        res = r.json()
        ok  = res.get("rt_cd") == "0"
        if not ok:
            print(f"    ⚠️ [{side}] {ticker} 주문 실패: {res.get('msg1','')}")
        return ok
    except Exception as e:
        print(f"    ⚠️ [{side}] {ticker} 예외: {e}")
        return False

# ── Discord ───────────────────────────────────────────────────────────
def discord(msg):
    if not DISCORD_WH:
        return
    try:
        requests.post(DISCORD_WH, json={"content": msg[:1900]}, timeout=10)
    except:
        pass

# ── positions.json ────────────────────────────────────────────────────
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 구버전 마이그레이션 (long/short → portfolio)
        if "long" in data and "portfolio" not in data:
            print("  📦 구버전 → 통합 시스템 마이그레이션")
            old_pos = {}
            old_pos.update(data.get("long",     {}).get("positions", {}))
            old_pos.update(data.get("short",    {}).get("positions", {}))
            old_pos.update(data.get("momentum", {}).get("positions", {}))
            data = {
                "portfolio":    {"budget": BUDGET, "used": 0, "positions": old_pos},
                "trade_history": data.get("trade_history", [])
            }
        # 예산 자동 동기화
        if data.get("portfolio", {}).get("budget", 0) != BUDGET:
            data.setdefault("portfolio", {})["budget"] = BUDGET
        return data
    return {
        "portfolio":    {"budget": BUDGET, "used": 0, "positions": {}},
        "trade_history": []
    }

def save_positions(data):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def log_trade(data, ticker, name, buy_price, sell_price, sell_qty, reason, now):
    pnl     = round((sell_price - buy_price) * sell_qty)
    pnl_pct = round((sell_price - buy_price) / buy_price * 100, 2)
    trade = {
        "ticker": ticker, "name": name,
        "buy_price": buy_price, "sell_price": sell_price, "qty": sell_qty,
        "sell_date": now.strftime("%Y%m%d"), "sell_time": now.strftime("%H:%M"),
        "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason
    }
    data.setdefault("trade_history", []).insert(0, trade)
    data["trade_history"] = data["trade_history"][:100]

# ── 만기일 guard ──────────────────────────────────────────────────────
def load_expiry_guard() -> dict:
    default = {"note": "만기 정보 없음", "allow_new_longterm": True,
               "pos_mult_adj": 0.0, "score_penalty": 0}
    try:
        if os.path.exists(EXPIRY_FILE):
            with open(EXPIRY_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            guard = d.get("guard", default)
            d_day = d.get("d_day", 99)
            print(f"  만기일: {d.get('expiry_date','')} (D-{d_day}) | {guard.get('note','')}")
            return guard
    except Exception as e:
        print(f"  만기 로드 실패: {e}")
    return default

# ── 통합 매도 ─────────────────────────────────────────────────────────
def unified_sells(token, data, stocks, now):
    """
    보유 종목 청산 체크.
    손절 -7% / 1차익절 +10%(절반) / 2차익절 +20%(전량)
    RSI 78 이상(수익 중) / 매도주도 전환(손실 중) / 7일 시간손절
    """
    port      = data.setdefault("portfolio", {"budget": BUDGET, "used": 0, "positions": {}})
    positions = port.get("positions", {})

    if not positions:
        print("  [보유] 없음")
        return

    stock_map = {s["ticker"]: s for s in stocks}
    today     = now.date()

    print(f"  [보유] {len(positions)}종목 체크 중...")

    for ticker, pos in list(positions.items()):
        name      = pos.get("name", ticker)
        buy_price = pos.get("buy_price", 0)
        qty       = pos.get("qty", 0)
        sold_qty  = pos.get("sold_qty", 0)
        try:
            buy_date = datetime.date.fromisoformat(pos.get("buy_date", str(today)))
        except:
            buy_date = today
        days_held = (today - buy_date).days

        cur_price = get_price(token, ticker)
        if not cur_price or not buy_price:
            continue

        pnl_pct  = (cur_price - buy_price) / buy_price
        s        = stock_map.get(ticker, {})
        rsi      = float(s.get("rsi", 50))
        vol_char = s.get("vol_char", "")

        reason   = None
        sell_qty = qty

        if pnl_pct <= STOP_LOSS:
            reason = f"손절 {pnl_pct*100:.1f}%"

        elif pnl_pct >= TP2 and sold_qty == 0:
            reason = f"2차익절 {pnl_pct*100:.1f}%"

        elif pnl_pct >= TP1 and sold_qty == 0:
            # 1차 익절: 절반만 매도
            sell_qty = max(1, qty // 2)
            reason   = f"1차익절 {pnl_pct*100:.1f}%"

        elif rsi >= RSI_EXIT and pnl_pct > 0:
            reason = f"RSI과열({rsi:.0f}) {pnl_pct*100:.1f}%"

        elif "매도주도" in vol_char and pnl_pct < 0:
            reason = f"매도주도전환 {pnl_pct*100:.1f}%"

        elif days_held >= MAX_DAYS:
            reason = f"시간손절 {days_held}일 ({pnl_pct*100:.1f}%)"

        if not reason:
            pnl_str = f"{pnl_pct*100:+.1f}%"
            print(f"    {name} | {pnl_str} | RSI {rsi:.0f} | {days_held}일 | 유지")
            continue

        tr_id = "VTTC0801U" if MOCK else "TTTC0801U"
        ok    = place_order(token, ticker, sell_qty, cur_price, tr_id, "매도")
        if ok:
            pnl = round((cur_price - buy_price) * sell_qty)
            log_trade(data, ticker, name, buy_price, cur_price, sell_qty, reason, now)
            if pnl < 0:
                record_trade_result(ticker, pnl)

            if sell_qty >= qty:
                # 전량 청산
                port["used"] = max(0, port.get("used", 0) - int(buy_price * qty))
                del positions[ticker]
                icon = "✅" if pnl >= 0 else "🔴"
                print(f"    {icon} [청산] {name} | {reason} | {pnl:+,}원")
                discord(f"{icon} 청산: {name} | {reason} | {pnl:+,}원")
            else:
                # 1차 익절: 잔여 보유
                pos["qty"]       = qty - sell_qty
                pos["sold_qty"]  = sold_qty + sell_qty
                port["used"] = max(0, port.get("used", 0) - int(buy_price * sell_qty))
                print(f"    ✅ [1차익절] {name} | {sell_qty}주 | {pnl:+,}원 | 잔여 {pos['qty']}주")

        time.sleep(0.5)

# ── 통합 매수 ─────────────────────────────────────────────────────────
def unified_buys(token, data, stocks, now, allow_buy, regime_mult, kospi_ch5, expiry_guard):
    """
    진입: F등급 제외 + RSI 50~75 + 매도주도 아님 + 거래량 유지
    등급별 투자금 × 국면 배율
    진입 점수 내림차순 정렬
    """
    if not allow_buy:
        return

    # 만기일 D-5 이하이면 매수 제한
    if not expiry_guard.get("allow_new_longterm", True):
        print(f"  [매수 제한] {expiry_guard.get('note','만기일 근접')}")
        return

    port      = data.setdefault("portfolio", {"budget": BUDGET, "used": 0, "positions": {}})
    positions = port.get("positions", {})
    slots     = MAX_POS - len(positions)

    if slots <= 0:
        print(f"  [매수] 최대 {MAX_POS}종목 도달")
        return

    remaining = BUDGET - port.get("used", 0)
    if remaining < min(GRADE_AMOUNT.values()):
        print(f"  [매수] 예산 부족 ({remaining:,}원)")
        return

    # 급등장 거래량 기준 완화
    vt_min = -20.0 if kospi_ch5 >= 5.0 else VOL_TREND_MIN
    surge  = "🚀 급등장" if kospi_ch5 >= 5.0 else ""

    candidates = [
        s for s in stocks
        if s.get("grade") not in ("F",)
        and RSI_MIN <= float(s.get("rsi", 0)) <= RSI_MAX
        and "매도주도" not in s.get("vol_char", "")
        and float(s.get("vol_trend", -999)) >= vt_min
        and s.get("macd_bull") in (True, None)
        and s["ticker"] not in positions
    ]

    candidates.sort(key=lambda x: -(x.get("entry_score") or 0))

    print(f"\n  [매수 후보] {len(candidates)}개 "
          f"(RSI {RSI_MIN}~{RSI_MAX} vol≥{vt_min:.0f}% 배율:{regime_mult:.1f}x {surge})")

    if not candidates:
        print("  [매수] 후보 없음"); return

    bought = 0
    for stock in candidates[:slots]:
        ticker    = stock["ticker"]
        name      = stock.get("name", ticker)
        grade     = stock.get("grade", "C")
        rsi       = float(stock.get("rsi", 50))
        entry     = stock.get("entry_score") or 0
        rc        = stock.get("rank_change")
        rc_str    = "NEW" if rc is None else (f"▲{rc}" if isinstance(rc, (int,float)) and rc > 0 else "→")

        base_amt      = GRADE_AMOUNT.get(grade, 500_000)
        invest_target = int(base_amt * regime_mult)

        if invest_target > remaining:
            print(f"    {name} — 예산 부족 ({remaining:,}원 < {invest_target:,}원)")
            continue

        d_check = defense_can_buy(ticker, "unified")
        if not d_check["ok"]:
            print(f"    {name} — defense 차단: {d_check['reason']}")
            continue

        cur_price = get_price(token, ticker)
        if not cur_price:
            continue

        qty    = max(1, invest_target // cur_price)
        invest = cur_price * qty

        tr_id = "VTTC0802U" if MOCK else "TTTC0802U"
        ok    = place_order(token, ticker, qty, cur_price, tr_id, "매수")
        if ok:
            positions[ticker] = {
                "name":     name,
                "grade":    grade,
                "buy_price": cur_price,
                "qty":       qty,
                "sold_qty":  0,
                "buy_date":  str(now.date()),
                "sl":        round(cur_price * (1 + STOP_LOSS)),
                "tp1":       round(cur_price * (1 + TP1)),
                "tp2":       round(cur_price * (1 + TP2)),
            }
            port["used"] = port.get("used", 0) + invest
            remaining   -= invest
            bought      += 1
            print(f"  🟢 [매수] {name} ({grade}) RSI:{rsi:.0f} 점수:{entry} {rc_str} → {invest:,.0f}원")
            discord(f"🟢 매수: {name} ({grade}) | {invest:,.0f}원 | RSI {rsi:.0f} | 진입점수 {entry}")
            check_stock_crash(cur_price, cur_price * 0.9, ticker)

        time.sleep(0.5)
        if bought >= slots:
            break

    if bought > 0:
        print(f"  [매수] {bought}건 완료 | 투자중 {port['used']:,}원 / {BUDGET:,}원")

# ── 메인 ──────────────────────────────────────────────────────────────
def main():
    now      = datetime.datetime.now(KST)
    mode_str = "🧪 모의투자" if MOCK else "💰 실전투자"
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 자동매매  {now.strftime('%Y%m%d %H:%M KST')}  [{mode_str}]")
    print(f"  예산 {BUDGET//10000}만원 | F등급 제외 | 최대 {MAX_POS}종목 | 통합 시스템")
    print(f"{'='*50}")

    # 매매 정지 체크
    suspend = is_trading_suspended()
    if suspend["suspended"]:
        msg = f"⛔ 자동매매 정지: {suspend['reason']}"
        print(f"  {msg}"); discord(msg); return

    if not os.path.exists(RESULTS_FILE):
        print("  ⚠️  results.json 없음"); return

    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        results = json.load(f)

    # 시장 국면
    print("\n  [국면] 시장 국면 분석 중...")
    try:
        regime      = get_market_regime()
        regime_mult = REGIME_MULT.get(regime.get("regime", ""), 0.7)
        print(f"  국면: {regime['label']} | KOSPI {regime['kospi']:,.0f} | 배율 {regime_mult}x")
    except Exception as e:
        print(f"  국면 로드 실패 ({e}) — 기본값")
        regime      = {"regime": "UNKNOWN", "label": "판단불가 ⚪", "kospi": 0}
        regime_mult = 0.7

    # 만기일
    print("\n  [만기] 만기일 방어 체크...")
    expiry_guard = load_expiry_guard()

    # 장 시간 + 시그널
    now_time       = now.time()
    is_market_open = datetime.time(9, 0) <= now_time <= datetime.time(15, 30)
    market         = results.get("market_signal", {})
    kospi_ch5      = float(market.get("kospi_ch5", 0))
    kospi_ch1      = float(market.get("kospi_ch1", 0))
    rsi_14         = float(market.get("rsi_14", 50))
    kospi_aligned  = market.get("aligned", "")

    # ── 매수 허용 조건 (signal 텍스트 대신 객관적 지표 기반) ─────────
    # KOSPI 정배열 + RSI 80 미만 + 당일 -3% 이상
    allow_buy = (
        kospi_aligned in ("정배열",)
        and rsi_14 < 80
        and kospi_ch1 > -3.0
    )

    if allow_buy and not is_market_open:
        allow_buy = False
        print(f"  ⚠️ 장 마감 후 ({now_time.strftime('%H:%M')}) — 신규 매수 중단")

    if allow_buy and kospi_ch1 <= -3.0:
        allow_buy = False
        print(f"  ⚠️ KOSPI 당일 {kospi_ch1:+.1f}% 급락 — 매수 차단")

    if kospi_ch1 <= -2.0:
        check_gap_down(
            float(market.get("kospi_close", 0)) * (1 + kospi_ch1 / 100),
            float(market.get("kospi_close", 0)), "KOSPI"
        )
    if kospi_ch1 <= -8.0:
        check_circuit_breaker(kospi_ch1 / 100)

    print(f"  RSI {rsi_14:.0f} | KOSPI5일 {kospi_ch5:+.1f}% | 당일 {kospi_ch1:+.1f}% | {kospi_aligned}")
    print(f"  매수: {'✅' if allow_buy else '❌'} | 만기: {expiry_guard.get('note','')}")

    print("\n  KIS 토큰 발급 중...")
    try:
        token = get_token()
        print("  ✅ 토큰 발급 완료")
    except Exception as e:
        print(f"  ⚠️ KIS 연결 실패: {e}"); return

    data = load_positions()
    data["_kospi_ch5"] = kospi_ch5
    data["_kospi_ch1"] = kospi_ch1
    data["_regime"]    = regime.get("regime", "UNKNOWN")

    stocks = results.get("results", [])

    # 매도 체크 (항상)
    print("\n  [청산 체크]")
    unified_sells(token, data, stocks, now)

    # 매수
    if allow_buy:
        unified_buys(token, data, stocks, now, allow_buy,
                     regime_mult, kospi_ch5, expiry_guard)
    else:
        print("\n  매수 시그널 없음 — 청산 체크만 완료")

    save_positions(data)
    print(f"\n  💾 positions.json 저장 완료")

    if datetime.time(15, 30) <= now_time <= datetime.time(16, 0):
        _send_daily_report(data)

    print(f"\n✅ 자동매매 완료!")


def _send_daily_report(data: dict):
    """장 마감 후 일일 리포트"""
    lines   = ["📊 **StockPilot KR — 일일 리포트**"]
    history = data.get("trade_history", [])
    today   = datetime.datetime.now(KST).strftime("%Y%m%d")
    trades  = [h for h in history if h.get("sell_date") == today]

    if trades:
        lines.append(f"\n**오늘 {len(trades)}건**")
        for t in trades:
            sign = "✅" if t["pnl"] > 0 else "🔴"
            lines.append(f"  {sign} {t['name']} {t['pnl_pct']:+.1f}% {t['pnl']:+,}원 ({t['reason']})")

    positions = data.get("portfolio", {}).get("positions", {})
    if positions:
        lines.append(f"\n**보유 {len(positions)}종목**")
        for ticker, p in positions.items():
            try:
                days = (datetime.datetime.now(KST).date() -
                        datetime.date.fromisoformat(p.get("buy_date", ""))).days
            except:
                days = 0
            lines.append(f"  {p.get('grade','')}등급 {p['name']} | {p['buy_price']:,}원 | {days}일")
    else:
        lines.append("\n**보유 없음**")

    if history:
        total = sum(h["pnl"] for h in history)
        wins  = sum(1 for h in history if h["pnl"] > 0)
        lines.append(f"\n**누적** {len(history)}건 | 승률 {wins/len(history)*100:.0f}% | {total:+,}원")

    discord("\n".join(lines))


if __name__ == "__main__":
    main()
