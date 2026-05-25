#!/usr/bin/env python3
"""
analytics.py — StockPilot KR 분석 엔진
=======================================
★★ ATR 기반 손절/익절 자동조정
★★ 매수 강도 점수제 (0~10점)
★★ 국면별 익절% 동적 조정 (market_regime 연동)
★  베타 기반 포지션 사이징
★  등급 기반 포지션 사이징 (A=1.5x / B=1.0x / C=0.75x)  ← 신규
★  KOSPI 장기 사이클 반영 (market_regime 내장)

trader.py에서 import해서 사용합니다.
"""

import os, time, math, requests
from typing import Optional

try:
    import yfinance as yf
    import numpy as np
    HAS_YF = True
except ImportError:
    HAS_YF = False

BASE_URL  = "https://openapi.koreainvestment.com:9443"
APP_KEY   = os.environ.get("KIS_APP_KEY",    os.environ.get("KIS_APP_KEY_MOCK",    ""))
APP_SECRET= os.environ.get("KIS_APP_SECRET", os.environ.get("KIS_APP_SECRET_MOCK", ""))

# ── 기본 손절/익절 기준 (ATR 조정 전) ─────────────────────────────────
BASE_LONG_TP  = [0.10, 0.18, 0.25]
BASE_SHORT_TP = [0.07, 0.10, 0.13]
BASE_LONG_SL  = -0.10
BASE_SHORT_SL = -0.05
BASE_ATR_PCT  = 0.020   # ATR% 기준값 (2%)

# ── 등급별 포지션 배율 ────────────────────────────────────────────────
# 기준(B등급=1.0x) 대비 더 좋은 종목엔 더 많이, 나쁜 종목엔 적게 투자
# 실제 투자금 = base × effective_mult × beta_m × atr_m × grade_m
#
#   A (5/5 모두 충족): 1.5× — 검증된 우량주, 더 담아도 됨
#   B (4/5 충족)     : 1.0× — 기준 (변경 없음)
#   C (3/5 충족)     : 0.75× — 25% 줄여서 리스크 완화
#   D/F              : 0.50× — 사실상 매수 안 하지만 안전장치
#
GRADE_POSITION_MULT = {
    "A": 1.50,
    "B": 1.00,
    "C": 0.75,
    "D": 0.50,
    "F": 0.40,
}

# ════════════════════════════════════════════════════════════════════════
# ① KIS — 일봉 OHLCV 조회 (ATR/MA 계산용)
# ════════════════════════════════════════════════════════════════════════

def get_ohlcv(token: str, ticker: str, days: int = 20) -> list[dict]:
    """
    KIS API 일봉 OHLCV 조회.
    반환: [{"date","open","high","low","close","volume"}, ...] 최신→과거 순
    """
    try:
        import datetime
        today = datetime.date.today().strftime("%Y%m%d")
        past  = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y%m%d")
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY, "appsecret": APP_SECRET,
            "tr_id": "FHKST03010100",
        }
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
            "fid_input_date_1": past,
            "fid_input_date_2": today,
            "fid_period_div_code": "D",
            "fid_org_adj_prc": "0",
        }
        r    = requests.get(f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                            headers=headers, params=params, timeout=10)
        rows = r.json().get("output2", [])
        result = []
        for row in rows:
            try:
                result.append({
                    "date":   row.get("stck_bsop_date", ""),
                    "open":   int(row.get("stck_oprc", 0)),
                    "high":   int(row.get("stck_hgpr", 0)),
                    "low":    int(row.get("stck_lwpr", 0)),
                    "close":  int(row.get("stck_clpr", 0)),
                    "volume": int(row.get("acml_vol",  0)),
                })
            except Exception:
                continue
        return result[:days]   # 최신 N일치만
    except Exception as e:
        print(f"  [analytics] OHLCV 조회 실패 ({ticker}): {e}")
        return []

# ════════════════════════════════════════════════════════════════════════
# ② ATR 계산 + 손절/익절 자동조정
# ════════════════════════════════════════════════════════════════════════

def calc_atr(ohlcv: list[dict], period: int = 14) -> float:
    """
    Average True Range (%) 계산.
    True Range = max(H-L, |H-Cprev|, |L-Cprev|)
    반환: ATR / 평균종가 (소수점, 예: 0.022 = 2.2%)
    """
    if len(ohlcv) < period + 1:
        return BASE_ATR_PCT
    trs = []
    for i in range(len(ohlcv) - 1):
        cur  = ohlcv[i]
        prev = ohlcv[i + 1]
        if cur["high"] <= 0 or cur["low"] <= 0 or prev["close"] <= 0:
            continue
        hl  = cur["high"] - cur["low"]
        hpc = abs(cur["high"] - prev["close"])
        lpc = abs(cur["low"]  - prev["close"])
        trs.append(max(hl, hpc, lpc))
        if len(trs) >= period:
            break
    if not trs:
        return BASE_ATR_PCT
    atr_price = sum(trs) / len(trs)
    avg_close = sum(o["close"] for o in ohlcv[:period] if o["close"] > 0) / period
    return round(atr_price / avg_close, 4) if avg_close > 0 else BASE_ATR_PCT


