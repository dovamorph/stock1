#!/usr/bin/env python3
"""
StockPilot KR — 옵션 만기일 분석 (expiry.py)
만기일 D-day + 4대 지표 (KIS 파생 계좌 불필요)
① 베이시스    : KOSPI200 선물 - 현물 (yfinance)
② 풋/콜 비율 : pykrx (KRX 무료 데이터)
③ 미결제약정  : pykrx (KRX 무료 데이터)
④ 외국인선물  : 증권앱 직접 확인 (데이터 없음으로 표시)
"""
import json, datetime, traceback
from zoneinfo import ZoneInfo

try:
    import yfinance as yf
except ImportError:
    print("pip install yfinance"); exit(1)

try:
    from pykrx import stock as pykrx_stock
    PYKRX_OK = True
except ImportError:
    print("  ⚠️ pykrx 없음 — pip install pykrx")
    PYKRX_OK = False

KST      = ZoneInfo("Asia/Seoul")
OUT_FILE = "expiry_result.json"

# ── 만기일 계산 (매월 두 번째 목요일) ─────────────────────────────
def get_expiry_dates(n=3):
    """앞으로 n개월 만기일 반환"""
    today = datetime.date.today()
    dates = []
    y, m = today.year, today.month
    for _ in range(n * 2):
        # 해당 월 첫 번째 날 찾기
        first = datetime.date(y, m, 1)
        # 첫 번째 목요일 (weekday: 0=월 3=목)
        days_to_thu = (3 - first.weekday()) % 7
        first_thu = first + datetime.timedelta(days=days_to_thu)
        # 두 번째 목요일 = 첫 번째 목요일 + 7일
        second_thu = first_thu + datetime.timedelta(days=7)
        if second_thu >= today:
            dates.append(second_thu)
        if len(dates) >= n:
            break
        m += 1
        if m > 12:
            m = 1; y += 1
    return dates

# ── ① 베이시스 계산 (yfinance) ───────────────────────────────────
def fetch_basis():
    """
    베이시스 = KOSPI200선물 - KOSPI200현물
    yfinance: ^KS200 = KOSPI200 현물 인덱스
              ^KS200F 또는 KS200F=F = 선물 (시도)
    """
    try:
        # KOSPI200 현물
        spot_ticker = yf.Ticker("^KS200")
        df_spot = spot_ticker.history(period="3d")
        if df_spot is None or len(df_spot) < 1:
            return None
        spot = round(float(list(df_spot["Close"].dropna())[-1]), 2)

        # KOSPI200 선물 시도
        futures_price = None
        for ticker in ["KM=F", "^KS200F"]:
            try:
                df_fut = yf.Ticker(ticker).history(period="3d")
                if df_fut is not None and len(df_fut) >= 1:
                    closes = list(df_fut["Close"].dropna())
                    if closes and float(closes[-1]) > 0:
                        futures_price = round(float(closes[-1]), 2)
                        break
            except:
                continue

        if futures_price is None or spot == 0:
            return None

        basis = round(futures_price - spot, 2)

        if basis > 2:
            signal = "🟢"; desc = f"강세(+{basis:.2f})"
        elif basis > 0:
            signal = "🟡"; desc = f"약강세(+{basis:.2f})"
        elif basis > -2:
            signal = "🟡"; desc = f"약약세({basis:.2f})"
        else:
            signal = "🔴"; desc = f"약세({basis:.2f})"

        return {"value": basis, "spot": spot, "futures": futures_price,
                "signal": signal, "desc": desc}
    except Exception as e:
        print(f"  베이시스 조회 실패: {e}")
        return None

