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
            }
    if not HAS_YF:
        return result

    tickers = {"sp500": "^GSPC", "nasdaq": "^IXIC", "vix": "^VIX"}
    data = {}

    for key, sym in tickers.items():
        try:
            tk = yf.Ticker(sym)
            df = tk.history(period="2d", interval="1d")
            if df.empty or len(df) < 1:
                continue
            close  = float(df["Close"].iloc[-1])
            prev   = float(df["Close"].iloc[-2]) if len(df) >= 2 else close
            ch     = close - prev
            ch_pct = ch / prev * 100 if prev > 0 else 0
            data[key] = {"close": round(close, 2), "change": round(ch, 2),
                         "change_pct": round(ch_pct, 2)}
            time.sleep(0.3)
        except Exception as e:
            print(f"  [{sym}] 조회 실패: {e}")

    if "sp500" in data:
        result["sp500"] = data["sp500"]
    if "nasdaq" in data:
        result["nasdaq"] = data["nasdaq"]

    if "vix" in data:
        vix = data["vix"]["close"]
        result["vix"]["close"] = vix
        # screener.py와 동일 기준으로 통일 (같은 VIX가 파일마다 다른 라벨이 되지 않도록)
        if vix >= 35:   result["vix"]["level"] = "극공포"
        elif vix >= 25: result["vix"]["level"] = "공포"
        elif vix >= 20: result["vix"]["level"] = "불안"
        elif vix >= 15: result["vix"]["level"] = "안정"
        else:           result["vix"]["level"] = "과열낙관"

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

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 {OUT_FILE} 저장 완료\n✅ 완료!")


if __name__ == "__main__":
    main()
