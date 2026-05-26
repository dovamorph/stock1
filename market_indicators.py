#!/usr/bin/env python3
"""
StockPilot KR — 시장 지표 (market_indicators.py)
환율 / 유가(WTI) / 미국 10년물 금리 / 금 / 외국인 KOSPI 순매수
yfinance 기반
"""
import os, json, datetime, time
from zoneinfo import ZoneInfo

try:
    import yfinance as yf
    import requests
except ImportError:
    print("pip install yfinance requests"); exit(1)

KST      = ZoneInfo("Asia/Seoul")
OUT_FILE = "market_indicators.json"

# ── 유틸 ─────────────────────────────────────────────────────────────
def sf(v, d=0.0):
    try:
        s = str(v).replace(",", "").strip()
        val = float(s) if s else d
        return d if val != val else val
    except:
        return d

def pct_change(cur, prev):
    if prev and prev != 0:
        return round((cur - prev) / abs(prev) * 100, 2)
    return 0.0

def fetch_yf(ticker, label):
    """yfinance로 최근 2일치 → 현재가 + 전일비"""
    try:
        df = yf.Ticker(ticker).history(period="5d")
        if df is None or len(df) < 1:
            return None
        closes = list(df["Close"].dropna())
        cur  = round(float(closes[-1]), 4)
        prev = round(float(closes[-2]), 4) if len(closes) >= 2 else cur
        chg  = round(cur - prev, 4)
        return {"value": cur, "prev": prev, "change": chg, "change_pct": pct_change(cur, prev)}
    except Exception as e:
        print(f"  {label} 조회 실패: {e}")
        return None

# ── 메인 ─────────────────────────────────────────────────────────────
def main():
    now = datetime.datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 시장 지표  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}\n")

    result = {"updated": now.strftime("%Y-%m-%d %H:%M"), "indicators": {}}
    inds   = result["indicators"]

    # ── 지표 수집 (yfinance 기반) ─────────────────────────────────────
    print("  [환율] USD/KRW ...", end=" ", flush=True)
    d = fetch_yf("KRW=X", "환율")
    if d:
        inds["usdkrw"] = {
            "label": "USD/KRW", "value": round(d["value"], 0), "unit": "원",
            "change": round(d["change"], 1), "change_pct": d["change_pct"],
            "signal": "🔴" if d["change_pct"] > 0.3 else ("🟢" if d["change_pct"] < -0.3 else "🟡"),
            "desc": "원화 약세" if d["change_pct"] > 0.5 else ("원화 강세" if d["change_pct"] < -0.5 else "안정")
        }
        print(f"{d['value']:,.0f}원 ({d['change_pct']:+.2f}%)")
    else:
        print("실패")
    time.sleep(0.3)

    print("  [유가] WTI ...", end=" ", flush=True)
    d = fetch_yf("CL=F", "WTI")
    if d:
        inds["wti"] = {
            "label": "WTI 유가", "value": round(d["value"], 2), "unit": "$/배럴",
            "change": round(d["change"], 2), "change_pct": d["change_pct"],
            "signal": "🔴" if d["change_pct"] > 1 else ("🟢" if d["change_pct"] < -1 else "🟡"),
            "desc": f"${d['value']:.1f}/배럴"
        }
        print(f"${d['value']:.2f} ({d['change_pct']:+.2f}%)")
    else:
        print("실패")
    time.sleep(0.3)

    print("  [금리] US 10Y ...", end=" ", flush=True)
    d = fetch_yf("^TNX", "US10Y")
    if d:
        val = round(d["value"], 3)
        chg = round(d["change"], 3)
        inds["us10y"] = {
            "label": "미국 10년물", "value": val, "unit": "%",
            "change": chg, "change_pct": d["change_pct"],
            "signal": "🔴" if chg > 0.05 else ("🟢" if chg < -0.05 else "🟡"),
            "desc": f"{val:.2f}% ({'위험' if val >= 4.5 else '주의' if val >= 4.0 else '안정'})"
        }
        print(f"{val:.3f}% ({chg:+.3f}%p)")
    else:
        print("실패")
    time.sleep(0.3)

    print("  [금] Gold ...", end=" ", flush=True)
    d = fetch_yf("GC=F", "금")
    if d:
        inds["gold"] = {
            "label": "금", "value": round(d["value"], 1), "unit": "$/oz",
            "change": round(d["change"], 1), "change_pct": d["change_pct"],
            "signal": "🔴" if d["change_pct"] > 0.5 else ("🟢" if d["change_pct"] < -0.5 else "🟡"),
            "desc": f"${d['value']:,.0f}/oz"
        }
        print(f"${d['value']:,.1f} ({d['change_pct']:+.2f}%)")
    else:
        print("실패")
    time.sleep(0.3)

    # ── 저장 ──────────────────────────────────────────────────────────
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 {OUT_FILE} 저장 완료")
    print(f"\n✅ 시장 지표 완료!")

if __name__ == "__main__":
    main()
