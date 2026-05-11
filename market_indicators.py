#!/usr/bin/env python3
"""
StockPilot KR — 시장 지표 (market_indicators.py)
환율 / 비트코인 / 유가(WTI) / 미국 10년물 금리 / 외국인 코스피 선물
yfinance + KIS API
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
    """yfinance로 최근 2일치 가져와서 현재가 + 전일비 반환"""
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="5d")
        if df is None or len(df) < 1:
            return None
        closes = list(df["Close"].dropna())
        cur  = round(float(closes[-1]), 4)
        prev = round(float(closes[-2]), 4) if len(closes) >= 2 else cur
        chg  = round(cur - prev, 4)
        chg_pct = pct_change(cur, prev)
        return {"value": cur, "prev": prev, "change": chg, "change_pct": chg_pct}
    except Exception as e:
        print(f"  {label} 조회 실패: {e}")
        return None

def fetch_foreign_futures(tok):
    """외국인 코스피200 선물 순매수 (KIS API)"""
    if not tok:
        return None
    try:
        # 투자자별 선물 매매동향
        r = requests.get(
            f"{BASE_KIS}/uapi/domestic-futureoption/v1/quotations/inquire-futureoption-invest-trend",
            headers=H(tok, "FHKIF04010200"),
            params={
                "FID_COND_MRKT_DIV_CODE": "F",
                "FID_INPUT_ISCD": "101V06",
                "FID_INPUT_DATE_1": "",
                "FID_BLNG_CLS_CODE": "0"
            },
            timeout=12
        )
        data = r.json().get("output", [])
        foreign = next((d for d in data if "외국인" in d.get("mbcr_name", "")), None)
        if not foreign:
            return None

        raw = foreign.get("futs_net_buy_qty", "0")
        is_neg = str(raw).startswith("-")
        net = int(str(raw).replace(",", "").replace("-", "") or 0)
        net_signed = -net if is_neg else net

        direction = "매수" if net_signed > 0 else "매도"
        signal = "🟢" if net_signed > 1000 else ("🔴" if net_signed < -1000 else "🟡")
        return {
            "net_qty": net_signed,
            "direction": direction,
            "signal": signal
        }
    except Exception as e:
        print(f"  외국인 선물 조회 실패: {e}")
        return None

def fetch_kospi_futures_price(tok):
    """KOSPI200 선물 현재가"""
    if not tok:
        return None
    try:
        now_m = datetime.datetime.now().month
        now_y = datetime.datetime.now().year
        exp_months = [3, 6, 9, 12]
        front_m = next(m for m in exp_months if m >= now_m)
        front_y = now_y
        if front_m < now_m:
            front_y += 1
        fut_code = f"101V{str(front_y)[-2:]}{str(front_m).zfill(2)}"

        r = requests.get(
            f"{BASE_KIS}/uapi/domestic-futureoption/v1/quotations/inquire-price",
            headers=H(tok, "FHKIF03010100"),
            params={"fid_cond_mrkt_div_code": "F", "fid_input_iscd": fut_code},
            timeout=10
        )
        out = r.json().get("output1", r.json().get("output", {}))
        price = sf(out.get("futs_prpr", out.get("stck_prpr", 0)))
        prev  = sf(out.get("futs_bspr", 0))
        chg   = round(price - prev, 2) if prev > 0 else 0
        chg_pct = pct_change(price, prev) if prev > 0 else 0
        if price > 0:
            return {"value": price, "prev": prev, "change": chg, "change_pct": chg_pct}
        return None
    except Exception as e:
        print(f"  KOSPI선물 조회 실패: {e}")
        return None

def main():
    now = datetime.datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 시장 지표  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}\n")

    result = {"updated": now.strftime("%Y-%m-%d %H:%M"), "indicators": {}}
    inds = result["indicators"]

    # ── 1. 환율 (USD/KRW) ─────────────────────────────────────────
    print("  [환율] USD/KRW ...", end=" ", flush=True)
    d = fetch_yf("KRW=X", "환율")
    if d:
        inds["usdkrw"] = {
            "label": "USD/KRW",
            "value": round(d["value"], 0),
            "unit": "원",
            "change": round(d["change"], 1),
            "change_pct": d["change_pct"],
            "signal": "🔴" if d["change_pct"] > 0.5 else ("🟢" if d["change_pct"] < -0.5 else "🟡"),
            "desc": "원화 약세 (달러 강세)" if d["change_pct"] > 0.5 else ("원화 강세" if d["change_pct"] < -0.5 else "안정")
        }
        print(f"{d['value']:,.0f}원 ({d['change_pct']:+.2f}%)")
    else:
        print("실패")
    time.sleep(0.3)

    # ── 2. 비트코인 (BTC/USD) ─────────────────────────────────────
    print("  [비트코인] BTC/USD ...", end=" ", flush=True)
    d = fetch_yf("BTC-USD", "비트코인")
    if d:
        inds["bitcoin"] = {
            "label": "BTC/USD",
            "value": round(d["value"], 0),
            "unit": "$",
            "change": round(d["change"], 0),
            "change_pct": d["change_pct"],
            "signal": "🟢" if d["change_pct"] > 2 else ("🔴" if d["change_pct"] < -2 else "🟡"),
            "desc": f"${d['value']:,.0f}"
        }
        print(f"${d['value']:,.0f} ({d['change_pct']:+.2f}%)")
    else:
        print("실패")
    time.sleep(0.3)

    # ── 3. 유가 WTI ───────────────────────────────────────────────
    print("  [유가] WTI ...", end=" ", flush=True)
    d = fetch_yf("CL=F", "WTI")
    if d:
        inds["wti"] = {
            "label": "WTI 유가",
            "value": round(d["value"], 2),
            "unit": "$/배럴",
            "change": round(d["change"], 2),
            "change_pct": d["change_pct"],
            "signal": "🔴" if d["change_pct"] > 2 else ("🟢" if d["change_pct"] < -2 else "🟡"),
            "desc": f"${d['value']:.1f}/배럴"
        }
        print(f"${d['value']:.2f} ({d['change_pct']:+.2f}%)")
    else:
        print("실패")
    time.sleep(0.3)

    # ── 4. 미국 10년물 금리 ───────────────────────────────────────
    print("  [금리] US 10Y ...", end=" ", flush=True)
    d = fetch_yf("^TNX", "US10Y")
    if d:
        val = round(d["value"], 3)
        chg = round(d["change"], 3)
        inds["us10y"] = {
            "label": "미국 10년물",
            "value": val,
            "unit": "%",
            "change": chg,
            "change_pct": d["change_pct"],
            "signal": "🔴" if val >= 4.5 else ("🟡" if val >= 4.0 else "🟢"),
            "desc": f"{val:.2f}% ({'위험' if val >= 4.5 else '주의' if val >= 4.0 else '안정'})"
        }
        print(f"{val:.3f}% ({chg:+.3f}%p)")
    else:
        print("실패")
    time.sleep(0.3)

    # ── 5. 금 (Gold) ──────────────────────────────────────────────
    print("  [금] Gold ...", end=" ", flush=True)
    d = fetch_yf("GC=F", "금")
    if d:
        inds["gold"] = {
            "label": "금",
            "value": round(d["value"], 1),
            "unit": "$/oz",
            "change": round(d["change"], 1),
            "change_pct": d["change_pct"],
            # 금 급등 = 지정학 리스크 또는 달러 약세 신호
            "signal": "🔴" if d["change_pct"] > 1.5 else ("🟢" if d["change_pct"] < -1.5 else "🟡"),
            "desc": f"${d['value']:,.0f}/oz"
        }
        print(f"${d['value']:,.1f} ({d['change_pct']:+.2f}%)")
    else:
        print("실패")
    time.sleep(0.3)

    # ── 6. KOSPI200 선물 (yfinance) ───────────────────────────────
    print("  [KOSPI200선물] ...", end=" ", flush=True)
    d = fetch_yf("^KS200", "KOSPI200")
    if d:
        inds["kospi_futures"] = {
            "label": "KOSPI200",
            "value": round(d["value"], 2),
            "unit": "pt",
            "change": round(d["change"], 2),
            "change_pct": d["change_pct"],
            "signal": "🟢" if d["change_pct"] > 0.5 else ("🔴" if d["change_pct"] < -0.5 else "🟡"),
            "desc": f"{d['value']:.2f}pt"
        }
        print(f"{d['value']:.2f}pt ({d['change_pct']:+.2f}%)")
    else:
        print("실패 (장외/주말)")
    time.sleep(0.3)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n  💾 {OUT_FILE} 저장 완료")
    print(f"\n✅ 시장 지표 완료!")

if __name__ == "__main__":
    main()
