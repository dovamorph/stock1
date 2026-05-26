#!/usr/bin/env python3
"""
StockPilot KR — 시장 지표 (market_indicators.py)
환율 / 유가(WTI) / 미국 10년물 금리 / 금 / 외국인 KOSPI 순매수
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

# ── KIS API 설정 ──────────────────────────────────────────────────────
BASE_KIS   = "https://openapi.koreainvestment.com:9443"
APP_KEY    = os.environ.get("KIS_APP_KEY", "")
APP_SECRET = os.environ.get("KIS_APP_SECRET", "")

def H(tok, tr_id):
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {tok}",
        "appkey": APP_KEY, "appsecret": APP_SECRET,
        "tr_id": tr_id, "custtype": "P"
    }

def get_kis_token():
    if not APP_KEY or not APP_SECRET:
        return ""
    try:
        r = requests.post(f"{BASE_KIS}/oauth2/tokenP", json={
            "grant_type": "client_credentials",
            "appkey": APP_KEY, "appsecret": APP_SECRET
        }, timeout=15)
        tok = r.json().get("access_token", "")
        if tok:
            print("  ✅ KIS 토큰 발급 완료")
        return tok
    except Exception as e:
        print(f"  KIS 토큰 발급 실패: {e}")
        return ""

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

# ── 외국인 KOSPI 순매수 ───────────────────────────────────────────────
def fetch_foreign_stock_net(tok, ticker, name):
    """
    개별 종목 외국인 순매수 (수량 + 대금)
    FHKST01010900: 투자자별 매매 조회
    """
    try:
        r = requests.get(
            f"{BASE_KIS}/uapi/domestic-stock/v1/quotations/inquire-investor",
            headers=H(tok, "FHKST01010900"),
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker},
            timeout=10
        )
        rows = r.json().get("output", [])
        if not rows:
            return None

        # 당일(rows[0]) 외국인 데이터
        row = rows[0]

        # 순매수 수량
        qty_raw = str(row.get("frgn_ntby_qty", "0")).replace(",", "").strip()
        qty = int(qty_raw) if qty_raw.lstrip("-").isdigit() else 0

        # 순매수 대금 (단위: 천원) — 필드명이 증권사마다 다를 수 있음
        pbmn_raw = str(row.get("frgn_ntby_pbmn", row.get("frgn_seln_pbmn", "0"))).replace(",", "").strip()
        pbmn_chonwon = int(pbmn_raw) if pbmn_raw.lstrip("-").isdigit() else 0

        return {"name": name, "qty": qty, "pbmn_eok": round(pbmn_chonwon / 100_000, 1)}
    except Exception as e:
        print(f"    {name} 외국인 조회 실패: {e}")
        return None

def fetch_foreign_kospi(tok):
    """
    삼성전자 + SK하이닉스 외국인 순매수 합산
    이 두 종목이 KOSPI 시가총액의 약 35% → 외인 전체 방향성의 proxy
    """
    if not tok:
        return None
    print("  [외국인] KOSPI 순매수 조회 (삼성전자·SK하이닉스) ...", end=" ", flush=True)

    targets = [("005930", "삼성전자"), ("000660", "SK하이닉스")]
    results = []
    for ticker, name in targets:
        d = fetch_foreign_stock_net(tok, ticker, name)
        if d:
            results.append(d)
        time.sleep(0.3)

    if not results:
        print("실패")
        return None

    total_qty  = sum(r["qty"]       for r in results)
    total_eok  = sum(r["pbmn_eok"]  for r in results)

    # 대금 데이터가 있으면 대금 기준, 없으면 수량 기준으로 표시
    use_amount = any(abs(r["pbmn_eok"]) > 0 for r in results)

    direction = "매수" if total_qty > 0 else "매도"
    if use_amount:
        signal = "🟢" if total_eok > 500 else ("🔴" if total_eok < -500 else "🟡")
    else:
        signal = "🟢" if total_qty > 500_000 else ("🔴" if total_qty < -500_000 else "🟡")

    detail = " / ".join(
        f"{r['name']}: {r['qty']:+,}주"
        + (f" ({r['pbmn_eok']:+,.0f}억)" if r['pbmn_eok'] != 0 else "")
        for r in results
    )
    print(f"{direction} {total_qty:+,}주" + (f" ({total_eok:+,.0f}억)" if use_amount else ""))

    return {
        "total_qty":   total_qty,
        "total_eok":   total_eok if use_amount else None,
        "direction":   direction,
        "signal":      signal,
        "use_amount":  use_amount,
        "detail":      detail,
        "note":        "삼성전자 + SK하이닉스 합산 (KOSPI 시총 ~35% proxy)"
    }

# ── 메인 ─────────────────────────────────────────────────────────────
def main():
    now = datetime.datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 시장 지표  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}\n")

    result = {"updated": now.strftime("%Y-%m-%d %H:%M"), "indicators": {}}
    inds   = result["indicators"]

    # ── KIS 토큰 ─────────────────────────────────────────────────────
    tok = get_kis_token()

    # ── 지표 수집 ─────────────────────────────────────────────────────
    # 내려가면 주식에 좋음 (dir: -1)

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

    # ── 외국인 KOSPI 순매수 (KIS API) ────────────────────────────────
    foreign = fetch_foreign_kospi(tok)
    if foreign:
        inds["foreign_kospi"] = foreign

    # ── 저장 ──────────────────────────────────────────────────────────
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 {OUT_FILE} 저장 완료")
    print(f"\n✅ 시장 지표 완료!")

if __name__ == "__main__":
    main()
