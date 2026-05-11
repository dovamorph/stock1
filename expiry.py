#!/usr/bin/env python3
"""
StockPilot KR — 옵션 만기일 분석 (expiry.py)
KIS 파생 계좌 불필요 / 무료 공개 데이터 활용
① 베이시스    : market_indicators.json 활용
② 풋/콜 비율  : 네이버 금융 or KRX (장 마감 후 제공)
③ 미결제약정  : KRX (장 마감 후 제공)
④ 외국인선물  : 증권앱 직접 확인
"""
import json, datetime, re, os
import urllib.request
from zoneinfo import ZoneInfo

KST      = ZoneInfo("Asia/Seoul")
OUT_FILE = "expiry_result.json"
MI_FILE  = "market_indicators.json"   # market_indicators.py 출력

# ── 장 운영 여부 ──────────────────────────────────────────────────
def is_market_hours():
    now = datetime.datetime.now(KST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return datetime.time(9, 0) <= t <= datetime.time(15, 32)

# ── 만기일 계산 (매월 두 번째 목요일) ─────────────────────────────
def get_expiry_dates(n=2):
    today = datetime.date.today()
    dates = []
    y, m = today.year, today.month
    for _ in range(n * 3):
        first = datetime.date(y, m, 1)
        days_to_thu = (3 - first.weekday()) % 7
        second_thu  = first + datetime.timedelta(days=days_to_thu + 7)
        if second_thu >= today:
            dates.append(second_thu)
        if len(dates) >= n:
            break
        m += 1
        if m > 12:
            m = 1; y += 1
    return dates

# ── ① 베이시스 (market_indicators.json 활용) ──────────────────────
def fetch_basis():
    """
    market_indicators.json의 KOSPI200(^KS200)을 현물로 사용
    실제 선물 가격은 별도 조회가 어려우므로 생략 → 현재 지수 변동률로 대체
    """
    try:
        if not os.path.exists(MI_FILE):
            return None
        with open(MI_FILE, "r", encoding="utf-8") as f:
            mi = json.load(f)
        ind = mi.get("indicators", {})
        kp = ind.get("kospi_futures", {})
        spot   = kp.get("value", 0)
        ch_pct = kp.get("change_pct", 0)
        if not spot:
            return None
        # 변동률 기반 신호 (선물 별도 없으므로 현물 강약으로 판단)
        if ch_pct >= 1.0:
            signal = "🟢"; desc = f"KOSPI200 {spot:.1f}pt ({ch_pct:+.2f}%) 강세"
        elif ch_pct <= -1.0:
            signal = "🔴"; desc = f"KOSPI200 {spot:.1f}pt ({ch_pct:+.2f}%) 약세"
        else:
            signal = "🟡"; desc = f"KOSPI200 {spot:.1f}pt ({ch_pct:+.2f}%) 중립"
        return {"signal": signal, "desc": desc}
    except Exception as e:
        print(f"  베이시스 실패: {e}")
        return None

# ── ② 풋/콜 비율 (네이버 금융) ───────────────────────────────────
def fetch_pcr_naver():
    try:
        urls = [
            "https://finance.naver.com/futureoption/optionDealing.naver?marketCode=K2",
            "https://finance.naver.com/futureoption/market.naver?type=OPTION&futureId=K2&menuType=market",
        ]
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://finance.naver.com/"
                })
                with urllib.request.urlopen(req, timeout=12) as resp:
                    html = resp.read().decode("euc-kr", errors="ignore")

                # P/C 비율 패턴들 시도
                patterns = [
                    r'P/C비율[^<]*<[^>]+>([0-9.]+)',
                    r'풋콜비율[^<]*<[^>]+>([0-9.]+)',
                    r'pc_ratio[^>]*>([0-9.]+)',
                    r'P/C[^0-9]*([0-9]+\.[0-9]+)',
                ]
                for pat in patterns:
                    m = re.search(pat, html, re.IGNORECASE)
                    if m:
                        pcr = float(m.group(1))
                        if 0.1 < pcr < 10:
                            if pcr > 1.5:   sig, desc = "🔴", f"P/C {pcr:.2f} (풋우세·약세)"
                            elif pcr > 1.0: sig, desc = "🟡", f"P/C {pcr:.2f} (중립)"
                            else:           sig, desc = "🟢", f"P/C {pcr:.2f} (콜우세·강세)"
                            return {"signal": sig, "desc": desc}
            except:
                continue
        return None
    except Exception as e:
        print(f"  P/C 네이버 실패: {e}")
        return None

