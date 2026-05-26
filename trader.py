#!/usr/bin/env python3
"""
StockPilot KR — 자동매매 (trader.py) [장투 전용 버전]
장투 포트폴리오: 500만원 / A·B등급 / 시간손절 14일

[단타 폐기 사유]
- ATR 높은 종목에 단타 적용 시 TP가 10%+로 뛰어 사실상 중기 포지션화
- 장투에 집중하고 종목 필터를 A+B등급으로 넓히는 방식으로 전환

[기존 단타 포지션 처리]
- LG씨엔에스·알테오젠 등 기존 short 포지션은 청산될 때까지 process_sells 유지
- 신규 단타 매수는 영구 차단

[통합 모듈]
- market_regime : 시장 국면(BULL/SIDEWAYS/BEAR) → 익절/손절/포지션배율 동적 조정
- defense       : 갭다운/연속손실/서킷브레이커/블랙리스트 방어
- expiry.py     : 옵션만기일 guard → 매수 제한/익절 우선 모드
- analytics     : ATR 기반 손절/익절 자동조정 + 매수 점수제 + 베타/등급 포지션 사이징
"""
import os, json, time, datetime, requests
from zoneinfo import ZoneInfo

from market_regime import get_market_regime
from defense import (
    can_buy as defense_can_buy,
    is_trading_suspended,
    record_trade_result,
    check_stock_crash,
    check_gap_down,
    check_circuit_breaker,
    add_to_watchlist,
    get_watchlist,
    remove_from_watchlist,
    check_market_adjusted_stop,
)
from analytics import analyze_before_buy

# ── 장투 설정 ─────────────────────────────────────────────────────────
LONG_BUDGET        = 5_000_000        # 장투 총 예산 500만
LONG_PER_STOCK     = LONG_BUDGET // 5 # 종목당 기준 100만 (analytics base_capital)
LONG_STOP_LOSS     = -0.10
LONG_TIME_STOP     = 14
LONG_MAX_POS       = 5                # 최대 5종목
LONG_GRADE_FILTER  = {"A", "B"}       # A·B등급만 장투 허용
LONG_PARTIAL_SELLS = [
    (0.10, 0.40),   # 1차: +10% → 40% 매도
    (0.18, 0.35),   # 2차: +18% → 35% 매도
    (0.25, 1.00),   # 3차: +25% → 전량 매도
]

# ── 레거시 단타 설정 (기존 포지션 청산 관리용, 신규매수 없음) ────────
LEGACY_SHORT_TIME_STOP  = 3           # 기존 단타 시간손절 유지
LEGACY_SHORT_MAX_SL     = -0.07       # 기존 단타 손절 한도
SHORT_PARTIAL_SELLS = [               # process_sells에서 기존 포지션 관리용
    (0.05, 0.33),
    (0.08, 0.50),
    (0.10, 1.00),
]

# ── 공통 설정 ─────────────────────────────────────────────────────────
BUY_SIGNALS    = {"강한 매수", "매수 우위"}
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

def get_price(token, ticker, retries: int = 3):
    """
    현재가 조회. 실패 시 최대 retries회 재시도.
    3회 모두 실패 시 Discord 알림 후 0 반환.
    """
    for attempt in range(retries):
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
            cur   = int(d.get("stck_prpr", 0))
            open_ = int(d.get("stck_oprc", 0))
            high  = int(d.get("stck_hgpr", 0))
            low   = int(d.get("stck_lwpr", 0))
            prev  = int(d.get("stck_sdpr", 0))
            if cur > 0:
                return cur, open_, high, low, prev
            # cur==0 이면 재시도
            if attempt < retries - 1:
                time.sleep(2)
        except Exception as e:
            if attempt < retries - 1:
                print(f"    현재가 조회 재시도 {attempt+1}/{retries} ({ticker}): {e}")
                time.sleep(2)
            else:
                msg = f"⚠️ 현재가 조회 {retries}회 실패 [{ticker}] — 손절/익절 판단 불가"
                print(f"    {msg}")
                discord(msg)
    return 0, 0, 0, 0, 0

def is_good_candle(cur, open_, high, low):
    """
    매수 적합한 캔들 판단
    ① 상승 캔들: 현재가 >= 시가
    ② 반등 캔들: 저점 대비 20% 이상 반등
    """
    if cur <= 0 or open_ <= 0:
        return True  # 데이터 없으면 통과
    if cur >= open_:
        return True  # 상승 캔들
    rng = high - low
    if rng > 0 and (cur - low) / rng >= 0.2:
        return True  # 저점 대비 20% 이상 반등
    return False

def order(token, ticker, qty, side):
    if MOCK:
        return True, "모의주문 처리"
    # ── 실전 전환 시 주의 ──────────────────────────────────────────
    # 모의투자 계좌(VTTC) vs 실전 계좌(TTTC) 자동 분기
    # KIS_ACCOUNT_TYPE=mock → 모의투자 API 사용
    # KIS_ACCOUNT_TYPE=real (기본값) → 실전 API 사용
    account_type = os.environ.get("KIS_ACCOUNT_TYPE", "real").lower()
    if side == "buy":
        tr_id = "VTTC0802U" if account_type == "mock" else "TTTC0802U"
    else:
        tr_id = "VTTC0801U" if account_type == "mock" else "TTTC0801U"
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

