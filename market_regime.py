"""
market_regime.py — StockPilot KR 시장 국면 감지 모듈
=====================================================
KOSPI 기술적 지표 + 외인 수급 기반으로 3단계 국면을 판단하고
국면별 전략 파라미터를 반환합니다.

국면: BULL / SIDEWAYS / BEAR / UNKNOWN
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import requests
from datetime import datetime, date, timedelta

# ── 설정 ──────────────────────────────────────────────────────────────
REGIME_CACHE_FILE = "regime_cache.json"
REGIME_CACHE_TTL_MIN = 30          # 30분마다 재계산
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

# KOSPI 장기 사이클 기준 (2020 코로나 저점 이후)
# 2020 저점 ~1,440 → 2021 고점 ~3,316 → 2026 현재 ~7,493
# 기존 기준(3300이 경계)은 2021년 수준 → KOSPI 7,000대 기준으로 재조정
KOSPI_CYCLE_BASE_YEAR = 2020
KOSPI_LEVELS = {
    "recovery":   (0,    5000),   # 저점 회복: 공격적 매수
    "uptrend":    (5000, 8500),   # 정상 상승: 기본 전략 (7,000→8,500으로 상향 조정)
    "overheated": (8500, 10000),  # 과열: 포지션 축소
    "caution":    (10000, 99999), # 경계: 장투 신규매수 금지
}

# ── 국면별 전략 파라미터 ───────────────────────────────────────────────
REGIME_PARAMS = {
    "BULL": {
        "label":               "강세장 🟢",
        "allow_new_longterm":  True,
        "allow_new_daytrend":  True,
        "position_multiplier": 1.0,       # 기본 포지션 배율
        "long_tp":             [0.12, 0.20, 0.30],  # 장투 익절
        "short_tp":            [0.08, 0.12, 0.16],  # 단타 익절
        "long_sl":             -0.10,     # 장투 손절
        "short_sl":            -0.05,     # 단타 손절
        "min_buy_score":       5,          # 매수 최소 점수
        "trailing_stop":       0.04,       # 트레일링 스탑
    },
    "SIDEWAYS": {
        "label":               "횡보장 🟡",
        "allow_new_longterm":  True,       # 장투 허용 (단타 폐기 → 장투만 운용, 배율로 제어)
        "allow_new_daytrend":  False,      # 단타 폐기
        "position_multiplier": 0.6,        # 횡보장 배율 (0.7→0.6으로 보수적 조정)
        "long_tp":             [0.08, 0.15, 0.22],
        "short_tp":            [0.06, 0.09, 0.12],
        "long_sl":             -0.08,
        "short_sl":            -0.05,
        "min_buy_score":       6,
        "trailing_stop":       0.03,
    },
    "BEAR": {
        "label":               "약세장 🔴",
        "allow_new_longterm":  False,
        "allow_new_daytrend":  False,
        "position_multiplier": 0.0,        # 신규매수 없음
        "long_tp":             [0.05, 0.09, 0.13],
        "short_tp":            [0.04, 0.07, 0.10],
        "long_sl":             -0.06,      # 손절 더 타이트
        "short_sl":            -0.04,
        "min_buy_score":       9,           # 사실상 매수 불가
        "trailing_stop":       0.02,
    },
    "UNKNOWN": {
        "label":               "판단불가 ⚪",
        "allow_new_longterm":  False,
        "allow_new_daytrend":  False,   # 단타 전략 폐기에 맞게 수정
        "position_multiplier": 0.5,
        "long_tp":             [0.08, 0.15, 0.22],
        "short_tp":            [0.06, 0.09, 0.12],
        "long_sl":             -0.08,
        "short_sl":            -0.05,
        "min_buy_score":       7,
        "trailing_stop":       0.03,
    },
}

# ── 캐시 관리 ──────────────────────────────────────────────────────────
def _load_cache():
    if not os.path.exists(REGIME_CACHE_FILE):
        return None
    try:
        with open(REGIME_CACHE_FILE, "r") as f:
            cache = json.load(f)
        cached_at = datetime.fromisoformat(cache["cached_at"])
        if (datetime.now() - cached_at).seconds < REGIME_CACHE_TTL_MIN * 60:
            return cache
    except Exception:
        pass
    return None

def _save_cache(data: dict):
    data["cached_at"] = datetime.now().isoformat()
    with open(REGIME_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── KOSPI 데이터 로드 ──────────────────────────────────────────────────
def _get_kospi_df(period="3mo") -> pd.DataFrame:
    """yfinance로 KOSPI 일봉 DataFrame 반환. 실패 시 results.json fallback."""
    try:
        ticker = yf.Ticker("^KS11")
        df = ticker.history(period=period)
        if df.empty:
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except Exception as e:
        print(f"[market_regime] KOSPI yfinance 실패: {e}")
        return pd.DataFrame()

def _get_kospi_from_results() -> dict:
    """
    yfinance 실패 시 screener가 저장한 results.json에서 KOSPI 값 추출.
    반환: {"close": float, "ma5": float, "ma20": float, "ma60": float,
            "rsi": float, "ch5": float, "ch1": float} 또는 {}
    """
    for fname in ["results.json", "../results.json"]:
        try:
            with open(fname, "r", encoding="utf-8") as f:
                data = json.load(f)
            ms = data.get("market_signal", {})
            close = float(ms.get("kospi_close", 0))
            if close <= 0:
                continue
            return {
                "close": close,
                "ma5":   float(ms.get("ma5",  0)),
                "ma20":  float(ms.get("ma20", 0)),
                "ma60":  float(ms.get("ma60", 0)),
                "rsi":   float(ms.get("rsi_14", 50)),
                "ch5":   float(ms.get("kospi_ch5", 0)),
                "ch1":   float(ms.get("kospi_ch1", 0)),
                "vkospi": float(ms.get("vkospi_est", 0)),
            }
        except Exception:
            continue
    return {}

# ── VKOSPI 근사치 계산 ─────────────────────────────────────────────────
def _estimate_vkospi(df: pd.DataFrame) -> float:
    """
    VKOSPI API 없을 때 근사치:
    20일 일간 변동성(표준편차) × √252 × 100
    실제 VKOSPI와 유사한 수준으로 추정
    """
    if len(df) < 20:
        return 20.0
    daily_ret = df["Close"].pct_change().dropna()
    vol_20 = daily_ret.tail(20).std()
    return round(vol_20 * np.sqrt(252) * 100, 2)

# ── 국면 판단 핵심 로직 ────────────────────────────────────────────────
def _judge_regime(
    current: float,
    ma20: float,
    ma60: float,
    ret_5d: float,
    ret_20d: float,
    vkospi: float,
    vol_trend: bool,
) -> str:
    """
    조건표
    ┌──────────────────────────────────────────────────────┐
    │  BULL:   current > MA20 > MA60 AND ret_20d > 2%     │
    │  BEAR:   current < MA20 < MA60 AND ret_20d < -3%    │
    │  SIDEWAYS: 그 외                                     │
    └──────────────────────────────────────────────────────┘
    VKOSPI > 28: 국면 등급 한 단계 하향 (BULL→SIDEWAYS, SIDEWAYS→BEAR)
    """
    if current > ma20 > ma60 and ret_20d > 2.0:
        regime = "BULL"
    elif current < ma20 < ma60 and ret_20d < -3.0:
        regime = "BEAR"
    else:
        regime = "SIDEWAYS"

    # 변동성 과다 → 한 단계 하향
    if vkospi > 28:
        downgrade = {"BULL": "SIDEWAYS", "SIDEWAYS": "BEAR", "BEAR": "BEAR"}
        regime = downgrade[regime]

    return regime

# ── KOSPI 장기 사이클 단계 ──────────────────────────────────────────────
def _get_cycle_stage(kospi_level: float) -> dict:
    """
    현재 KOSPI 수준 기반 대사이클 단계 판단.
    KOSPI_LEVELS 구간별 배율 조정만 적용 (시간 기반 패널티 제거).
    → 시간 기반 패널티는 급등장에서 영구적으로 배율을 낮추는 부작용이 있어 제거.
      KOSPI 수준 자체가 이미 위험도를 반영함.
    """
    for stage, (lo, hi) in KOSPI_LEVELS.items():
        if lo <= kospi_level < hi:
            multiplier_adj = {
                "recovery":   +0.2,   # 저점 구간: 포지션 추가
                "uptrend":    0.0,    # 기본
                "overheated": -0.2,   # 과열: 포지션 축소
                "caution":    -0.4,   # 경계: 큰 폭 축소
            }[stage]
            return {
                "stage": stage,
                "kospi_level": kospi_level,
                "multiplier_adj": multiplier_adj,
                "longterm_new_buy": stage not in ("caution",),
            }

    return {"stage": "unknown", "multiplier_adj": 0.0, "longterm_new_buy": True}

# ── 메인 공개 함수 ─────────────────────────────────────────────────────
def get_market_regime(force_refresh: bool = False) -> dict:
    """
    시장 국면 전체 분석 결과 반환.
    캐시 유효 시 캐시 반환, force_refresh=True 시 강제 재계산.

    반환 예시:
    {
      "regime": "BULL",
      "label": "강세장 🟢",
      "kospi": 2750.5,
      "ma20": 2700.0,
      "ma60": 2650.0,
      "ret_5d": 1.2,
      "ret_20d": 4.5,
      "vkospi_est": 18.3,
      "vol_trend": True,
      "cycle_stage": "uptrend",
      "cycle_multiplier_adj": 0.0,
      "longterm_new_buy_cycle": True,
      "params": { ... REGIME_PARAMS[regime] ... },
      "effective_position_multiplier": 1.0,
      "cached_at": "...",
    }
    """
    if not force_refresh:
        cached = _load_cache()
        if cached:
            return cached

    df = _get_kospi_df("3mo")
    if df.empty or len(df) < 60:
        # ── fallback: results.json에서 KOSPI 값 읽기 ─────────────────
        fb = _get_kospi_from_results()
        if fb and fb["close"] > 0:
            print(f"[market_regime] yfinance 실패 → results.json fallback (KOSPI {fb['close']:,.0f})")
            current = fb["close"]
            ma20    = fb["ma20"] or current
            ma60    = fb["ma60"] or current
            ret_5d  = fb["ch5"]
            # 20일 수익률: MA20 기반으로 계산 (단순 5일*4 근사는 급등락 시 -32% 등 왜곡 발생)
            # MA20이 유효하면 현가/MA20 비율로 추정, 없으면 5일*2로 보수적 근사
            if ma20 > 0 and ma20 != current:
                ret_20d = round((current - ma20) / ma20 * 100, 2)
            else:
                ret_20d = round(ret_5d * 2, 2)  # 보수적 근사 (기존 *4 대비 왜곡 감소)
            # VKOSPI: screener가 results.json에 저장한 실측값(investing.com) 우선
            vkospi = 20.0
            try:
                _fv = float(fb.get("vkospi", 0)) if isinstance(fb, dict) else 0
                if _fv > 0:
                    vkospi = _fv
            except Exception:
                pass
            vol_trend = False

            regime = _judge_regime(current, ma20, ma60, ret_5d, ret_20d, vkospi, vol_trend)
            cycle  = _get_cycle_stage(current)
            params = REGIME_PARAMS[regime].copy()
            effective_mult = max(0.0, min(1.3,
                params["position_multiplier"] + cycle["multiplier_adj"]
            ))
            longterm_ok = params["allow_new_longterm"] and cycle["longterm_new_buy"]

            result = {
                "regime":                      regime,
                "label":                       params["label"],
                "kospi":                       round(current, 2),
                "ma20":                        round(ma20, 2),
                "ma60":                        round(ma60, 2),
                "ret_5d":                      round(ret_5d, 2),
                "ret_20d":                     round(ret_20d, 2),
                "vkospi_est":                  vkospi,
                "vol_trend":                   vol_trend,
                "cycle_stage":                 cycle["stage"],
                "cycle_multiplier_adj":        cycle["multiplier_adj"],
                "longterm_new_buy_ok":         longterm_ok,
                "daytrend_new_buy_ok":         params["allow_new_daytrend"],
                "params":                      params,
                "effective_position_multiplier": round(effective_mult, 2),
                "source":                      "results.json_fallback",
            }
            _save_cache(result)
            return result

        # 완전 실패 시 기본값
        result = {
            "regime": "UNKNOWN",
            "label": REGIME_PARAMS["UNKNOWN"]["label"],
            "kospi": 0,
            "params": REGIME_PARAMS["UNKNOWN"],
            "effective_position_multiplier": 0.5,
            "error": "KOSPI 데이터 로드 실패",
        }
        _save_cache(result)
        return result

    close = df["Close"]
    current  = close.iloc[-1]
    ma20     = close.rolling(20).mean().iloc[-1]
    ma60     = close.rolling(60).mean().iloc[-1]
    ret_5d   = (current - close.iloc[-5])  / close.iloc[-5]  * 100
    ret_20d  = (current - close.iloc[-20]) / close.iloc[-20] * 100
    vkospi   = _estimate_vkospi(df)

    # ── 실제 VKOSPI 우선 사용 ──────────────────────────────────────
    # results.json에 screener가 저장한 실제 VKOSPI 값이 있으면 사용
    # (추정치는 급등장에서 변동성이 과대평가되어 BULL→SIDEWAYS 강등 오류 발생)
    try:
        import json as _json, os as _os
        if _os.path.exists("results.json"):
            _rj = _json.load(open("results.json", encoding="utf-8"))
            _vk = _rj.get("market_signal", {}).get("vkospi_est", 0)
            if _vk and float(_vk) > 0:
                vkospi = float(_vk)
    except Exception:
        pass   # 실패 시 추정치 유지

    # 거래량 추세: 최근 5일 평균 > 20일 평균
    vol_5    = df["Volume"].iloc[-5:].mean()
    vol_20   = df["Volume"].iloc[-20:].mean()
    vol_trend = bool(vol_5 > vol_20)

    regime = _judge_regime(current, ma20, ma60, ret_5d, ret_20d, vkospi, vol_trend)
    cycle  = _get_cycle_stage(current)
    params = REGIME_PARAMS[regime].copy()

    # 유효 포지션 배율 = 국면 배율 + 사이클 조정
    effective_mult = max(0.0, min(1.3,
        params["position_multiplier"] + cycle["multiplier_adj"]
    ))

    # 장투 신규매수: 국면 AND 사이클 둘 다 허용해야 가능
    longterm_ok = params["allow_new_longterm"] and cycle["longterm_new_buy"]

    result = {
        "regime":                      regime,
        "label":                       params["label"],
        "kospi":                       round(current, 2),
        "ma20":                        round(ma20, 2),
        "ma60":                        round(ma60, 2),
        "ret_5d":                      round(ret_5d, 2),
        "ret_20d":                     round(ret_20d, 2),
        "vkospi_est":                  vkospi,
        "vol_trend":                   vol_trend,
        "cycle_stage":                 cycle["stage"],
        "cycle_multiplier_adj":        cycle["multiplier_adj"],
        "longterm_new_buy_ok":         longterm_ok,
        "daytrend_new_buy_ok":         params["allow_new_daytrend"],
        "params":                      params,
        "effective_position_multiplier": round(effective_mult, 2),
    }

    # 국면 변화 감지 → Discord 알림
    cached = _load_cache()
    if cached and cached.get("regime") != regime:
        prev_label = cached.get("label", "?")
        _send_regime_change_alert(prev_label, params["label"], result)

    _save_cache(result)
    return result


def _send_regime_change_alert(prev: str, now: str, data: dict):
    if not DISCORD_WEBHOOK:
        return
    msg = (
        f"📊 **시장 국면 변화**\n"
        f"{prev} → {now}\n"
        f"KOSPI: {data['kospi']:,.0f} | MA20: {data['ma20']:,.0f} | MA60: {data['ma60']:,.0f}\n"
        f"20일 수익률: {data['ret_20d']:+.1f}% | VKOSPI(추정): {data['vkospi_est']:.1f}\n"
        f"유효 포지션 배율: {data['effective_position_multiplier']:.1f}x"
    )
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
    except Exception:
        pass


# ── 편의 함수 ─────────────────────────────────────────────────────────
def get_regime_params_only(regime: str = None) -> dict:
    """국면 파라미터만 빠르게 가져올 때 사용"""
    if regime is None:
        data = get_market_regime()
        regime = data.get("regime", "UNKNOWN")
    return REGIME_PARAMS.get(regime, REGIME_PARAMS["UNKNOWN"])


# ── 단독 실행 테스트 ───────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  market_regime.py 단독 테스트")
    print("=" * 55)
    result = get_market_regime(force_refresh=True)
    print(f"  국면         : {result['label']}")
    print(f"  KOSPI        : {result['kospi']:,.2f}")
    print(f"  MA20 / MA60  : {result['ma20']:,.2f} / {result['ma60']:,.2f}")
    print(f"  5일/20일 수익 : {result['ret_5d']:+.2f}% / {result['ret_20d']:+.2f}%")
    print(f"  VKOSPI 추정  : {result['vkospi_est']:.1f}")
    print(f"  사이클 단계  : {result['cycle_stage']}")
    print(f"  포지션 배율  : {result['effective_position_multiplier']}x")
    print(f"  장투 신규OK  : {result['longterm_new_buy_ok']}")
    print(f"  단타 신규OK  : {result['daytrend_new_buy_ok']}")
    print()
    p = result["params"]
    print(f"  장투 익절    : {[f'{x*100:.0f}%' for x in p['long_tp']]}")
    print(f"  장투 손절    : {p['long_sl']*100:.0f}%")
    print(f"  단타 익절    : {[f'{x*100:.0f}%' for x in p['short_tp']]}")
    print(f"  단타 손절    : {p['short_sl']*100:.0f}%")
    print(f"  최소매수점수 : {p['min_buy_score']}점")