def adjust_sl_tp(
    trade_type: str,
    atr_pct: float,
    regime_sl: float,
    regime_tp: list[float],
) -> dict:
    """
    ATR 기반으로 손절/익절 레벨 조정.

    원리:
      기준 ATR=2%일 때 손절/익절을 1배로 설정.
      ATR이 크면 → 손절 폭 넓히고 익절도 비례 상향.
      ATR이 작으면 → 손절 좁히고 익절도 소폭 낮춤.
      단, 손절은 최대 1.8배까지만 허용 (리스크 제한).

    예시:
      ATR 1% → 손절 -5% (기본 -10%의 0.5배) ← 변동성 낮으니 타이트하게
      ATR 2% → 손절 -10% (기본 그대로)
      ATR 3% → 손절 -15% (기본의 1.5배)    ← 변동성 높으니 여유 있게
      ATR 4% → 손절 -18% (최대 1.8배 제한)
    """
    ratio   = atr_pct / BASE_ATR_PCT            # ex: 0.03/0.02 = 1.5
    ratio   = max(0.4, min(1.8, ratio))         # 0.4~1.8 범위 제한
    adj_sl  = round(regime_sl * ratio, 3)
    adj_tp  = [round(tp * max(ratio, 0.8), 3) for tp in regime_tp]
    return {
        "sl":     adj_sl,
        "tp":     adj_tp,
        "atr_pct": atr_pct,
        "atr_ratio": round(ratio, 2),
        "sl_str": f"{adj_sl*100:.1f}%",
        "tp_str": [f"{t*100:.1f}%" for t in adj_tp],
    }

# ════════════════════════════════════════════════════════════════════════
# ③ 외국인 순매수 조회 (매수 점수용)
# ════════════════════════════════════════════════════════════════════════

def get_foreign_net(token: str, ticker: str) -> int:
    """
    외국인 당일 순매수 수량 반환 (양수=순매수, 음수=순매도).
    조회 실패 시 0 반환.
    """
    try:
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY, "appsecret": APP_SECRET,
            "tr_id": "FHKST01010900",
        }
        params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker}
        r    = requests.get(f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor",
                            headers=headers, params=params, timeout=8)
        rows = r.json().get("output", [])
        # rows[0] = 당일, frgn_ntby_qty = 외국인 순매수
        if rows:
            raw = str(rows[0].get("frgn_ntby_qty", "0")).replace(",", "")
            return int(raw) if raw.lstrip("-").isdigit() else 0
    except Exception:
        pass
    return 0

# ════════════════════════════════════════════════════════════════════════
# ④ 매수 강도 점수제 (0~10점)
# ════════════════════════════════════════════════════════════════════════