# ── positions.json ────────────────────────────────────────────────────
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 구버전 마이그레이션
        if "positions" in data and "long" not in data:
            print("  📦 구버전 → 장투 전용 마이그레이션")
            data = {
                "long":  {"budget": LONG_BUDGET, "used": data.get("used", 0),
                          "positions": data.get("positions", {})},
                "short": {"budget": 0, "used": 0, "positions": {}},
                "trade_history": data.get("trade_history", [])
            }
        # 예산 자동 동기화 (LONG_BUDGET이 변경됐을 때)
        if data.get("long", {}).get("budget", 0) != LONG_BUDGET:
            data.setdefault("long", {})["budget"] = LONG_BUDGET
        return data
    return {
        "long":  {"budget": LONG_BUDGET, "used": 0, "positions": {}},
        "short": {"budget": 0,           "used": 0, "positions": {}},
        "trade_history": []
    }

def save_positions(data):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── 거래내역 기록 ─────────────────────────────────────────────────────
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

# ── Discord ───────────────────────────────────────────────────────────
def discord(msg):
    if not DISCORD_WH:
        return
    try:
        requests.post(DISCORD_WH, json={"content": ("[모의] " if MOCK else "[실전] ") + msg}, timeout=10)
    except Exception:
        pass

# ── 만기일 guard 로드 ─────────────────────────────────────────────────
def load_expiry_guard() -> dict:
    """
    expiry.py가 저장한 expiry_result.json에서 guard 읽기.
    파일 없으면 기본값(제한 없음) 반환.
    """
    default = {
        "allow_new_longterm": True,
        "allow_new_daytrend": True,
        "sell_priority":      False,
        "score_penalty":      0,
        "pos_mult_adj":       0.0,
        "note":               "만기일 데이터 없음 — 제한 없음",
    }
    try:
        if os.path.exists(EXPIRY_FILE):
            with open(EXPIRY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            guard = data.get("guard", default)
            d_day = data.get("d_day", 99)
            print(f"  만기일: {data.get('expiry_date','-')} (D-{d_day}) | {guard['note']}")
            return guard
    except Exception as e:
        print(f"  만기일 파일 로드 실패: {e}")
    return default

# ── 국면별 익절/손절 파라미터 오버라이드 ─────────────────────────────
def build_regime_params(regime: dict) -> dict:
    """
    market_regime의 국면에 따라 장투 익절/손절/포지션배율을 동적으로 결정.
    단타는 폐기됐으나 레거시 포지션 청산 관리를 위해 short 파라미터 유지.
    """
    params = regime.get("params", {})

    long_tp_pcts  = params.get("long_tp",  [0.10, 0.18, 0.25])
    short_tp_pcts = params.get("short_tp", [0.05, 0.08, 0.10])   # 레거시 고정
    long_sl       = params.get("long_sl",  LONG_STOP_LOSS)
    short_sl      = params.get("short_sl", LEGACY_SHORT_MAX_SL)   # 레거시 고정
    pos_mult      = regime.get("effective_position_multiplier", 1.0)

    long_sells  = [(pct, ratio) for pct, (_, ratio) in zip(long_tp_pcts,  LONG_PARTIAL_SELLS)]
    short_sells = [(pct, ratio) for pct, (_, ratio) in zip(short_tp_pcts, SHORT_PARTIAL_SELLS)]

    return {
        "regime_label":    regime.get("label", "?"),
        "long_sells":      long_sells,
        "short_sells":     short_sells,
        "long_sells_pct":  long_tp_pcts,
        "short_sells_pct": short_tp_pcts,
        "long_sl":         long_sl,
        "short_sl":        short_sl,
        "pos_mult":        pos_mult,
        "allow_longterm":  regime.get("longterm_new_buy_ok", True),
        "allow_daytrend":  False,   # 단타 영구 차단
    }

# ── 포트폴리오 매도 처리 ──────────────────────────────────────────────
def process_sells(token, data, ptype, partial_sells, stop_loss, time_stop, now):
    port      = data[ptype]
    positions = port["positions"]
    print(f"\n  [{ptype} 보유종목 체크] {len(positions)}개")

    for ticker, p in list(positions.items()):
        # ── 날짜 기반 보유일 계산 (현재가 조회 전) ────────────────────
        try:
            buy_dt    = datetime.datetime.strptime(p.get("buy_date", "19000101"), "%Y%m%d")
            days_held = (now.replace(tzinfo=None) - buy_dt).days
        except Exception:
            days_held = 0

        cur_price, open_, high, low, prev_close = get_price(token, ticker)
        if cur_price == 0:
            # 현재가 조회 실패 — 시간손절 초과 여부만 Discord 알림
            if days_held >= time_stop:
                msg = (f"⚠️ **[{ptype}] 현재가 조회 실패 + 시간손절 {days_held}일 초과**\n"
                       f"   {p['name']}({ticker}) — 수동 확인 후 손절하세요!")
                print(f"    {msg}"); discord(msg)
            else:
                print(f"    {p['name']} — 현재가 조회 실패 (보유 {days_held}일), 스킵")
            continue

        buy_price     = p["buy_price"]
        remaining_qty = p.get("remaining_qty", p["qty"])
        sold_stage    = p.get("sold_stage", 0)
        pnl           = (cur_price - buy_price) / buy_price
        pnl_str       = f"{pnl*100:+.1f}%"

        # ── 포지션별 ATR 조정 sl/tp 적용 (없으면 기본값) ─────────────
        sl          = p.get("sl", stop_loss)
        tp_levels   = p.get("tp", [ps[0] for ps in partial_sells])
        sell_ratios = [ps[1] for ps in partial_sells]
        dyn_sells   = list(zip(tp_levels, sell_ratios))

        # ── [신규] 개별 종목 급락 감지 (전일 종가 기준) ──────────────
        if prev_close > 0:
            crash = check_stock_crash(ticker, cur_price, prev_close)
            if crash["hard_crash"]:
                ok, msg = order(token, ticker, remaining_qty, "sell")
                if ok:
                    profit = (cur_price - buy_price) * remaining_qty
                    log_trade(data, ticker, p, cur_price, remaining_qty, "급락손절", now, ptype)
                    record_trade_result(is_loss=True, amount=profit, ticker=ticker)
                    port["used"] = max(0, port["used"] - p.get("amount", 0))
                    del positions[ticker]
                    log = f"🆘 **[{ptype}] 급락손절** {p['name']} {crash['change_pct']:+.1%} | {profit:+,}원"
                    print(f"    ✅ {log}"); discord(log)
                else:
                    print(f"    ❌ 급락손절 실패 {p['name']}: {msg}")
                time.sleep(0.3)
                continue

        # ── 손절 (시장 하락 시 우량종목 완화 적용) ───────────────────
        if pnl <= sl:
            stop_check = check_market_adjusted_stop(
                pnl       = pnl,
                base_sl   = sl,
                grade     = p.get("grade", "C"),
                kospi_ch5 = float(data.get("_kospi_ch5", 0)),
                regime    = data.get("_regime", "UNKNOWN"),
            )
            if not stop_check["should_stop"]:
                # 손절 유예 — 손절선 완화값으로 업데이트
                p["sl"] = stop_check.get("relaxed_sl", sl)
                print(f"    ⏸ {p['name']} 손절 유예: {stop_check['reason']}")
                continue
            ok, msg = order(token, ticker, remaining_qty, "sell")
            if ok:
                profit = (cur_price - buy_price) * remaining_qty
                log_trade(data, ticker, p, cur_price, remaining_qty, "손절", now, ptype)
                record_trade_result(is_loss=True, amount=profit, ticker=ticker)
                # A/B 등급이고 시장 하락이면 watchlist 등록 (재매수 후보)
                if p.get("grade") in ("A", "B") and stop_check.get("market_driven"):
                    add_to_watchlist(ticker, p["name"], cur_price,
                                     p.get("grade", "B"), "시장하락손절")
                    print(f"    📋 {p['name']} Watchlist 등록 (재매수 후보)")
                port["used"] = max(0, port["used"] - p.get("amount", 0))
                del positions[ticker]
                log = f"📤 **[{ptype}] 손절** {p['name']} ({pnl_str} / 설정:{sl*100:.1f}%) | {profit:+,}원"
                print(f"    ✅ {log}"); discord(log)
            else:
                print(f"    ❌ 손절 실패 {p['name']}: {msg}")
            time.sleep(0.3); continue

        # ── 시간손절 ──────────────────────────────────────────────────
        if days_held >= time_stop and pnl < 0:
            ok, msg = order(token, ticker, remaining_qty, "sell")
            if ok:
                profit = (cur_price - buy_price) * remaining_qty
                log_trade(data, ticker, p, cur_price, remaining_qty, f"시간손절({days_held}일)", now, ptype)
                record_trade_result(is_loss=True, amount=profit, ticker=ticker)
                port["used"] = max(0, port["used"] - p.get("amount", 0))
                del positions[ticker]
                log = f"📤 **[{ptype}] 시간손절** {p['name']} {days_held}일 ({pnl_str}) | {profit:+,}원"
                print(f"    ✅ {log}"); discord(log)
            else:
                print(f"    ❌ 시간손절 실패 {p['name']}: {msg}")
            time.sleep(0.3); continue

        # ── 분할 익절 (ATR 조정 tp 적용) ─────────────────────────────
        partial_done = False
        for idx, (target_pnl, sell_ratio) in enumerate(dyn_sells):
            if sold_stage > idx: continue
            if pnl < target_pnl: break
            is_last  = (idx == len(partial_sells) - 1)
            sell_qty = remaining_qty if is_last else max(1, min(int(p["qty"] * sell_ratio), remaining_qty))
            if sell_qty < 1:
                sold_stage += 1; continue

            ok, msg = order(token, ticker, sell_qty, "sell")
            if ok:
                profit        = (cur_price - buy_price) * sell_qty
                sold_cost     = round(buy_price * sell_qty)
                remaining_qty -= sell_qty
                sold_stage     = idx + 1
                p["remaining_qty"] = remaining_qty
                p["sold_stage"]    = sold_stage
                p["amount"]        = max(0, p.get("amount", 0) - sold_cost)

                if remaining_qty <= 0 or is_last:
                    # ── BUG FIX: 잔여amount만 차감 (중복 차감 방지) ──
                    port["used"] = max(0, port["used"] - p.get("amount", 0))
                    log_trade(data, ticker, p, cur_price, sell_qty,
                              f"분할매도{idx+1}차(+{int(target_pnl*100)}%)", now, ptype)
                    record_trade_result(is_loss=False, amount=profit, ticker=ticker)  # ← 신규
                    del positions[ticker]
                else:
                    port["used"] = max(0, port["used"] - sold_cost)
                    log_trade(data, ticker, p, cur_price, sell_qty,
                              f"분할매도{idx+1}차(+{int(target_pnl*100)}%)", now, ptype)
                    # 마지막 분할매도 아닌 경우 수익 기록 (연패 리셋 목적)
                    if profit > 0:
                        record_trade_result(is_loss=False, amount=profit, ticker=ticker)

                log = (f"📤 **[{ptype}] 분할{idx+1}차** +{int(target_pnl*100)}% "
                       f"{p['name']} | {sell_qty}주 {profit:+,}원 | 잔여{remaining_qty}주")
                print(f"    ✅ {log}"); discord(log)
                partial_done = True
            else:
                print(f"    ❌ 분할매도 실패 {p['name']}: {msg}")
            time.sleep(0.3); break

        if ticker in positions and not partial_done:
            sold_stage = p.get("sold_stage", 0)
            stage_info = f" 분할{sold_stage}차완료" if sold_stage > 0 else ""

            # ── 트레일링 스탑: 1차 익절 완료 후 원금 보장선으로 손절 상향 ──
            if sold_stage >= 1 and pnl > 0:
                trailing_floor = buy_price  # 원금 보장
                trailing_pct   = p.get("trailing_stop_pct", 0.03)
                trailing_price = cur_price * (1 - trailing_pct)
                new_sl_price   = max(trailing_floor, trailing_price)
                new_sl_pct     = (new_sl_price - buy_price) / buy_price
                old_sl         = p.get("sl", stop_loss)
                if new_sl_pct > old_sl:   # 스탑은 올라가기만 함
                    p["sl"] = round(new_sl_pct, 3)

            # ── 보유 손실 경고 Discord (단타 2일 이상 손실 중) ────────────
            if ptype == "short" and days_held >= 2 and pnl < -0.03:
                days_to_stop = time_stop - days_held
                if days_to_stop <= 1:
                    msg = (f"⚠️ **[단타] 손절 임박** {p['name']} "
                           f"{pnl_str} | 보유 {days_held}일 | "
                           f"내일 시간손절 예정")
                    discord(msg)

            # ── 장투 2차 매수 체크 ────────────────────────────────────
            if ptype == "long" and not p.get("stage2_done", True):
                s1_price  = p.get("stage1_price", buy_price)
                s2_budget = p.get("stage2_budget", 0)
                trigger   = s1_price * 0.95

                if cur_price <= trigger and s2_budget >= cur_price:
                    cur2, open2, high2, low2, _ = get_price(token, ticker)
                    # ── BUG FIX: 데드코드 제거 → 실제 캔들로만 확인 ──
                    if is_good_candle(cur2, open2, high2, low2) and cur2 > 0:
                        qty2 = s2_budget // cur2
                        if qty2 >= 1:
                            ok2, _ = order(token, ticker, qty2, "buy")
                            if ok2:
                                amt2 = cur2 * qty2
                                # ── BUG FIX: remaining_qty 먼저 계산, qty 나중 업데이트 ──
                                prev_remaining = p.get("remaining_qty", p["qty"])
                                prev_qty       = p["qty"]
                                total_qty      = prev_qty + qty2
                                avg_price      = round((buy_price * prev_qty + cur2 * qty2) / total_qty)
                                p["qty"]           = total_qty
                                p["remaining_qty"] = prev_remaining + qty2
                                p["buy_price"]     = avg_price
                                p["amount"]        = p.get("amount", 0) + amt2
                                p["stage2_done"]   = True
                                port["used"]      += amt2
                                log = (f"📥 **[장투] 2차매수** {p['name']} ({ticker})\n"
                                       f"   {cur2:,}원 × {qty2}주 = {amt2:,}원\n"
                                       f"   평균단가 {avg_price:,}원 (1차 {s1_price:,}원 대비 -{(1-cur2/s1_price)*100:.1f}%)")
                                print(f"    ✅ {log}"); discord(log)
                                time.sleep(1.0)
                    else:
                        print(f"    {p['name']} 2차매수 대기 — 반등 미확인 ({cur_price:,}원 / 트리거 {trigger:,}원)")

            stage_info2 = (" [2차완료]" if p.get("stage2_done") else
                           " [2차대기]" if not p.get("stage2_done", True) and ptype == "long" else "")
            print(f"    {p['name']} ({ticker}) — {cur_price:,}원 ({pnl_str}){stage_info}{stage_info2} 유지")

# ── 포트폴리오 매수 처리 ──────────────────────────────────────────────
def process_buys(token, data, stocks, ptype, max_pos, budget, partial_sells,
                 grade_filter, rsi_min, rsi_max, now,
                 regime_params: dict, expiry_guard: dict,
                 allow_override: bool = True,
                 max_new_today: int = 3,
                 pos_mult_override: float = None):
    """
    pos_mult_override: 기회 포착 모드 시 배율 강제 지정 (None이면 국면 배율 사용)
    """
    port        = data[ptype]
    positions   = port["positions"]
    all_tickers = set(data["long"]["positions"]) | set(data["short"]["positions"])

    # ── allow_override 체크 (당일 등락률/시그널로 외부에서 차단) ─────
    if not allow_override:
        print(f"  [{ptype}] 외부 조건으로 신규매수 차단")
        return

    # ── 국면/만기일 기반 신규매수 허용 여부 ───────────────────────────
    if ptype == "long":
        if not regime_params["allow_longterm"]:
            print(f"  [{ptype}] 시장국면 {regime_params['regime_label']} — 장투 신규매수 금지")
            return
        if not expiry_guard["allow_new_longterm"]:
            print(f"  [{ptype}] {expiry_guard['note']} — 장투 신규매수 금지")
            return
    else:
        if not regime_params["allow_daytrend"]:
            print(f"  [{ptype}] 시장국면 {regime_params['regime_label']} — 단타 신규매수 금지")
            return
        if not expiry_guard["allow_new_daytrend"]:
            print(f"  [{ptype}] {expiry_guard['note']} — 단타 신규매수 금지")
            return

    slots            = max_pos - len(positions)
    pos_mult         = pos_mult_override if pos_mult_override is not None else \
                       max(0.0, min(1.3, regime_params["pos_mult"] + expiry_guard["pos_mult_adj"]))
    effective_budget = int(budget * pos_mult)
    remaining_budget = effective_budget - port["used"]

    # ── ③ 하루 최대 신규매수 제한 적용 ───────────────────────────────
    today_str  = now.strftime("%Y%m%d")
    bought_today = sum(
        1 for p in positions.values()
        if p.get("buy_date") == today_str
    )
    slots = min(slots, max(0, max_new_today - bought_today))
    if slots <= 0 and bought_today >= max_new_today:
        print(f"  [{ptype}] 당일 최대 신규매수 {max_new_today}종목 도달 — 추가 매수 없음")
        return

    if slots <= 0:
        print(f"  [{ptype}] 최대 종목 도달 — 매수 스킵"); return
    if remaining_budget < 10_000:
        print(f"  [{ptype}] 예산 부족 ({remaining_budget:,}원 / 포지션배율 {pos_mult:.1f}x) — 매수 스킵"); return

    # 급등장(KOSPI 5일 +5% 이상) → vol_trend 기준 완화
    # 급등 후 20일 평균이 높아져서 좋은 종목도 마이너스로 찍히는 왜곡 방지
    kospi_ch5 = float(data.get("_kospi_ch5", 0))
    vol_trend_min = -20.0 if kospi_ch5 >= 5.0 else 0.0

    candidates = [
        s for s in stocks
        if s.get("grade") in grade_filter
        and s["ticker"] not in all_tickers
        and rsi_min <= float(s.get("rsi", 0)) <= rsi_max
        and float(s.get("vol_trend", -999)) >= vol_trend_min
        and s.get("macd_bull") is not False
    ]

    surge_str = f" 🚀 급등장(vol≥{vol_trend_min:.0f}%)" if kospi_ch5 >= 5.0 else f" vol≥{vol_trend_min:.0f}%"
    print(f"\n  [{ptype} 매수 후보] {len(candidates)}개 "
          f"(등급:{grade_filter} RSI:{rsi_min}~{rsi_max} 배율:{pos_mult:.1f}x{surge_str})")
    if not candidates:
        print(f"  [{ptype}] 신규 후보 없음"); return

    max_buys = min(slots, len(candidates))
    if max_buys == 0: return
    per_stock = remaining_budget // max_buys
    bought    = 0

    # ── BUG FIX: 전체 candidates 순회 (슬라이스 버그 수정) ────────────
    for stock in candidates:
        if bought >= max_buys:
            break

        ticker = stock["ticker"]
        name   = stock["name"]

        # ── [신규] 방어 모듈: 블랙리스트/갭다운/정지 개별 체크 ─────────
        d_check = defense_can_buy(ticker, ptype)
        if not d_check["allowed"]:
            print(f"    {name} — 방어차단: {d_check['reason']}, 스킵")
            continue

        # ── Watchlist 종목이면 최소 점수 1점 완화 ───────────────────────
        watchlist = get_watchlist()
        is_watchlist = ticker in watchlist
        score_bonus  = 1 if is_watchlist else 0
        if is_watchlist:
            print(f"    📋 {name} — Watchlist 재매수 후보 (이전 손절가 {watchlist[ticker]['sold_price']:,}원)")

        cur_price, open_, high, low, prev_close = get_price(token, ticker)
        if cur_price == 0:
            print(f"    {name} — 현재가 조회 실패, 스킵"); continue

        qty = per_stock // cur_price
        if qty < 1:
            print(f"    {name} ({ticker}) — {cur_price:,}원 수량 부족, 다음 후보로")
            # ── BUG FIX: continue로 다음 후보 시도 (buy_count 조작 제거) ──
            continue

        # ── [통합] analytics: ATR/점수/베타/포지션사이징 ──────────────
        analysis = analyze_before_buy(
            token        = token,
            stock        = stock,
            trade_type   = ptype,
            regime_sl    = regime_params["long_sl"] if ptype == "long" else regime_params["short_sl"],
            regime_tp    = regime_params["long_sells_pct"] if ptype == "long" else regime_params["short_sells_pct"],
            base_capital = LONG_PER_STOCK if ptype == "long" else 500_000,  # 종목당 100만 기준
            effective_mult = pos_mult,
            min_score    = max(1, (4 if ptype == "short" else 5) - score_bonus),
        )
        if not analysis["ok"]:
            print(f"    {name} — 분석 스킵: {analysis['reason']}")
            continue

        # ── 반등 진입 시 추가 조정 ────────────────────────────────────
        # 기회포착모드(ADR 80%+)이면 반등감지 축소 미적용 (충돌 방지)
        is_reversal_entry = (
            data.get("_is_reversal", False) and
            ptype == "short" and
            not data.get("_opportunity_mode", False)
        )
        if is_reversal_entry:
            analysis["sizing"]["total"]  = analysis["sizing"]["total"] // 2
            analysis["sizing"]["first"]  = analysis["sizing"]["first"] // 2
            analysis["sizing"]["second"] = analysis["sizing"]["second"] // 2
            analysis["sl"]               = max(analysis["sl"], -0.03)
            print(f"    ⚡ 반등진입 보정: 포지션 50% + 손절 {analysis['sl']*100:.0f}%")

        # ── 단타 손절 최대 -7% 제한 ──────────────────────────────────
        if ptype == "short":
            analysis["sl"] = max(analysis["sl"], -0.07)

        # ── 단타 거래량 0.8x 미만 → 스킵 ────────────────────────────
        if ptype == "short" and analysis.get("vol_ratio", 1.0) < 0.8:
            print(f"    {name} — 거래량 부족 ({analysis.get('vol_ratio',0):.1f}x < 0.8x) 스킵")
            continue

        # watchlist 재매수 확정 시 watchlist에서 제거
        if is_watchlist:
            remove_from_watchlist(ticker)
        if not analysis["ok"]:
            print(f"    {name} — 분석 스킵: {analysis['reason']}")
            continue

        # analytics가 계산한 포지션 금액 사용 (남은 예산 초과 방지)
        per_stock_actual = min(analysis["sizing"]["total"], remaining_budget)
        if per_stock_actual < cur_price:
            print(f"    {name} — 포지션 금액({per_stock_actual:,}원) < 현재가({cur_price:,}원), 스킵")
            continue

        # ── 캔들 체크 ─────────────────────────────────────────────────
        if not is_good_candle(cur_price, open_, high, low):
            print(f"    {name} — 하락캔들 진입 보류 (현재:{cur_price:,} 시가:{open_:,})")
            continue

        # ── 장투: 분할 매수 (점수 기반 1차 비중) ─────────────────────
        if ptype == "long":
            first_ratio = analysis["sizing"]["first_ratio"]
            qty_total   = per_stock_actual // cur_price
            qty1        = max(1, int(qty_total * first_ratio))
            amt1        = cur_price * qty1
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
                    "buy_stage": 1,
                    "stage1_price": cur_price,
                    "stage1_qty": qty1,
                    "stage2_done": False,
                    "stage2_budget": per_stock_actual - amt1,
                    # ── analytics 저장 ──
                    "sl":       analysis["sl"],
                    "tp":       analysis["tp"],
                    "score":    analysis["score"],
                    "beta":     analysis["beta"],
                    "atr_pct":  analysis["atr_pct"],
                }
                port["used"] += amt1
                bought += 1
                tp_str = [f"{t*100:.1f}%" for t in analysis["tp"]]
                log = (f"📥 **[장투] 1차매수** {name} ({ticker}) {stock.get('grade','')}등급\n"
                       f"   {cur_price:,}원 × {qty1}주 = {amt1:,}원 ({int(first_ratio*100)}%)\n"
                       f"   점수:{analysis['score']}점 β:{analysis['beta']:.1f} ATR:{analysis['atr_pct']*100:.1f}%\n"
                       f"   손절:{analysis['sl']*100:.1f}% 익절:{tp_str}\n"
                       f"   국면:{regime_params['regime_label']} | 2차: 1차 대비 -5%+반등 시")
                print(f"    ✅ {log}"); discord(log)
            else:
                print(f"    ❌ 매수 실패 {name}: {msg}")
            time.sleep(1.0)
            continue

        # ── 단타: 단번 매수 ──────────────────────────────────────────
        qty           = per_stock_actual // cur_price
        if qty < 1:
            print(f"    {name} — 수량 부족, 스킵"); continue
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
                "buy_date": now.strftime("%Y%m%d"),
                # ── analytics 저장 ──
                "sl":      analysis["sl"],
                "tp":      analysis["tp"],
                "score":   analysis["score"],
                "beta":    analysis["beta"],
                "atr_pct": analysis["atr_pct"],
            }
            port["used"] += actual_amount
            bought += 1
            tp_str2 = [f"{t*100:.1f}%" for t in analysis["tp"]]
            log = (f"📥 **[{ptype}] 매수** {name} ({ticker}) {stock.get('grade','')}등급\n"
                   f"   {cur_price:,}원 × {qty}주 = {actual_amount:,}원\n"
                   f"   점수:{analysis['score']}점 β:{analysis['beta']:.1f} ATR:{analysis['atr_pct']*100:.1f}%\n"
                   f"   손절:{analysis['sl']*100:.1f}% 익절:{tp_str2}\n"
                   f"   국면:{regime_params['regime_label']}")
            print(f"    ✅ {log}"); discord(log)
        else:
            print(f"    ❌ 매수 실패 {name}: {msg}")
        time.sleep(1.0)

    if bought > 0:
        print(f"  [{ptype}] 매수 {bought}건 | 사용 {port['used']:,}원 / {effective_budget:,}원 (배율 {pos_mult:.1f}x)")

