#!/usr/bin/env python3
"""
StockPilot KR — 옵션 만기일 분석 (expiry.py)
KIS 파생 계좌 불필요 / 무료 공개 데이터 활용
① 베이시스    : market_indicators.json 활용
② 풋/콜 비율  : 네이버 금융 or KRX (장 마감 후 제공)
③ 미결제약정  : KRX (장 마감 후 제공)
④ 외국인선물  : KRX (장 마감 후 제공)
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
def get_krx_session():
    """KRX 세션 쿠키 초기화"""
    import requests as req_lib
    session = req_lib.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    })
    try:
        # 메인 페이지 방문으로 세션 쿠키 획득
        session.get("https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd", timeout=10)
        # 파생상품 메뉴 페이지도 방문
        session.get("https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201050403", timeout=10)
    except:
        pass
    return session

def fetch_basis(session):
    """KRX 베이시스 추이(선물) - MDCSTAT13401"""
    try:
        today     = datetime.date.today()
        end_str   = today.strftime("%Y%m%d")
        start_str = (today - datetime.timedelta(days=7)).strftime("%Y%m%d")
        url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://data.krx.co.kr",
            "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201050401",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT13401",
            "locale": "ko_KR", "secugrpId": "1", "aggBasTpCd": "0",
            "prodid": "KR__FUK2I", "expmmNo": "1",
            "isuCd": "", "isuCd2": "",
            "strtDd": start_str, "endDd": end_str, "csvxls_isNo": "false"
        }
        res = session.post(url, headers=headers, data=data, timeout=12)
        print(f"    [베이시스] status={res.status_code}", end=" ")
        if res.status_code != 200: print("실패"); return None
        output = res.json().get("output", [])
        print(f"→ {len(output)}건")
        if not output: return None
        latest = output[0]
        for key in ["BASIS","basis","BAS"]:
            if key in latest:
                v = float(str(latest[key]).replace(",","") or 0)
                if v != 0:
                    if v > 1:    return {"signal":"🟢","desc":f"베이시스 +{v:.2f} (강세)"}
                    elif v > 0:  return {"signal":"🟡","desc":f"베이시스 +{v:.2f} (약강세)"}
                    elif v > -1: return {"signal":"🟡","desc":f"베이시스 {v:.2f} (약약세)"}
                    else:        return {"signal":"🔴","desc":f"베이시스 {v:.2f} (약세)"}
        fut  = float(str(latest.get("FUTURES_PRICE",latest.get("FUT_PRC",0))).replace(",","") or 0)
        spot = float(str(latest.get("SPOT_PRICE",latest.get("SPOT_PRC",0))).replace(",","") or 0)
        if fut > 0 and spot > 0:
            v = round(fut - spot, 2)
            if v > 1:    return {"signal":"🟢","desc":f"베이시스 +{v:.2f} (강세)"}
            elif v > 0:  return {"signal":"🟡","desc":f"베이시스 +{v:.2f} (약강세)"}
            elif v > -1: return {"signal":"🟡","desc":f"베이시스 {v:.2f} (약약세)"}
            else:        return {"signal":"🔴","desc":f"베이시스 {v:.2f} (약세)"}
        return None
    except Exception as e:
        print(f"  베이시스 실패: {e}"); return None

def fetch_pcr_naver():
    """네이버 금융 옵션 시장에서 P/C비율 + 미결제약정 스크래핑 (디버그 모드)"""
    try:
        import requests as req_lib
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
            "Referer": "https://finance.naver.com/futureoption/",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }

        urls = [
            "https://finance.naver.com/futureoption/market.naver?type=OPTION&futureId=K2&menuType=market",
            "https://finance.naver.com/futureoption/market.naver?type=OPTION&futureId=K2",
        ]

        for url in urls:
            try:
                res = req_lib.get(url, headers=headers, timeout=12)
                html = res.content.decode("euc-kr", errors="ignore")
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text(" ", strip=True)

                # 디버그: 텍스트 앞 500자 출력
                print(f"\n    [Naver HTML텍스트]: {text[:500]}")

                pcr_patterns = [
                    r'P/C\s*비율\s*([\d.]+)',
                    r'풋콜\s*비율\s*([\d.]+)',
                    r'P/C\s*Ratio\s*([\d.]+)',
                    r'P/C\s*([\d]+\.\d+)',
                ]
                for pat in pcr_patterns:
                    m = re.search(pat, text, re.IGNORECASE)
                    if m:
                        pcr = float(m.group(1))
                        if 0.1 < pcr < 10:
                            print(f"    ✅ P/C비율 발견: {pcr}")
                            if pcr > 1.5:   sig, desc = "🔴", f"P/C {pcr:.2f} (풋우세·약세)"
                            elif pcr > 1.0: sig, desc = "🟡", f"P/C {pcr:.2f} (중립)"
                            else:           sig, desc = "🟢", f"P/C {pcr:.2f} (콜우세·강세)"
                            return {"type": "pcr", "signal": sig, "desc": desc}

                oi_patterns = [r'미결제\s*약정\s*([\d,]+)', r'미결제\s*([\d,]+)']
                for pat in oi_patterns:
                    m = re.search(pat, text)
                    if m:
                        oi = int(m.group(1).replace(",", ""))
                        if oi > 100:
                            print(f"    ✅ 미결제약정 발견: {oi:,}")
                            return {"type": "oi", "signal": "🟡", "desc": f"{oi:,}계약"}

            except Exception as e:
                print(f"    [URL 오류]: {e}")
                continue

        return None
    except Exception as e:
        print(f"  네이버 크롤링 실패: {e}")
        return None

# ── ② 풋/콜 비율 (KRX, 장 마감 후) ──────────────────────────────
def get_prev_trading_date():
    """오늘 데이터 없을 때 쓸 직전 거래일"""
    today = datetime.date.today()
    d = today - datetime.timedelta(days=1)
    while d.weekday() >= 5:   # 토·일 건너뜀
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")

def fetch_pcr_krx(session):
    try:
        today     = datetime.date.today()
        end_str   = today.strftime("%Y%m%d")
        # 조회 시작일: 5영업일 전 (주말 포함 7일 여유)
        start_str = (today - datetime.timedelta(days=7)).strftime("%Y%m%d")

        url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://data.krx.co.kr",
            "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201050403",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {
            "bld":        "dbms/MDC/STAT/standard/MDCSTAT13601",
            "locale":     "ko_KR",
            "prodId":     "KR__OPK2I",
            "strtDd":     start_str,
            "endDd":      end_str,
            "aggBasTpCd": "",
            "share":      "1",
            "csvxls_isNo": "false"
        }
        res = session.post(url, headers=headers, data=data, timeout=12)
        print(f"    [PCR KRX] status={res.status_code}", end=" ")
        if res.status_code != 200:
            print("실패"); return None

        output = res.json().get("output", [])
        print(f"→ {len(output)}건")
        if not output:
            return None

        # 가장 최근 데이터 사용
        latest = output[0]
        print(f"    keys: {list(latest.keys())}")

        # PUT/CALL 거래량으로 P/C 비율 계산
        put_vol  = float(str(latest.get("PUT",  latest.get("put_trdvol",  0))).replace(",", "") or 0)
        call_vol = float(str(latest.get("CALL", latest.get("call_trdvol", 0))).replace(",", "") or 0)

        # 직접 PCR 필드가 있으면 사용
        for key in ["PCR", "pcr", "PC_RATIO", "PUT_CALL_RATIO"]:
            if key in latest:
                pcr = float(str(latest[key]).replace(",","") or 0)
                if 0.1 < pcr < 10:
                    if pcr > 1.5:   sig, desc = "🔴", f"P/C {pcr:.2f} (풋우세·약세)"
                    elif pcr > 1.0: sig, desc = "🟡", f"P/C {pcr:.2f} (중립)"
                    else:           sig, desc = "🟢", f"P/C {pcr:.2f} (콜우세·강세)"
                    return {"signal": sig, "desc": desc}

        # PUT/CALL 거래량으로 직접 계산
        if call_vol > 0 and put_vol > 0:
            pcr = round(put_vol / call_vol, 2)
            if pcr > 1.5:   sig, desc = "🔴", f"P/C {pcr:.2f} (풋우세·약세)"
            elif pcr > 1.0: sig, desc = "🟡", f"P/C {pcr:.2f} (중립)"
            else:           sig, desc = "🟢", f"P/C {pcr:.2f} (콜우세·강세)"
            return {"signal": sig, "desc": desc}

        return None
    except Exception as e:
        print(f"  P/C KRX 실패: {e}")
        return None

def fetch_foreign_futures_krx(session):
    """KRX 투자자별 거래실적 - 외국인 코스피200 선물 순매수"""
    try:
        today     = datetime.date.today()
        end_str   = today.strftime("%Y%m%d")
        start_str = (today - datetime.timedelta(days=7)).strftime("%Y%m%d")

        url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://data.krx.co.kr",
            "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201050302",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {
            "bld":          "dbms/MDC/STAT/standard/MDCSTAT13101",
            "locale":       "ko_KR",
            "isuCd":        "KR__FUK2I",
            "inqTpCd":      "1",
            "prtType":      "AMT",
            "prtCheck":     "SUN",
            "aggBasTpCd":   "",
            "strtDd":       start_str,
            "endDd":        end_str,
            "strtDdBox1":   start_str,
            "endDdBox1":    end_str,
            "share":        "1",
            "money":        "3",
            "csvxls_isNo":  "false"
        }
        res = session.post(url, headers=headers, data=data, timeout=12)
        print(f"    [외국인선물 KRX] status={res.status_code}", end=" ")
        if res.status_code != 200:
            print("실패"); return None

        output = res.json().get("output", [])
        print(f"→ {len(output)}건")
        if not output:
            return None

        # 가장 최근 날짜 데이터에서 외국인 찾기
        latest = output[0]
        print(f"    keys: {list(latest.keys())[:10]}")

        # 외국인 순매수 수량 찾기
        for key in ["FRGN_NET_BUY", "frgn_netbuy", "FOREIGN_NET", "NET_BUY"]:
            if key in latest:
                net = float(str(latest[key]).replace(",","") or 0)
                direction = "매수" if net > 0 else "매도"
                sig = "🟢" if net > 1000 else ("🔴" if net < -1000 else "🟡")
                return {"signal": sig, "desc": f"외국인 {net:+,.0f}계약 ({direction})"}

        # 전체 row에서 외국인 행 찾기
        for row in output:
            name = str(row.get("INVST_TP_NM", row.get("invst_tp_nm", ""))).strip()
            if "외국인" in name or "foreign" in name.lower():
                for key in ["NET_BUY_QTY", "NETBUY", "NET_QTY", "SELN_WTNM_NETBUY_QTY"]:
                    if key in row:
                        net = float(str(row[key]).replace(",","") or 0)
                        direction = "매수" if net > 0 else "매도"
                        sig = "🟢" if net > 1000 else ("🔴" if net < -1000 else "🟡")
                        return {"signal": sig, "desc": f"외국인 {net:+,.0f}계약 ({direction})"}

        return None
    except Exception as e:
        print(f"  외국인선물 KRX 실패: {e}")
        return None

def fetch_oi_krx(session):
    """KRX 최근월물 시세 추이(선물) - 미결제약정 - MDCSTAT12701"""
    try:
        today     = datetime.date.today()
        end_str   = today.strftime("%Y%m%d")
        start_str = (today - datetime.timedelta(days=7)).strftime("%Y%m%d")

        url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://data.krx.co.kr",
            "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201050103",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {
            "bld":         "dbms/MDC/STAT/standard/MDCSTAT12701",
            "locale":      "ko_KR",
            "prodId":      "KR__FUK2I",
            "strtDd":      start_str,
            "endDd":       end_str,
            "aggBasTpCd":  "",
            "share":       "1",
            "money":       "3",
            "csvxls_isNo": "false"
        }
        res = session.post(url, headers=headers, data=data, timeout=12)
        print(f"    [미결제 KRX] status={res.status_code}", end=" ")
        if res.status_code != 200:
            print("실패"); return None

        output = res.json().get("output", [])
        print(f"→ {len(output)}건")
        if not output:
            return None

        # 가장 최근 2개 데이터로 변화량 계산
        latest = output[0]
        prev   = output[1] if len(output) > 1 else None
        print(f"    keys: {list(latest.keys())[:10]}")

        for key in ["OPNINT_QTY", "OI", "OPNINT", "opnint_qty", "REMA_QTY"]:
            if key in latest:
                oi      = int(str(latest[key]).replace(",","") or 0)
                prev_oi = int(str(prev[key]).replace(",","") or 0) if prev else 0
                chg     = oi - prev_oi
                if oi > 0:
                    if chg < -5000:  sig, desc = "🔴", f"{oi:,}계약 ↓{abs(chg):,} 청산압력"
                    elif chg < 0:    sig, desc = "🟡", f"{oi:,}계약 ↓{abs(chg):,} 소폭감소"
                    elif chg > 5000: sig, desc = "🟢", f"{oi:,}계약 ↑{chg:,} 포지션확대"
                    else:            sig, desc = "🟡", f"{oi:,}계약 보합"
                    return {"signal": sig, "desc": desc}
        return None
    except Exception as e:
        print(f"  미결제약정 KRX 실패: {e}")
        return None

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
        print("  KRX 세션 초기화 중...")
        krx_session = get_krx_session()

        # ① 베이시스 (항상 시도 가능)
        print(f"  [① 베이시스]", end=" ", flush=True)
        basis = fetch_basis(krx_session)
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
            pcr = fetch_pcr_naver() or fetch_pcr_krx(krx_session)
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
            oi = fetch_oi_krx(krx_session)
            if oi:
                indicators["oi"] = {"signal": oi["signal"], "desc": oi["desc"]}
                print(oi["desc"])
            else:
                indicators["oi"] = {"signal": "—", "desc": "조회 실패"}
                print("조회 실패")

        # ③ 외국인 선물 순매수 (KRX)
        print(f"  [④ 외국인선물]", end=" ", flush=True)
        if in_market:
            indicators["foreign"] = {"signal": "🕐", "desc": "장 마감 후 제공 (~16:00)"}
            print("장 마감 후 제공")
        else:
            ff = fetch_foreign_futures_krx(krx_session)
            if ff:
                indicators["foreign"] = {"signal": ff["signal"], "desc": ff["desc"]}
                print(ff["desc"])
            else:
                indicators["foreign"] = {"signal": "📱", "desc": "증권앱에서 직접 확인"}
                print("조회 실패 → 증권앱 확인")


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