def calc_buy_score(
    grade: str,           # "A"/"B"/"C"
    rsi: float,
    ch5: float,           # 5일 수익률 (%)
    ch20: float,          # 20일 수익률 (%)
    foreign_net: int,     # 외국인 순매수 수량
    volume_ratio: float,  # 현재량 / 20일 평균량
    is_rising_candle: bool,  # 종가 >= 시가
    ma5_gt_ma20: bool,    # 5MA > 20MA
    rank_change: int,     # 순위 변동 (양수=상승)
) -> dict:
    """
    점수 구성 (최대 10점):
      등급       A=+2  B=+1  C=0
      RSI        <35=+3  <45=+2  <55=+1  이상=0
      거래량급증 ≥2배=+2  ≥1.5배=+1  미만=0
      외인순매수 >0=+1  ≤0=0
      상승캔들   True=+1  False=0
      골든크로스 True=+1  False=0
      5일수익률  ≥5%=-1(과열)  ≥2%=0  <0%=+1(눌림)
      순위상승   ≥5=+1  하락=-1  유지=0
    """
    score  = 0
    detail = {}

    # 등급
    g = {"A": 2, "B": 1, "C": 0}.get(grade, -1)
    score += g; detail["grade"] = g

    # RSI
    r = 3 if rsi < 35 else 2 if rsi < 45 else 1 if rsi < 55 else 0
    score += r; detail["rsi"] = r

    # 거래량
    v = 2 if volume_ratio >= 2.0 else 1 if volume_ratio >= 1.5 else 0
    score += v; detail["volume"] = v

    # 외인 순매수
    f = 1 if foreign_net > 0 else 0
    score += f; detail["foreign"] = f

    # 상승캔들
    c = 1 if is_rising_candle else 0
    score += c; detail["candle"] = c

    # 골든크로스
    m = 1 if ma5_gt_ma20 else 0
    score += m; detail["ma_cross"] = m

    # 5일 수익률 (과열/눌림)
    p = -1 if ch5 >= 5 else 1 if ch5 < 0 else 0
    score += p; detail["ch5"] = p

    # 순위 변동
    rk = 1 if rank_change >= 5 else -1 if rank_change < 0 else 0
    score += rk; detail["rank"] = rk

    score = max(0, min(10, score))

    # 진입 강도 판단
    if score >= 8:   strength = "강매수"
    elif score >= 6: strength = "매수"
    elif score >= 4: strength = "관망"
    else:            strength = "스킵"

    # 1차 매수 비중 (점수별 분할 비중)
    if score >= 8:   first_ratio = 0.60
    elif score >= 6: first_ratio = 0.45
    else:            first_ratio = 0.30

    return {
        "score":       score,
        "strength":    strength,
        "first_ratio": first_ratio,
        "detail":      detail,
    }

# ════════════════════════════════════════════════════════════════════════
# ⑤ 베타 근사치 계산 (yfinance)
# ════════════════════════════════════════════════════════════════════════

def get_beta(ticker: str, period: str = "3mo") -> float:
    """
    종목 베타 계산 (vs KOSPI).
    yfinance 사용 불가 시 1.0 반환.
    공식: β = Cov(종목수익률, KOSPI수익률) / Var(KOSPI수익률)
    """
    if not HAS_YF:
        return 1.0
    try:
        stock_ticker = f"{ticker}.KS" if ticker.isdigit() else ticker
        stock_df = yf.Ticker(stock_ticker).history(period=period)["Close"].pct_change().dropna()
        kospi_df = yf.Ticker("^KS11").history(period=period)["Close"].pct_change().dropna()

        stock_df.index = stock_df.index.tz_localize(None)
        kospi_df.index = kospi_df.index.tz_localize(None)
        aligned = stock_df.align(kospi_df, join="inner")
        s, k    = aligned[0].values, aligned[1].values

        if len(s) < 20:
            return 1.0
        cov  = float(np.cov(s, k)[0][1])
        var  = float(np.var(k))
        beta = round(cov / var, 2) if var > 0 else 1.0
        return max(0.2, min(3.0, beta))   # 0.2~3.0 범위 제한
    except Exception:
        return 1.0

# ════════════════════════════════════════════════════════════════════════
# ⑥ 포지션 사이징 (등급 + 베타 + ATR + 점수 통합)   ← 등급 배율 신규 추가
# ════════════════════════════════════════════════════════════════════════

def calc_position_size(
    base_capital: float,   # 장투 200만 / 단타 300만
    effective_mult: float, # 국면×만기일 배율
    score: int,            # 매수 점수
    grade: str = "B",      # ★ 등급 (A/B/C/D/F) — 포지션 배율 결정
    beta: float = 1.0,
    atr_pct: float = BASE_ATR_PCT,
) -> dict:
    """
    최종 투자 금액 결정.

    ★ 등급 배율 (신규):
      A등급 (5/5 모두 충족) → ×1.5  : 검증된 우량주, 50% 더 투자
      B등급 (4/5 충족)      → ×1.0  : 기준 (변경 없음)
      C등급 (3/5 충족)      → ×0.75 : 25% 줄여서 리스크 완화
      D/F                   → ×0.5  : 안전장치 (실제론 매수 안 함)

    베타 배율:
      β < 0.8  → ×1.2  (방어주 — 더 담아도 됨)
      β 0.8~1.2 → ×1.0 (시장 추종)
      β 1.2~1.5 → ×0.8 (공격주)
      β > 1.5  → ×0.6  (고변동 — 작게)

    ATR 배율:
      ATR < 1% → ×1.1  (저변동)
      ATR 1~3% → ×1.0  (기본)
      ATR > 3% → ×0.85 (고변동 — 사이즈 줄임)

    1차 비중: 점수 8+→60%  6~7→45%  나머지→30%

    최종: base × effective_mult × grade_m × beta_m × atr_m
    """

    # ── 등급 배율 ─────────────────────────────────────────────────
    grade_m = GRADE_POSITION_MULT.get(grade, 1.0)

    # ── 베타 배율 ─────────────────────────────────────────────────
    if   beta < 0.8:  beta_m = 1.20
    elif beta <= 1.2: beta_m = 1.00
    elif beta <= 1.5: beta_m = 0.80
    else:             beta_m = 0.60

    # ── ATR 배율 ──────────────────────────────────────────────────
    if   atr_pct < 0.01: atr_m = 1.10
    elif atr_pct <= 0.03: atr_m = 1.00
    else:                 atr_m = 0.85

    total = math.floor(
        base_capital * effective_mult * grade_m * beta_m * atr_m / 1000
    ) * 1000
    total = max(0, total)

    # ── 분할 비중 ─────────────────────────────────────────────────
    if   score >= 8: fr = 0.60
    elif score >= 6: fr = 0.45
    else:            fr = 0.30

    first  = math.floor(total * fr / 1000) * 1000
    second = total - first

    return {
        "total":       total,
        "first":       first,
        "second":      second,
        "first_ratio": fr,
        "grade":       grade,
        "grade_mult":  grade_m,
        "beta":        beta,
        "beta_mult":   beta_m,
        "atr_mult":    atr_m,
        "desc": (
            f"총{total//10000}만 | 1차{first//10000}만({int(fr*100)}%) | "
            f"{grade}등급(×{grade_m}) β{beta:.1f}(×{beta_m}) "
            f"ATR{atr_pct*100:.1f}%(×{atr_m})"
        ),
    }