# ── 메인 ──────────────────────────────────────────────────────────────
def main():
    now      = datetime.datetime.now(KST)
    mode_str = "🧪 모의투자" if MOCK else "💰 실전투자"
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 자동매매  {now.strftime('%Y%m%d %H:%M KST')}  [{mode_str}]")
    print(f"  장투 {LONG_BUDGET//10000}만원 | A·B등급 | 최대 {LONG_MAX_POS}종목 [단타 폐기]")
    print(f"{'='*50}")

    # ── [신규] 매매 정지 여부 최우선 확인 ───────────────────────────────
    suspend = is_trading_suspended()
    if suspend["suspended"]:
        msg = f"⛔ 자동매매 정지 중: {suspend['reason']}"
        if suspend["resume_at"]:
            msg += f" | 재개 예정: {suspend['resume_at']}"
        print(f"  {msg}")
        discord(msg)
        return

    if not os.path.exists(RESULTS_FILE):
        print("  ⚠️  results.json 없음"); return

    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        results = json.load(f)

    # ── [신규] 시장 국면 로드 ────────────────────────────────────────
    print("\n  [국면] 시장 국면 분석 중...")
    try:
        regime = get_market_regime()
        print(f"  국면: {regime['label']} | KOSPI {regime['kospi']:,.0f} "
              f"| 포지션배율 {regime['effective_position_multiplier']}x")
    except Exception as e:
        print(f"  시장 국면 로드 실패 ({e}) — 기본값 사용")
        regime = {"regime": "UNKNOWN", "label": "판단불가 ⚪",
                  "effective_position_multiplier": 0.7,
                  "longterm_new_buy_ok": True, "daytrend_new_buy_ok": True,
                  "params": {"long_tp": [0.10, 0.18, 0.25], "short_tp": [0.07, 0.10, 0.13],
                             "long_sl": -0.10, "short_sl": -0.05}}
    regime_params = build_regime_params(regime)

    # ── [신규] 만기일 guard 로드 ────────────────────────────────────
    print("\n  [만기] 만기일 방어 체크...")
    expiry_guard = load_expiry_guard()

    # ── 장 시간 체크 (BUG FIX: 상단 import 활용, 내부 재선언 제거) ───
    now_time       = now.time()
    is_market_open = datetime.time(9, 0) <= now_time <= datetime.time(15, 30)

    market     = results.get("market_signal", {})
    signal_raw = market.get("final_signal", results.get("signal", ""))
    rsi_14     = float(market.get("rsi_14", 50))
    kospi_ch5  = float(market.get("kospi_ch5", 0))
    kospi_ch1  = float(market.get("kospi_ch1", 0))   # ① 당일 등락률

    allow_buy = any(s in signal_raw for s in BUY_SIGNALS)

    if allow_buy and not is_market_open:
        allow_buy = False
        print(f"  ⚠️ 장 마감 후 ({now_time.strftime('%H:%M')}) — 신규 매수 중단")

    if allow_buy and kospi_ch5 < 0:
        # ── 반등 감지: ch5 마이너스여도 단기 강한 반등이면 허용 ──────────
        # 조건: 당일 +2% 이상 + 2일 수익률 +1% 이상 = 반등 초기 진입 허용
        # 단, 단타만 허용 (장투는 ch5 양수 확인 필요)
        kospi_ch2 = float(market.get("kospi_ch2", 0))
        is_reversal = kospi_ch1 >= 2.0 and kospi_ch2 >= 1.0
        if is_reversal:
            print(f"  📈 반등 감지 — ch5:{kospi_ch5:+.1f}% 이나 당일:{kospi_ch1:+.1f}% 2일:{kospi_ch2:+.1f}%")
            print(f"     장투 차단 유지 / 단타 A·B등급만 소규모 허용")
            # allow_buy는 False 유지 (장투 차단)
            # allow_short_today는 아래서 별도 처리
        else:
            allow_buy = False
            print(f"  ⚠️ KOSPI 5일 하락 중 ({kospi_ch5:+.1f}%) — 신규 매수 중단")

    # ── ① KOSPI 당일 등락률 체크 ─────────────────────────────────────
    # 당일 -3% 이상 급락 시 전체 매수 차단
    if allow_buy and kospi_ch1 <= -3.0:
        allow_buy = False
        print(f"  ⚠️ KOSPI 당일 {kospi_ch1:+.1f}% 급락 — 전체 매수 차단")

    # ── 만기일 익절 우선 알림 ────────────────────────────────────────
    if expiry_guard.get("sell_priority"):
        print(f"  ⚠️ {expiry_guard['note']} — 익절 우선 모드")
        discord(f"⚠️ {expiry_guard['note']}")

    print(f"  시장 시그널: {signal_raw} | RSI {rsi_14:.0f} | KOSPI5일 {kospi_ch5:+.1f}% | 당일 {kospi_ch1:+.1f}%")
    print(f"  장투 매수:   {'✅' if allow_buy else '❌'} | 등급: A·B | 국면: {regime_params['regime_label']}")
    print(f"  만기:        {expiry_guard['note']}")

    print("\n  KIS 토큰 발급 중...")
    try:
        token = get_token()
        print("  ✅ 토큰 발급 완료")
    except Exception as e:
        print(f"  ⚠️ KIS 서버 연결 실패: {e}"); return

    data = load_positions()

    # ── 매도/매수 상태 data에 주입 ───────────────────────────────────
    data["_kospi_ch5"]        = kospi_ch5
    data["_kospi_ch1"]        = kospi_ch1
    data["_regime"]           = regime.get("regime", "UNKNOWN")
    data["_signal"]           = signal_raw
    data["_is_reversal"]      = False
    data["_opportunity_mode"] = False

    # ── 장투 매도 체크 ───────────────────────────────────────────────
    process_sells(token, data, "long",
                  regime_params["long_sells"],
                  regime_params["long_sl"],
                  LONG_TIME_STOP, now)

    # ── 레거시 단타 포지션 청산 관리 (신규매수 없음) ─────────────────
    if data.get("short", {}).get("positions"):
        print(f"\n  [레거시 단타 포지션 청산 관리] {len(data['short']['positions'])}개 남음")
        process_sells(token, data, "short",
                      [(0.05, 0.33), (0.08, 0.50), (0.10, 1.00)],
                      -0.07,
                      LEGACY_SHORT_TIME_STOP, now)
    else:
        print(f"\n  [단타] 레거시 포지션 없음 — 완전 종료")

    # ── Watchlist 재매수 후보 출력 ─────────────────────────────────
    watchlist = get_watchlist()
    if watchlist:
        print(f"\n  📋 재매수 Watchlist ({len(watchlist)}종목):")
        for t, info in watchlist.items():
            print(f"    {info['grade']}등급 {info['name']}({t}) | 손절가 {info['sold_price']:,}원")

    # ── 장투 매수 ────────────────────────────────────────────────────
    if not allow_buy:
        print("\n  매수 시그널 없음 — 매도 체크만 완료")
        save_positions(data); return

    stocks = results.get("results", [])

    process_buys(token, data, stocks, "long",
                 LONG_MAX_POS, LONG_BUDGET,
                 regime_params["long_sells"],
                 LONG_GRADE_FILTER, 0, 65, now,
                 regime_params, expiry_guard,
                 allow_override=allow_buy)

    save_positions(data)
    print(f"\n  💾 positions.json 저장 완료")

    # ── 장 마감 후 일일 리포트 (15:30~16:00 실행 시) ─────────────────
    if datetime.time(6, 30) <= now_time <= datetime.time(7, 0):   # 15:30~16:00 KST
        _send_daily_report(data)

    print(f"\n✅ 자동매매 완료!")