# ── ② 풋/콜 비율 (pykrx) ─────────────────────────────────────────
def fetch_pcr():
    """pykrx로 KOSPI200 옵션 P/C 비율 조회"""
    if not PYKRX_OK:
        return None
    try:
        today = datetime.date.today()
        date_str = today.strftime("%Y%m%d")

        # pykrx 옵션 거래량 조회
        df = pykrx_stock.get_market_trading_volume_by_date(
            fromdate=date_str, todate=date_str, ticker="코스피200"
        )
        if df is not None and not df.empty:
            print(f"  pykrx trading volume: {df.columns.tolist()}")

        # 대안: 옵션 종합 통계
        try:
            df_opt = pykrx_stock.get_index_ohlcv_by_date(
                fromdate=date_str, todate=date_str, ticker="1028"
            )
            print(f"  pykrx KOSPI200 index: {df_opt}")
        except:
            pass

        return None  # pykrx 옵션 직접 조회는 추가 확인 필요
    except Exception as e:
        print(f"  P/C 비율 조회 실패: {e}")
        return None

# ── ② 풋/콜 비율 (KRX 웹 스크래핑) ──────────────────────────────
def fetch_pcr_krx():
    """KRX 정보데이터시스템에서 P/C 비율 스크래핑"""
    try:
        import urllib.request, json as json_lib
        today = datetime.date.today()
        date_str = today.strftime("%Y%m%d")

        # KRX 파생상품 일별 P/C Ratio API
        url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
        params = f"bld=dbms/MDC/STAT/standard/MDCSTAT12401&locale=ko_KR&trdDd={date_str}&share=1&money=1&csvxls_isNo=false"

        req = urllib.request.Request(
            url,
            data=params.encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://data.krx.co.kr/"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json_lib.loads(resp.read().decode("utf-8"))

        output = data.get("output", [])
        if not output:
            return None

        # 코스피200 옵션 P/C 비율 찾기
        for item in output:
            name = item.get("ITEM_NAME", "")
            if "200" in name or "코스피200" in name or "KOSPI200" in name:
                pcr_val = float(str(item.get("PCR", 0)).replace(",", "") or 0)
                if pcr_val > 0:
                    if pcr_val > 1.5:
                        signal = "🔴"; desc = f"P/C {pcr_val:.2f} (풋 우세·약세)"
                    elif pcr_val > 1.0:
                        signal = "🟡"; desc = f"P/C {pcr_val:.2f} (중립)"
                    else:
                        signal = "🟢"; desc = f"P/C {pcr_val:.2f} (콜 우세·강세)"
                    return {"value": pcr_val, "signal": signal, "desc": desc}

        return None
    except Exception as e:
        print(f"  P/C 비율 KRX 조회 실패: {e}")
        return None

# ── ③ 미결제약정 (KRX 웹 스크래핑) ──────────────────────────────
def fetch_oi_krx():
    """KRX에서 코스피200 선물 미결제약정 조회"""
    try:
        import urllib.request, json as json_lib
        today = datetime.date.today()
        date_str = today.strftime("%Y%m%d")

        url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
        params = f"bld=dbms/MDC/STAT/standard/MDCSTAT12301&locale=ko_KR&trdDd={date_str}&share=1&money=1&csvxls_isNo=false"

        req = urllib.request.Request(
            url,
            data=params.encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://data.krx.co.kr/"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json_lib.loads(resp.read().decode("utf-8"))

        output = data.get("output", [])
        if not output:
            return None

        for item in output:
            name = item.get("ITEM_NAME", "")
            if "200" in name or "코스피200" in name:
                oi_val = int(str(item.get("OI", "0")).replace(",", "") or 0)
                prev_oi = int(str(item.get("PREV_OI", "0")).replace(",", "") or 0)
                if oi_val > 0:
                    chg = oi_val - prev_oi
                    if chg < -10000:
                        signal = "🔴"; desc = f"{oi_val:,}계약 (급감 → 청산압력)"
                    elif chg < 0:
                        signal = "🟡"; desc = f"{oi_val:,}계약 (감소)"
                    elif chg > 10000:
                        signal = "🟢"; desc = f"{oi_val:,}계약 (급증 → 포지션확대)"
                    else:
                        signal = "🟡"; desc = f"{oi_val:,}계약 (유지)"
                    return {"value": oi_val, "change": chg, "signal": signal, "desc": desc}

        return None
    except Exception as e:
        print(f"  미결제약정 KRX 조회 실패: {e}")
        return None

# ── 종합 판단 ─────────────────────────────────────────────────────
def judge_expiry(d_day, indicators, active):
    if not active:
        return {"level": "관망", "color": "gray", "action": "D-6 이내부터 분석 시작"}

    scores = []
    # 베이시스
    basis = indicators.get("basis")
    if basis:
        if basis["value"] > 1:    scores.append(1)
        elif basis["value"] < -1: scores.append(-1)
        else:                      scores.append(0)
    # P/C 비율
    pcr = indicators.get("pcr")
    if pcr:
        if pcr["value"] > 1.5:   scores.append(-1)
        elif pcr["value"] < 0.7: scores.append(1)
        else:                     scores.append(0)
    # 미결제약정
    oi = indicators.get("oi")
    if oi:
        if oi.get("change", 0) < -10000: scores.append(-1)
        elif oi.get("change", 0) > 10000: scores.append(1)
        else:                              scores.append(0)

    if not scores:
        return {"level": "알 수 없음", "color": "gray", "action": "데이터 부족"}

    total = sum(scores)
    urgency = "🚨 즉각 대응" if d_day <= 1 else ("⚠️ 주의" if d_day <= 3 else "모니터링")

    if total >= 2:
        return {"level": "강세", "color": "green",
                "action": f"만기 강세 예상 ({urgency})"}
    elif total <= -2:
        return {"level": "약세", "color": "red",
                "action": f"만기 약세·변동성 주의 ({urgency})"}
    elif total < 0:
        return {"level": "약보합", "color": "orange",
                "action": f"하방 압력 주의 ({urgency})"}
    else:
        return {"level": "중립", "color": "yellow",
                "action": f"방향 불분명 ({urgency})"}


def main():
    now = datetime.datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 옵션만기 분석  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}")

    expiry_dates = get_expiry_dates(2)
    if not expiry_dates:
        print("  ⚠️ 만기일 계산 실패")
        return

    today       = datetime.date.today()
    expiry_date = expiry_dates[0]
    d_day       = (expiry_date - today).days
    active      = d_day <= 6

    print(f"  다음 만기일: {expiry_date} (D-{d_day})")

    indicators = {}

    if active:
        print(f"\n  ⚠️  만기일 D-{d_day} — 지표 분석 시작")

        # ① 베이시스
        print(f"  [① 베이시스]", end=" ", flush=True)
        basis = fetch_basis()
        if basis:
            indicators["basis"] = {"signal": basis["signal"], "desc": basis["desc"]}
            print(f"{basis['desc']}")
        else:
            print("데이터 없음")

        # ② 풋/콜 비율
        print(f"  [② 풋/콜 비율]", end=" ", flush=True)
        pcr = fetch_pcr_krx()
        if pcr:
            indicators["pcr"] = {"signal": pcr["signal"], "desc": pcr["desc"]}
            print(f"{pcr['desc']}")
        else:
            print("데이터 없음 (장 마감 후 제공)")

        # ③ 미결제약정
        print(f"  [③ 미결제약정]", end=" ", flush=True)
        oi = fetch_oi_krx()
        if oi:
            indicators["oi"] = {"signal": oi["signal"], "desc": oi["desc"]}
            print(f"{oi['desc']}")
        else:
            print("데이터 없음 (장 마감 후 제공)")

        # ④ 외국인 선물 (증권앱 직접 확인)
        indicators["foreign"] = {
            "signal": "📱",
            "desc": "증권앱에서 직접 확인"
        }
        print(f"  [④ 외국인선물] 증권앱 직접 확인")

    judgment = judge_expiry(d_day, indicators, active)
    print(f"\n  📊 종합 판단: {judgment['level']} → {judgment['action']}")

    result = {
        "expiry_date": str(expiry_date),
        "d_day":       d_day,
        "active":      active,
        "updated":     now.strftime("%Y-%m-%d %H:%M"),
        "indicators":  indicators,
        "judgment":    judgment
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n  💾 {OUT_FILE} 저장 완료")
    print(f"\n✅ 옵션 만기 분석 완료!")

if __name__ == "__main__":
    main()