# ════════════════════════════════════════════════════════════════════════
# ⑦ 통합 분석 — trader.py에서 매수 전 단일 호출
# ════════════════════════════════════════════════════════════════════════

def analyze_before_buy(
    token: str,
    stock: dict,          # results.json 종목 dict
    trade_type: str,      # "long" / "short"
    regime_sl: float,
    regime_tp: list[float],
    base_capital: float,
    effective_mult: float,
    min_score: int = 4,   # 최소 점수 (국면별 조정값 포함)
) -> dict:
    """
    매수 전 분석 통합 함수.

    반환:
    {
        "ok":       bool,    # False면 매수 스킵
        "reason":   str,
        "score":    int,
        "strength": str,
        "sl":       float,   # ATR 조정 손절
        "tp":       list,    # ATR 조정 익절
        "atr_pct":  float,
        "beta":     float,
        "sizing":   dict,    # calc_position_size 결과 (grade_mult 포함)
    }
    """
    ticker = stock.get("ticker", "")
    name   = stock.get("name", ticker)
    grade  = stock.get("grade", "C")   # ★ 등급 읽기 (포지션 사이징에 전달)
    rsi    = float(stock.get("rsi",  50))
    ch5    = float(stock.get("ch5",   0))
    ch20   = float(stock.get("ch20",  0))

    # ── 외국인 순매수 조회 ──────────────────────────────────────────
    foreign_net = get_foreign_net(token, ticker)
    time.sleep(0.2)

    # ── OHLCV + ATR ────────────────────────────────────────────────
    ohlcv   = get_ohlcv(token, ticker, days=20)
    atr_pct = calc_atr(ohlcv, period=14) if ohlcv else BASE_ATR_PCT
    time.sleep(0.2)

    # 거래량 비율 (20일 평균 대비)
    vol_20avg = (
        sum(o["volume"] for o in ohlcv[1:21]) / min(len(ohlcv) - 1, 20)
        if len(ohlcv) > 1 else 0
    )
    vol_today  = ohlcv[0]["volume"] if ohlcv else 0
    vol_ratio  = vol_today / vol_20avg if vol_20avg > 0 else 1.0

    # MA5/MA20 계산
    closes      = [o["close"] for o in ohlcv if o["close"] > 0]
    ma5         = sum(closes[:5])  / 5  if len(closes) >= 5  else 0
    ma20        = sum(closes[:20]) / 20 if len(closes) >= 20 else 0
    ma5_gt_ma20 = ma5 > ma20 > 0

    # 상승 캔들
    is_rising = ohlcv[0]["close"] >= ohlcv[0]["open"] if ohlcv else True

    # 순위 변동
    rank_change = stock.get("rank_change", 0)
    if rank_change is None:
        rank_change = 3          # NEW = 약한 상승으로 간주
    elif isinstance(rank_change, str):
        rank_change = {"NEW": 3, "→": 0}.get(rank_change, 0)

    # ── 매수 점수 ───────────────────────────────────────────────────
    score_res = calc_buy_score(
        grade=grade, rsi=rsi, ch5=ch5, ch20=ch20,
        foreign_net=foreign_net, volume_ratio=vol_ratio,
        is_rising_candle=is_rising, ma5_gt_ma20=ma5_gt_ma20,
        rank_change=rank_change,
    )
    score = score_res["score"]

    grade_m = GRADE_POSITION_MULT.get(grade, 1.0)
    print(
        f"  [{name}] {grade}등급(×{grade_m}) 점수:{score}점({score_res['strength']}) | "
        f"RSI:{rsi:.0f} 외인:{'+' if foreign_net > 0 else ''}{foreign_net:,} "
        f"ATR:{atr_pct*100:.1f}% 거래량:{vol_ratio:.1f}x"
    )

    if score < min_score:
        return {
            "ok":       False,
            "reason":   f"매수점수 부족 ({score}점 < {min_score}점)",
            "score":    score,
            "strength": score_res["strength"],
        }

    # ── 베타 ─────────────────────────────────────────────────────────
    beta = get_beta(ticker)

    # ── ATR 조정 손절/익절 ──────────────────────────────────────────
    levels = adjust_sl_tp(trade_type, atr_pct, regime_sl, regime_tp)

    # ── 포지션 사이징 (★ grade 전달) ──────────────────────────────
    sizing = calc_position_size(
        base_capital   = base_capital,
        effective_mult = effective_mult,
        score          = score,
        grade          = grade,        # ★ 등급 전달 → grade_m 적용
        beta           = beta,
        atr_pct        = atr_pct,
    )

    if sizing["total"] <= 0:
        return {
            "ok":       False,
            "reason":   "포지션 금액 0원",
            "score":    score,
            "strength": score_res["strength"],
        }

    print(f"  [{name}] {sizing['desc']} | 손절:{levels['sl_str']} 익절:{levels['tp_str']}")

    return {
        "ok":        True,
        "reason":    "OK",
        "score":     score,
        "strength":  score_res["strength"],
        "sl":        levels["sl"],
        "tp":        levels["tp"],
        "atr_pct":   atr_pct,
        "beta":      beta,
        "sizing":    sizing,
        "foreign_net": foreign_net,
        "vol_ratio": round(vol_ratio, 2),
    }

