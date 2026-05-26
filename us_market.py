#!/usr/bin/env python3
"""
us_market.py — 미국 시장 야간 데이터 수집
==========================================
S&P500 / NASDAQ / VIX / 공포탐욕지수를 yfinance로 수집해
us_market.json에 저장합니다.

GitHub Actions에서 미국 장 중 30분 간격으로 실행됩니다.
KST 22:30~06:00 (미국 서머타임 기준)
"""

import json, os, time, datetime
from zoneinfo import ZoneInfo

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

KST      = ZoneInfo("Asia/Seoul")
OUT_FILE = "us_market.json"


def fetch_us_data() -> dict:
    result = {
        "updated":     datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "sp500":  {"close": 0, "change": 0, "change_pct": 0},
        "nasdaq": {"close": 0, "change": 0, "change_pct": 0},
        "vix":    {"close": 0, "level": ""},
        "sp500_aligned": "혼조", "sp500_rsi": 50.0,
        "ndx_aligned":   "혼조", "ndx_rsi":   50.0,
        "us_signal": "", "us_signal_en": "WATCH",
    }
    if not HAS_YF:
        return result

    key_map = {"sp500": "^GSPC", "nasdaq": "^IXIC", "vix": "^VIX"}
    data = {}

    for key, sym in key_map.items():
        try:
            tk = yf.Ticker(sym)
            df = tk.history(period="3mo", interval="1d")
            if df.empty or len(df) < 1:
                continue
            prices = list(df["Close"].dropna())
            close  = float(prices[-1])
            prev   = float(prices[-2]) if len(prices) >= 2 else close
            ch     = close - prev
            ch_pct = ch / prev * 100 if prev > 0 else 0
            data[key] = {"close": round(close, 2), "change": round(ch, 2),
                         "change_pct": round(ch_pct, 2), "prices": prices}
            time.sleep(0.3)
        except Exception as e:
            print(f"  [{sym}] 조회 실패: {e}")

    if "sp500" in data:
        result["sp500"] = {k: v for k, v in data["sp500"].items() if k != "prices"}
    if "nasdaq" in data:
        result["nasdaq"] = {k: v for k, v in data["nasdaq"].items() if k != "prices"}

    # ── S&P500 / NASDAQ 각각 MA정배열 + RSI ──────────────────────────
    for key, result_key in [("sp500", "sp500"), ("nasdaq", "ndx")]:
        if key not in data:
            continue
        prices = data[key]["prices"]
        close  = float(prices[-1])
        ma5    = sum(float(p) for p in prices[-5:])  / 5  if len(prices) >= 5  else close
        ma20   = sum(float(p) for p in prices[-20:]) / 20 if len(prices) >= 20 else ma5
        ma60   = sum(float(p) for p in prices[-60:]) / 60 if len(prices) >= 60 else ma20

        # MA 정배열
        if close > ma5 > ma20 > ma60:   result[f"{result_key}_aligned"] = "정배열"
        elif close < ma5 < ma20 < ma60: result[f"{result_key}_aligned"] = "역배열"
        else:                            result[f"{result_key}_aligned"] = "혼조"

        # RSI
        if len(prices) >= 15:
            deltas   = [float(prices[i])-float(prices[i-1]) for i in range(1, len(prices))]
            gains    = [d if d > 0 else 0 for d in deltas[-14:]]
            losses   = [-d if d < 0 else 0 for d in deltas[-14:]]
            avg_gain = sum(gains)/14; avg_loss = sum(losses)/14
            rsi_val  = 100.0 if avg_loss==0 else round(100-(100/(1+avg_gain/avg_loss)),1)
            result[f"{result_key}_rsi"] = rsi_val

    if "vix" in data:
        vix = data["vix"]["close"]
        result["vix"]["close"] = vix
        if vix >= 30:   result["vix"]["level"] = "공포극단"
        elif vix >= 25: result["vix"]["level"] = "불안"
        elif vix >= 20: result["vix"]["level"] = "보통"
        elif vix >= 15: result["vix"]["level"] = "안정"
        else:           result["vix"]["level"] = "낙관"

    # 미장 시그널 판단
    sp5_pct = result["sp500"].get("change_pct", 0)
    ndx_pct = result["nasdaq"].get("change_pct", 0)
    vix_val = result["vix"].get("close", 20)
    avg_pct = (sp5_pct + ndx_pct) / 2

    if avg_pct >= 1.0 and vix_val < 25:
        result["us_signal"]    = "📈 상승장"
        result["us_signal_en"] = "BUY"
    elif avg_pct >= 0.3:
        result["us_signal"]    = "📈 약한 상승"
        result["us_signal_en"] = "BUY"
    elif avg_pct <= -1.0 or vix_val >= 30:
        result["us_signal"]    = "📉 하락장"
        result["us_signal_en"] = "SELL"
    elif avg_pct <= -0.3:
        result["us_signal"]    = "📉 약한 하락"
        result["us_signal_en"] = "SELL"
    else:
        result["us_signal"]    = "⚖️ 관망"
        result["us_signal_en"] = "WATCH"

    return result


def main():
    now = datetime.datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 미장 데이터  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}")

    data = fetch_us_data()

    sp5 = data["sp500"]
    ndx = data["nasdaq"]
    vix = data["vix"]
    print(f"  S&P500:  {sp5['close']:,.2f} ({sp5['change_pct']:+.2f}%)")
    print(f"  NASDAQ:  {ndx['close']:,.2f} ({ndx['change_pct']:+.2f}%)")
    print(f"  VIX:     {vix['close']:.1f} [{vix['level']}]")
    print(f"  시그널:  {data['us_signal']}")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 {OUT_FILE} 저장 완료\n✅ 완료!")


if __name__ == "__main__":
    main()