# ── ② 풋/콜 비율 (KRX, 장 마감 후) ──────────────────────────────
def fetch_pcr_krx():
    try:
        today = datetime.date.today()
        date_str = today.strftime("%Y%m%d")
        url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"

        # 여러 파라미터 조합 시도
        param_list = [
            f"bld=dbms/MDC/STAT/standard/MDCSTAT12401&locale=ko_KR&trdDd={date_str}&share=1&money=1&csvxls_isNo=false",
            f"bld=dbms/MDC/STAT/standard/MDCSTAT12401&locale=ko_KR&trdDd={date_str}&prodId=201VX06&csvxls_isNo=false",
            f"bld=dbms/MDC/STAT/standard/MDCSTAT12401&locale=ko_KR&trdDd={date_str}&mktId=KRX&prodId=201V06&csvxls_isNo=false",
        ]
        for params in param_list:
            try:
                req = urllib.request.Request(
                    url, data=params.encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "User-Agent": "Mozilla/5.0",
                             "Referer": "https://data.krx.co.kr/"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                output = data.get("output", [])
                if not output:
                    continue
                for item in output:
                    for key in ["PCR", "PUT_CALL_RATIO", "pcr"]:
                        if key in item:
                            pcr = float(str(item[key]).replace(",", "") or 0)
                            if 0.1 < pcr < 10:
                                if pcr > 1.5:   sig, desc = "🔴", f"P/C {pcr:.2f} (풋우세·약세)"
                                elif pcr > 1.0: sig, desc = "🟡", f"P/C {pcr:.2f} (중립)"
                                else:           sig, desc = "🟢", f"P/C {pcr:.2f} (콜우세·강세)"
                                return {"signal": sig, "desc": desc}
            except:
                continue
        return None
    except Exception as e:
        print(f"  P/C KRX 실패: {e}")
        return None

# ── ③ 미결제약정 (KRX, 장 마감 후) ──────────────────────────────
def fetch_oi_krx():
    try:
        today = datetime.date.today()
        date_str = today.strftime("%Y%m%d")
        url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"

        param_list = [
            f"bld=dbms/MDC/STAT/standard/MDCSTAT12301&locale=ko_KR&trdDd={date_str}&share=1&money=1&csvxls_isNo=false",
            f"bld=dbms/MDC/STAT/standard/MDCSTAT12301&locale=ko_KR&trdDd={date_str}&prodId=201VX06&csvxls_isNo=false",
        ]
        for params in param_list:
            try:
                req = urllib.request.Request(
                    url, data=params.encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "User-Agent": "Mozilla/5.0",
                             "Referer": "https://data.krx.co.kr/"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                output = data.get("output", [])
                if not output:
                    continue
                for item in output:
                    name = str(item.get("ITEM_NAME", "") or item.get("ISU_NM", ""))
                    if "200" not in name and "KOSPI" not in name.upper():
                        continue
                    for key in ["OI", "OPNINT_QTY", "REMA_QTY", "OPNINT"]:
                        if key in item:
                            oi = int(str(item[key]).replace(",", "") or 0)
                            prev_oi = int(str(item.get("PREV_"+key, 0)).replace(",", "") or 0)
                            if oi > 0:
                                chg = oi - prev_oi
                                if chg < -5000:    sig, desc = "🔴", f"{oi:,}계약 (↓{abs(chg):,} 청산압력)"
                                elif chg < 0:      sig, desc = "🟡", f"{oi:,}계약 (↓{abs(chg):,} 소폭감소)"
                                elif chg > 5000:   sig, desc = "🟢", f"{oi:,}계약 (↑{chg:,} 포지션확대)"
                                else:              sig, desc = "🟡", f"{oi:,}계약 (보합)"
                                return {"signal": sig, "desc": desc}
            except:
                continue
        return None
    except Exception as e:
        print(f"  미결제약정 KRX 실패: {e}")
        return None

# ── 종합 판단 ─────────────────────────────────────────────────────
def judge_expiry(d_day, indicators, active, in_market):
    if not active:
        return {"level": "대기", "color": "gray",
                "action": "D-6 이내부터 분석 시작"}

    urgency = ""
    if d_day <= 1:   urgency = "🚨 D-1 최고경계"
    elif d_day <= 2: urgency = "🚨 D-2 변동성주의"
    elif d_day == 3: urgency = "⚠️ D-3 경계강화"
    else:            urgency = "📌 모니터링"

    if in_market:
        return {"level": f"장중 ({urgency})", "color": "yellow",
                "action": "장 마감 후 지표 업데이트 예정 (16:00~)"}

    # 장 마감 후 판단
    scores = []
    basis = indicators.get("basis", {})
    if basis.get("signal") == "🟢": scores.append(1)
    elif basis.get("signal") == "🔴": scores.append(-1)

    pcr = indicators.get("pcr", {})
    if pcr.get("signal") == "🟢": scores.append(1)
    elif pcr.get("signal") == "🔴": scores.append(-1)

    oi = indicators.get("oi", {})
    if oi.get("signal") == "🟢": scores.append(1)
    elif oi.get("signal") == "🔴": scores.append(-1)

    if not scores:
        return {"level": "알 수 없음", "color": "gray",
                "action": f"데이터 부족 ({urgency})"}

    total = sum(scores)
    if total >= 2:
        return {"level": "강세예상", "color": "green",
                "action": f"만기 강세 예상 ({urgency})"}
    elif total <= -2:
        return {"level": "약세주의", "color": "red",
                "action": f"만기 변동성·약세 주의 ({urgency})"}
    elif total < 0:
        return {"level": "약보합", "color": "orange",
                "action": f"하방 압력 주의 ({urgency})"}
    else:
        return {"level": "중립", "color": "yellow",
                "action": f"방향 불분명 ({urgency})"}

# ── 메인 ──────────────────────────────────────────────────────────
def main():
    now       = datetime.datetime.now(KST)
    in_market = is_market_hours()

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
    print(f"  장 상태: {'장중' if in_market else '장외/마감'}")

    indicators = {}

    if active:
        print(f"\n  ⚠️  만기일 D-{d_day} — 지표 분석 시작")

        # ① 베이시스 (항상 시도 가능)
        print(f"  [① 베이시스]", end=" ", flush=True)
        basis = fetch_basis()
        if basis:
            indicators["basis"] = {"signal": basis["signal"], "desc": basis["desc"]}
            print(basis["desc"])
        else:
            indicators["basis"] = {"signal": "—", "desc": "데이터 없음"}
            print("데이터 없음")

        # ② P/C 비율 (장 마감 후 정확)
        print(f"  [② 풋/콜 비율]", end=" ", flush=True)
        if in_market:
            indicators["pcr"] = {"signal": "🕐", "desc": "장 마감 후 제공 (~16:00)"}
            print("장 마감 후 제공")
        else:
            pcr = fetch_pcr_naver() or fetch_pcr_krx()
            if pcr:
                indicators["pcr"] = {"signal": pcr["signal"], "desc": pcr["desc"]}
                print(pcr["desc"])
            else:
                indicators["pcr"] = {"signal": "—", "desc": "조회 실패"}
                print("조회 실패")

        # ③ 미결제약정 (장 마감 후 정확)
        print(f"  [③ 미결제약정]", end=" ", flush=True)
        if in_market:
            indicators["oi"] = {"signal": "🕐", "desc": "장 마감 후 제공 (~16:00)"}
            print("장 마감 후 제공")
        else:
            oi = fetch_oi_krx()
            if oi:
                indicators["oi"] = {"signal": oi["signal"], "desc": oi["desc"]}
                print(oi["desc"])
            else:
                indicators["oi"] = {"signal": "—", "desc": "조회 실패"}
                print("조회 실패")

        # ④ 외국인 선물
        indicators["foreign"] = {"signal": "📱", "desc": "증권앱에서 직접 확인"}
        print(f"  [④ 외국인선물] 증권앱 직접 확인")

    judgment = judge_expiry(d_day, indicators, active, in_market)
    print(f"\n  📊 종합 판단: {judgment['level']} → {judgment['action']}")

    result = {
        "expiry_date": str(expiry_date),
        "d_day":       d_day,
        "active":      active,
        "in_market":   in_market,
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