# ── 단독 테스트 ────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  analytics.py 단독 테스트 (API 없이)")
    print("=" * 60)

    # 점수 테스트
    cases = [
        dict(grade="A", rsi=40, ch5=-1, ch20=10, foreign_net=5000,
             volume_ratio=1.8, is_rising_candle=True,  ma5_gt_ma20=True,  rank_change=3),
        dict(grade="B", rsi=62, ch5=7,  ch20=20, foreign_net=-1000,
             volume_ratio=0.8, is_rising_candle=False, ma5_gt_ma20=False, rank_change=-2),
        dict(grade="C", rsi=33, ch5=-3, ch20=5,  foreign_net=2000,
             volume_ratio=2.5, is_rising_candle=True,  ma5_gt_ma20=False, rank_change=0),
    ]
    for c in cases:
        r = calc_buy_score(**c)
        print(f"  {c['grade']}등급 RSI{c['rsi']} → {r['score']}점 [{r['strength']}] "
              f"1차비중:{int(r['first_ratio']*100)}%")
    print()

    # ATR 조정 테스트
    for atr in [0.010, 0.020, 0.030, 0.040]:
        r = adjust_sl_tp("long", atr, -0.10, [0.10, 0.18, 0.25])
        print(f"  ATR {atr*100:.1f}% → 손절:{r['sl_str']} 익절:{r['tp_str']}")
    print()

    # ★ 포지션 사이징 테스트 — 등급별 비교 (장투 200만 / SIDEWAYS 0.7x)
    print("  ── 등급별 포지션 사이징 (장투 200만 / 국면배율 0.7x / β1.0 / ATR2%) ──")
    for grade in ["A", "B", "C"]:
        r = calc_position_size(2_000_000, 0.7, 7, grade=grade, beta=1.0, atr_pct=0.02)
        print(f"  {grade}등급(×{r['grade_mult']}) → 총{r['total']//10000}만 "
              f"1차{r['first']//10000}만({int(r['first_ratio']*100)}%)")
    print()

    # 베타 조합 테스트 (A등급 기준)
    print("  ── A등급 × 베타 조합 (단타 300만 / 국면배율 1.0x / ATR2%) ──")
    for beta in [0.7, 1.0, 1.3, 1.8]:
        r = calc_position_size(3_000_000, 1.0, 7, grade="A", beta=beta, atr_pct=0.02)
        print(f"  β{beta} → {r['desc']}")