def _send_daily_report(data: dict):
    """장 마감 후 보유 종목 손익 현황 Discord 리포트"""
    lines = ["📊 **StockPilot KR — 일일 리포트**"]
    total_pnl = 0
    history   = data.get("trade_history", [])

    # 오늘 거래내역
    today = datetime.datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    today_trades = [h for h in history if h.get("sell_date") == today]
    if today_trades:
        lines.append(f"\n**오늘 거래 {len(today_trades)}건**")
        for t in today_trades:
            sign = "✅" if t["pnl"] > 0 else "❌"
            lines.append(f"  {sign} {t['name']} {t['pnl_pct']:+.1f}% {t['pnl']:+,}원 ({t['reason']})")

    # 보유 현황
    all_pos = {**data["long"]["positions"], **data["short"]["positions"]}
    if all_pos:
        lines.append(f"\n**보유 {len(all_pos)}종목**")
        for ticker, p in all_pos.items():
            ptype  = "장투" if ticker in data["long"]["positions"] else "단타"
            days   = (datetime.datetime.now() - datetime.datetime.strptime(
                      p.get("buy_date", "20260101"), "%Y%m%d")).days
            pnl_pct = 0   # 현재가 없으면 0
            lines.append(f"  [{ptype}] {p['name']} | 매수 {p['buy_price']:,}원 | {days}일째")
    else:
        lines.append("\n**보유 없음 — 현금 100%**")

    # 누적 손익
    total = sum(h["pnl"] for h in history)
    wins  = sum(1 for h in history if h["pnl"] > 0)
    lines.append(f"\n**누적** 총 {len(history)}건 | 승률 {wins/len(history)*100:.0f}% | {total:+,}원" if history else "\n**거래없음**")

    discord("\n".join(lines))

if __name__ == "__main__":
    main()
