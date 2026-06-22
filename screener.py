"""
StockPilot KR — KIS OpenAPI 스크리닝
지표: 거래대금 / ROE / PER / PBR / EPS / EPS추세 / 배당여부 / 20일등락
시장 시그널: KOSPI MA5/MA20/MA60 정배열/역배열 기반
등급: A(5/5) B(4/5) C(3/5) D(2/5) F(1이하) — 부채비율 포함 5개 기준
"""
import os, json, time, traceback
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests, pandas as pd
    import FinanceDataReader as fdr
    import yfinance as yf
except ImportError:
    print("pip install requests pandas finance-datareader yfinance"); exit(1)

APP_KEY    = os.environ.get("KIS_APP_KEY","")
APP_SECRET = os.environ.get("KIS_APP_SECRET","")
DISCORD    = os.environ.get("DISCORD_WEBHOOK","")
BASE       = "https://openapi.koreainvestment.com:9443"
TOP_N      = 40
CAND_N     = 500
CAND_CACHE = "candidates_cache.json"   # KRX 서버 오류 시 폴백용
RESULTS_FILE = "results.json"

ETF_KW = ["ETF","ETN","KODEX","TIGER","KBSTAR","ARIRANG","HANARO","SOL","ACE",
          "RISE","레버리지","인버스","선물","PLUS","TIMEFOLIO"]

def sf(v, d=0.0):
    try:
        s=str(v).replace(",","").strip()
        val=float(s) if s else d
        return d if val!=val else val
    except: return d

def get_token():
    for attempt in range(3):
        try:
            r=requests.post(f"{BASE}/oauth2/tokenP",timeout=15,
                json={"grant_type":"client_credentials","appkey":APP_KEY,"appsecret":APP_SECRET})
            r.raise_for_status()
            tok=r.json().get("access_token","")
            if tok:
                print("  ✅ KIS 토큰 발급 완료"); return tok
        except Exception as e:
            print(f"  토큰 발급 시도 {attempt+1}/3 실패: {e}")
            if attempt < 2: time.sleep(3)
    raise ValueError("토큰 발급 최종 실패")

def H(tok, tr_id):
    return {"Content-Type":"application/json","authorization":f"Bearer {tok}",
            "appkey":APP_KEY,"appsecret":APP_SECRET,"tr_id":tr_id}

def is_etf(name): return any(k in name for k in ETF_KW)

# ── 0단계: KOSPI 시장 시그널 ─────────────────────────────────────
def fetch_market_signal(tok) -> dict:
    result = {
        "reason": "데이터 없음",
        "kospi_close": 0, "ma5": 0, "ma20": 0, "ma60": 0,
        "kospi_ch5": 0, "kospi_ch20": 0, "aligned": "",
        "kosdaq_close": 0, "kosdaq_ch5": 0, "kosdaq_aligned": "혼조", "kosdaq_rsi": 50.0,
        "rsi_14": 50.0, "basis": None, "basis_signal": "조회불가",
        "usdkrw": 0, "usdkrw_ch1": 0, "usdkrw_ch5": 0,
    }
    try:
        reasons = []
        now = datetime.now()
        s   = (now - timedelta(days=120)).strftime("%Y-%m-%d")
        e   = now.strftime("%Y-%m-%d")
        # ── MA/RSI 계산용 과거 가격 배열 ─────────────────────────────
        df  = fdr.DataReader("KS11", s, e)

        try:
            df_kq = fdr.DataReader("KQ11", s, e)
            if df_kq is not None and len(df_kq) >= 2:
                kq_prices = list(df_kq["Close"].dropna())[::-1]
                result["kosdaq_close"] = round(kq_prices[0], 2)
                result["kosdaq_ch5"]   = round((kq_prices[0]-kq_prices[4])/kq_prices[4]*100, 2) if len(kq_prices)>=5 and kq_prices[4]>0 else 0
                result["kosdaq_ch1"]   = round((kq_prices[0]-kq_prices[1])/kq_prices[1]*100, 2) if len(kq_prices)>=2 and kq_prices[1]>0 else 0
                # ── KOSDAQ MA 정배열 ──────────────────────────────────
                if len(kq_prices) >= 20:
                    kq_c   = kq_prices[0]
                    kq_ma5 = sum(kq_prices[:5])  / 5
                    kq_ma20= sum(kq_prices[:20]) / 20
                    kq_ma60= sum(kq_prices[:60]) / 60 if len(kq_prices)>=60 else sum(kq_prices)/len(kq_prices)
                    if kq_c > kq_ma5 > kq_ma20 > kq_ma60:   result["kosdaq_aligned"] = "정배열"
                    elif kq_c < kq_ma5 < kq_ma20 < kq_ma60: result["kosdaq_aligned"] = "역배열"
                    else:                                      result["kosdaq_aligned"] = "혼조"
                # ── KOSDAQ RSI ────────────────────────────────────────
                if len(kq_prices) >= 15:
                    p_asc  = kq_prices[:15][::-1]
                    gains  = [max(p_asc[i]-p_asc[i-1], 0) for i in range(1,15)]
                    losses = [max(p_asc[i-1]-p_asc[i], 0) for i in range(1,15)]
                    avg_g  = sum(gains)/14; avg_l = sum(losses)/14
                    result["kosdaq_rsi"] = 100.0 if avg_l==0 else round(100-(100/(1+avg_g/avg_l)),1)
        except: pass

        # ── 원/달러 환율 (외인 이탈 선행 신호용) ──────────────────────
        try:
            df_fx = fdr.DataReader("USD/KRW", s, e)
            if df_fx is not None and len(df_fx) >= 2:
                fx = list(df_fx["Close"].dropna())[::-1]   # 최신이 앞
                result["usdkrw"]     = round(fx[0], 2)
                result["usdkrw_ch1"] = round((fx[0]-fx[1])/fx[1]*100, 2) if len(fx)>=2 and fx[1]>0 else 0
                result["usdkrw_ch5"] = round((fx[0]-fx[4])/fx[4]*100, 2) if len(fx)>=5 and fx[4]>0 else 0
        except: pass

        # ── KIS 일별 차트 API: 당일 ch1 확보 (항상 호출) ─────────────
        s2 = (now - timedelta(days=60)).strftime("%Y%m%d")
        e2 = now.strftime("%Y%m%d")
        try:
            res = requests.get(
                f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
                headers=H(tok,"FHKUP03500100"), timeout=10,
                params={"fid_cond_mrkt_div_code":"U","fid_input_iscd":"0001",
                        "fid_input_date_1":s2,"fid_input_date_2":e2,"fid_period_div_code":"D"}
            )
            rjson = res.json()
            items = rjson.get("output2", rjson.get("output",[]))
            if items:
                today_str  = now.strftime("%Y%m%d")
                latest     = max(items, key=lambda x: x.get("stck_bsop_date",""))  # 응답 정렬 무관 최신 거래일 행
                item_date  = latest.get("stck_bsop_date","")
                item_ctrt  = sf(latest.get("bstp_nmix_prdy_ctrt", 0))
                item_close = sf(latest.get("bstp_nmix_prpr", 0))
                # 오늘 KIS 데이터가 있으면 라이브값 사용 (fdr 일별종가보다 신선)
                if item_date == today_str and item_close > 0:
                    result["kospi_ch1"]   = round(item_ctrt, 2)
                    result["kospi_close"] = round(item_close, 2)
                    print(f"  KOSPI 당일: {item_close:,.2f} ({item_ctrt:+.2f}%) [KIS]")
                else:
                    print(f"  ⚠️ KIS KOSPI 최신행={item_date or '없음'} (오늘 {today_str} 아님/종가0) → fdr 사용")
                # MA/RSI용 가격 배열 (날짜 내림차순 정렬해 최신이 맨 앞)
                prices_raw = [sf(x.get("bstp_nmix_prpr",0)) for x in sorted(items, key=lambda x: x.get("stck_bsop_date",""), reverse=True)]
                kis_prices = [p for p in prices_raw if p > 0]
            else:
                print("  ⚠️ KIS KOSPI 지수 응답 비어있음 → fdr 사용")
                kis_prices = []
        except Exception as e:
            print(f"  ⚠️ KIS 일별 차트 오류: {e}")
            items = []; kis_prices = []

        # ── KOSDAQ 당일 보정: KIS API(1001)로 실시간 ch1 확보 ────────
        # (fdr은 일별 종가라 장중엔 전일값 → KOSPI처럼 KIS 당일값으로 덮어씀)
        try:
            res_kq = requests.get(
                f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
                headers=H(tok,"FHKUP03500100"), timeout=10,
                params={"fid_cond_mrkt_div_code":"U","fid_input_iscd":"1001",
                        "fid_input_date_1":s2,"fid_input_date_2":e2,"fid_period_div_code":"D"}
            )
            rjson_kq = res_kq.json()
            items_kq = rjson_kq.get("output2", rjson_kq.get("output",[]))
            if items_kq:
                today_str   = now.strftime("%Y%m%d")
                latest_kq   = max(items_kq, key=lambda x: x.get("stck_bsop_date",""))  # 정렬 무관 최신 거래일 행
                kq_date     = latest_kq.get("stck_bsop_date","")
                kq_ctrt     = sf(latest_kq.get("bstp_nmix_prdy_ctrt", 0))
                kq_close_k  = sf(latest_kq.get("bstp_nmix_prpr", 0))
                if kq_date == today_str and kq_close_k > 0:
                    result["kosdaq_ch1"]   = round(kq_ctrt, 2)
                    result["kosdaq_close"] = round(kq_close_k, 2)
                    print(f"  KOSDAQ 당일: {kq_close_k:,.2f} ({kq_ctrt:+.2f}%) [KIS]")
                else:
                    print(f"  ⚠️ KIS KOSDAQ 최신행={kq_date or '없음'} (오늘 {today_str} 아님/종가0) → fdr 사용")
            else:
                print("  ⚠️ KIS KOSDAQ 지수 응답 비어있음 → fdr 사용")
        except Exception as e:
            print(f"  ⚠️ KIS KOSDAQ 차트 오류: {e}")

        if df is None or len(df) < 20:
            prices = kis_prices if len(kis_prices) >= 20 else []
        else:
            prices = list(df["Close"].dropna())[::-1]
            # DataReader 사용 시 ch1이 아직 0이면 prices로 계산
            if result.get("kospi_ch1", 0) == 0 and len(prices) >= 2 and prices[1] > 0:
                result["kospi_ch1"]   = round((prices[0]-prices[1])/prices[1]*100, 2)
                result["kospi_close"] = round(prices[0], 2)

        if len(prices) < 20:
            return result

        close = prices[0]
        ma5   = sum(prices[:5])  / 5
        ma20  = sum(prices[:20]) / 20
        ma60  = sum(prices[:60]) / 60 if len(prices) >= 60 else sum(prices) / len(prices)

        rsi_14 = 50.0
        if len(prices) >= 15:
            p_asc = prices[:15][::-1]
            gains = [max(p_asc[i]-p_asc[i-1], 0) for i in range(1,15)]
            losses= [max(p_asc[i-1]-p_asc[i], 0) for i in range(1,15)]
            avg_gain = sum(gains) / 14
            avg_loss = sum(losses) / 14
            if avg_loss == 0: rsi_14 = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi_14 = round(100 - (100 / (1 + rs)), 1)

        # KIS 일별 차트로 이미 확보한 ch1/close는 덮어쓰지 않음
        # (DataReader prices[0]은 장 중 마지막 확정일이 어제일 수 있음)
        _kis_ch1_ok = result.get("kospi_ch1", 0) != 0
        result.update({
            "kospi_close": result["kospi_close"] if _kis_ch1_ok else round(close, 2),
            "ma5":         round(ma5, 2),
            "ma20":        round(ma20, 2),
            "ma60":        round(ma60, 2),
            "kospi_ch5":   round((close - prices[4]) / prices[4] * 100, 2) if len(prices) >= 5 and prices[4] > 0 else 0,
            "kospi_ch20":  round((close - prices[19]) / prices[19] * 100, 2) if len(prices) >= 20 and prices[19] > 0 else 0,
            "kospi_ch1":   result["kospi_ch1"] if _kis_ch1_ok else (round((close - prices[1]) / prices[1] * 100, 2) if len(prices) >= 2 and prices[1] > 0 else 0),
            "kospi_ch2":   round((close - prices[2]) / prices[2] * 100, 2) if len(prices) >= 3 and prices[2] > 0 else 0,
            "rsi_14":      rsi_14,
        })


        is_golden   = ma5 > ma20
        is_above_60 = ma20 > ma60
        above_all   = close > ma5 > ma20 > ma60
        below_all   = close < ma5 < ma20 < ma60
        reasons = []

        if above_all:
            result["aligned"] = "정배열"
            reasons.append("정배열 (현가>MA5>MA20>MA60)")
        elif below_all:
            result["aligned"] = "역배열"
            reasons.append("역배열 (현가<MA5<MA20<MA60)")
        else:
            result["aligned"] = "혼조"

        if is_golden and not above_all: reasons.append("MA5>MA20 골든크로스")
        elif not is_golden and not below_all: reasons.append("MA5<MA20 데드크로스")
        if is_above_60: reasons.append("MA20>MA60 중기 상승")
        else: reasons.append("MA20<MA60 중기 하락")

        ch5 = result["kospi_ch5"]
        if ch5 >= 2: reasons.append(f"5일 +{ch5:.1f}%↑")
        elif ch5 <= -2: reasons.append(f"5일 {ch5:.1f}%↓")

        # RSI 이유 추가
        if rsi_14 > 70:   reasons.append(f"RSI {rsi_14:.0f} 과매수")
        elif rsi_14 < 30: reasons.append(f"RSI {rsi_14:.0f} 과매도")

        kr_score = 0
        if above_all:     kr_score += 2
        elif close > ma5: kr_score += 1
        if below_all:     kr_score -= 2
        elif close < ma5: kr_score -= 1
        if is_golden:     kr_score += 1
        else:             kr_score -= 1
        if is_above_60:   kr_score += 1
        else:             kr_score -= 1

        if rsi_14 > 75:   kr_score -= 2
        elif rsi_14 > 70: kr_score -= 1
        elif rsi_14 < 25: kr_score += 2
        elif rsi_14 < 30: kr_score += 1

        # ── 단기 수익률 반영 ─────────────────────────────────────────
        ch5  = result.get("kospi_ch5", 0)
        ch1  = result.get("kospi_ch1", 0)
        ch2  = result.get("kospi_ch2", 0)
        if ch5 <= -5:    kr_score -= 2
        elif ch5 <= -2:  kr_score -= 1
        elif ch5 >= 5:   kr_score += 2
        elif ch5 >= 2:   kr_score += 1
        if ch1 <= -3:    kr_score -= 2
        elif ch1 <= -1:  kr_score -= 1
        elif ch1 >= 3:   kr_score += 1
        if ch2 <= -4:    kr_score -= 1
        elif ch2 >= 4:   kr_score += 1

        # ch1 reasons
        if abs(ch1) >= 1: reasons.append(f"당일 {ch1:+.1f}%")

        result["kr_score"] = kr_score

        if kr_score >= 3:
            result["signal"]    = "📈 매수 우위"
            result["signal_en"] = "BUY"
        elif kr_score <= -3:
            result["signal"]    = "📉 매도 우위"
            result["signal_en"] = "SELL"
        else:
            result["signal"]    = "⚖️ 관망"
            result["signal_en"] = "WATCH"

        result["reason"] = " · ".join(reasons) if reasons else "중립"

        try:
            now_m = datetime.now().month; now_y = datetime.now().year
            exp_months = [3, 6, 9, 12]
            front_m = next(m for m in exp_months if m >= now_m)
            front_y = now_y
            if front_m < now_m: front_y += 1
            fut_code = f"101W{str(front_y)[-2:]}{str(front_m).zfill(2)}"
            res_fut = requests.get(
                f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=H(tok, "FHKIF03010100"),
                params={"fid_cond_mrkt_div_code":"F","fid_input_iscd":fut_code},
                timeout=8
            )
            fut_data = res_fut.json()
            if fut_data.get("rt_cd") == "0":
                fut_price = sf(fut_data.get("output",{}).get("stck_prpr", 0))
                kospi200 = close / 5
                if fut_price > 0:
                    basis = round(fut_price - kospi200, 2)
                    result["basis"] = basis
                    if basis > 1.5:   result["basis_signal"] = f"강세(+{basis:.1f})"
                    elif basis > 0:   result["basis_signal"] = f"약강세(+{basis:.1f})"
                    elif basis > -1.5:result["basis_signal"] = f"약약세({basis:.1f})"
                    else:             result["basis_signal"] = f"약세({basis:.1f})"
                    print(f"  선물({fut_code}) {fut_price:.2f} | 베이시스 {basis:+.2f} → {result['basis_signal']}")
        except Exception as eb:
            print(f"  선물 베이시스 조회 실패: {eb}")

        print(f"  KOSPI {close:,.2f} | MA5 {ma5:,.2f} MA20 {ma20:,.2f} MA60 {ma60:,.2f} | RSI {rsi_14:.0f} | {result['aligned']} → {result['signal']}")
        print(f"  근거: {result['reason']}")

    except Exception as e:
        print(f"  시장 시그널 오류: {e}")

    return result

# ── 미국 시장 시그널 ──────────────────────────────────────────────
def fetch_us_signal() -> dict:
    result = {
        "sp500_close": 0, "sp500_ch5": 0, "sp500_ch20": 0,
        "sp500_ma5": 0,   "sp500_ma20": 0, "sp500_ma60": 0, "sp500_aligned": "혼조",
        "ndx_close": 0,   "ndx_ch5": 0,   "ndx_ch20": 0,
        "ndx_ma5": 0,     "ndx_ma20": 0,  "ndx_aligned": "혼조", "ndx_rsi": 50.0,
        "vix_close": 0,   "vix_level": "데이터없음",
        "sp500_rsi": 50.0,
        "us_signal": "⚖️ 관망", "us_signal_en": "WATCH",
        "us_reason": "데이터 없음",
    }
    # ── market_indicators.json에서 현재가/등락률 선 로드 (yfinance 중복 절감) ──
    MI_FILE = "market_indicators.json"
    try:
        if os.path.exists(MI_FILE):
            with open(MI_FILE, "r", encoding="utf-8") as _f:
                _mi = json.load(_f)
            _inds = _mi.get("indicators", {})
            # us_market.json에서 SP500/NDX/VIX 현재값 우선 로드
    except Exception:
        pass
    US_FILE = "us_market.json"
    try:
        if os.path.exists(US_FILE):
            with open(US_FILE, "r", encoding="utf-8") as _f:
                _us = json.load(_f)
            if _us.get("sp500", {}).get("close", 0) > 0:
                result["sp500_close"] = _us["sp500"]["close"]
                result["sp500_ch1"]   = _us["sp500"].get("change_pct", 0)
            if _us.get("nasdaq", {}).get("close", 0) > 0:
                result["ndx_close"] = _us["nasdaq"]["close"]
                result["ndx_ch1"]   = _us["nasdaq"].get("change_pct", 0)
            if _us.get("vix", {}).get("close", 0) > 0:
                result["vix_close"] = _us["vix"]["close"]
                result["vix_level"] = _us["vix"].get("level", "")
            print(f"  us_market.json 로드: SP500 {result['sp500_close']:,.2f} / NDX {result['ndx_close']:,.2f} / VIX {result['vix_close']:.1f}")
    except Exception as e:
        print(f"  us_market.json 로드 실패: {e}")
    try:
        now = datetime.now()
        s   = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        e_dt = now.strftime("%Y-%m-%d")   # 종료일 (except의 e와 이름충돌 방지 위해 e_dt 사용)
        scores = []; reasons = []

        for ticker, key in [("^GSPC","sp500"), ("^IXIC","ndx")]:
            try:
                t_obj = yf.Ticker(ticker)
                df = t_obj.history(start=s, end=e_dt)
                if df is None or len(df) < 5: continue
                prices = list(df["Close"].dropna())
                close = float(prices[-1])
                ma5   = sum(float(p) for p in prices[-5:])  / 5
                ma20  = sum(float(p) for p in prices[-20:]) / 20 if len(prices)>=20 else ma5
                ma60  = sum(float(p) for p in prices[-60:]) / 60 if len(prices)>=60 else ma20
                ch5   = round((float(prices[-1])-float(prices[-5]))/float(prices[-5])*100, 2) if len(prices)>=5 else 0
                ch20  = round((float(prices[-1])-float(prices[-20]))/float(prices[-20])*100, 2) if len(prices)>=20 else 0
                result[f"{key}_close"] = round(close, 2)
                result[f"{key}_ma5"]   = round(ma5, 2)
                result[f"{key}_ma20"]  = round(ma20, 2)
                result[f"{key}_ma60"]  = round(ma60, 2)
                result[f"{key}_ch5"]   = ch5
                result[f"{key}_ch20"]  = ch20
                result[f"{key}_ch1"]   = round((float(prices[-1])-float(prices[-2]))/float(prices[-2])*100, 2) if len(prices)>=2 else 0

                # ── MA 정배열 (sp500, ndx 각각) ──────────────────────
                if close > ma5 > ma20 > ma60:   result[f"{key}_aligned"] = "정배열"
                elif close < ma5 < ma20 < ma60: result[f"{key}_aligned"] = "역배열"
                else:                            result[f"{key}_aligned"] = "혼조"

                # ── RSI 계산 (sp500, ndx 각각) ───────────────────────
                if len(prices) >= 15:
                    deltas   = [float(prices[i])-float(prices[i-1]) for i in range(1, len(prices))]
                    gains    = [d if d > 0 else 0 for d in deltas[-14:]]
                    losses   = [-d if d < 0 else 0 for d in deltas[-14:]]
                    avg_gain = sum(gains)/14; avg_loss = sum(losses)/14
                    rsi_val  = 100.0 if avg_loss==0 else round(100-(100/(1+avg_gain/avg_loss)),1)
                    result[f"{key}_rsi"] = rsi_val
                    # 하위 호환: sp500_rsi 별도 유지
                    if key == "sp500":
                        result["sp500_rsi"] = rsi_val
                label = "SP500" if key=="sp500" else "NASDAQ"
                if close > ma5 and ma5 > ma20:
                    scores.append(1); reasons.append(f"{label} 상승추세")
                elif close < ma5 and ma5 < ma20:
                    scores.append(-1); reasons.append(f"{label} 하락추세")
                else:
                    scores.append(0); reasons.append(f"{label} 혼조")
            except Exception as e:
                print(f"  {ticker} 조회 오류: {e}")

        vix_close = result.get("vix_close", 0); vix_level = result.get("vix_level", "보통")  # 실패 시 us_market.json 로드값 유지(0 덮어쓰기 방지)
        try:
            vix_obj = yf.Ticker("^VIX")
            df_vix = vix_obj.history(start=s, end=e_dt)
            if df_vix is not None and len(df_vix) >= 1:
                vix_close = round(float(list(df_vix["Close"].dropna())[-1]), 2)
                if vix_close < 15:    vix_level = "과열낙관"; reasons.append(f"VIX {vix_close:.1f} 과열낙관 (조심)")
                elif vix_close < 20:  vix_level = "안정"; scores.append(1); reasons.append(f"VIX {vix_close:.1f} 안정")
                elif vix_close < 25:  vix_level = "불안"; scores.append(-1); reasons.append(f"VIX {vix_close:.1f} 불안")
                elif vix_close < 35:  vix_level = "공포"; scores.append(-1); reasons.append(f"VIX {vix_close:.1f} 공포구간")
                else:                 vix_level = "극공포"; reasons.append(f"VIX {vix_close:.1f} 극공포 (역발상주의)")
        except Exception as e:
            print(f"  VIX 조회 오류: {e}")

        result["vix_close"] = vix_close
        result["vix_level"] = vix_level

        total = sum(scores)
        if total >= 2:    result["us_signal"] = "📈 상승장";    result["us_signal_en"] = "BUY"
        elif total <= -2: result["us_signal"] = "📉 하락장";    result["us_signal_en"] = "SELL"
        elif total == 1:  result["us_signal"] = "📈 약한 상승"; result["us_signal_en"] = "BUY"
        elif total == -1: result["us_signal"] = "📉 약한 하락"; result["us_signal_en"] = "SELL"
        else:             result["us_signal"] = "⚖️ 혼조";      result["us_signal_en"] = "WATCH"

        result["us_reason"] = " · ".join(reasons) if reasons else "데이터 없음"
        print(f"  S&P500 {result['sp500_close']:,.2f} (5일{result['sp500_ch5']:+.1f}%) | NASDAQ {result['ndx_close']:,.2f} (5일{result['ndx_ch5']:+.1f}%) | VIX {vix_close:.1f} [{vix_level}] → {result['us_signal']}")

    except Exception as e:
        print(f"  미국 시장 오류: {e}")

    return result

# ── 1단계: 후보 로드 ──────────────────────────────────────────────
def load_candidates_from_kis(tok):
    """
    KIS 거래대금 순위 API로 후보 직접 조회.
    KRX 차단 시 폴백 — KRX 의존성 없음.
    KOSPI + KOSDAQ 각각 상위 30개 = 총 60개 후보.
    """
    print(f"  KIS 거래대금 순위 직접 조회 중...")
    result = []
    seen   = set()

    for mkt_code, mkt_name in [("0001", "KOSPI"), ("1001", "KOSDAQ")]:
        try:
            res = requests.get(
                f"{BASE}/uapi/domestic-stock/v1/ranking/val-part",
                headers=H(tok, "FHPST01720000"),
                params={
                    "fid_cond_mrkt_div_code":  "J",
                    "fid_cond_scr_div_code":   "20172",
                    "fid_input_iscd":          mkt_code,
                    "fid_div_cls_code":        "0",
                    "fid_blng_cls_code":       "0",
                    "fid_trgt_cls_code":       "111111111",
                    "fid_trgt_exls_cls_code":  "000000",
                    "fid_input_price_1":       "0",
                    "fid_input_price_2":       "0",
                    "fid_vol_cnt":             "0",
                    "fid_input_date_1":        "",
                },
                timeout=15
            )
            data = res.json()
            if data.get("rt_cd") != "0":
                print(f"  {mkt_name} KIS 순위 오류: {data.get('msg1','')}")
                continue
            cnt = 0
            for item in data.get("output", []):
                ticker = str(item.get("mksc_shrn_iscd", "")).zfill(6)
                name   = item.get("hts_kor_isnm", "").strip()
                if not ticker or not name or name in seen: continue
                if is_etf(name): continue
                if name.endswith("우") or name.endswith("우B") or name.endswith("우C"): continue
                seen.add(name)
                result.append({"ticker": ticker, "name": name, "market": mkt_name})
                cnt += 1
            print(f"  {mkt_name}: {cnt}종목")
            time.sleep(0.5)
        except Exception as e:
            print(f"  {mkt_name} KIS 순위 조회 실패: {e}")

    return result


def should_refresh_candidates() -> str:
    """
    종목 목록 갱신 여부 결정.
    하루 2회만 KRX 요청 (창 안에서 첫 성공 1번만 조회, 이후 캐시 재사용):
    - 오전: 08:00~09:29 (장전 실행 포함 — GitHub cron 지연으로 창을 놓치는 날 방지)
    - 오후: 15:30~16:59 (밀린 15:50 실행까지 포함)
    반환값: 'morning' | 'afternoon' | None(갱신 불필요)
    """
    now  = datetime.utcnow() + timedelta(hours=9)
    h, m = now.hour, now.minute

    if h == 8 or (h == 9 and m < 30):       return "morning"    # 08:00~09:29
    if (h == 15 and m >= 30) or h == 16:    return "afternoon"  # 15:30~16:59
    return None

def load_candidates():
    print(f"\n[1/3] 후보 {CAND_N}종목 로드 중...")

    slot = should_refresh_candidates()   # 'morning' | 'afternoon' | None

    # ── 캐시 확인 ────────────────────────────────────────────────────
    if os.path.exists(CAND_CACHE):
        try:
            with open(CAND_CACHE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            cache_date  = cache.get("date", "")
            cache_slot  = cache.get("slot", "")
            today       = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y%m%d")
            candidates  = cache.get("candidates", [])

            # 캐시 재사용 조건: 오늘 날짜 + 해당 슬롯 이미 갱신됨
            already_refreshed = (
                cache_date == today and (
                    cache_slot == slot or          # 같은 슬롯
                    (cache_slot == "afternoon") or # 오후 갱신 완료
                    (slot is None)                 # 갱신 시간대 아님
                )
            )
            if already_refreshed and candidates:
                print(f"  📦 캐시 사용 ({cache_date} {cache_slot}) → {len(candidates)}종목")
                return candidates
        except:
            pass

    # ── KRX 갱신 필요 시에만 fdr 호출 ────────────────────────────────
    if slot:
        print(f"  🔄 종목 목록 갱신 ({slot}) — KRX 조회 시작")
    rows = []
    for m in ["KOSPI","KOSDAQ"]:
        lst = None
        for attempt in range(2):
            try:
                lst = fdr.StockListing(m); lst["market"] = m
                break
            except Exception as e:
                if attempt == 0: time.sleep(2)
        if lst is None: continue
        try:
            cm = {}
            for c in lst.columns:
                cl = c.lower()
                if cl in ("symbol","code","ticker"): cm[c]="Code"
                elif cl == "name": cm[c]="Name"
                elif "marcap" in cl: cm[c]="Marcap"
            lst = lst.rename(columns=cm)
            if "Marcap" not in lst.columns:
                num = lst.select_dtypes(include="number").columns
                if len(num): lst["Marcap"] = lst[num[0]]
            lst["Marcap"] = pd.to_numeric(lst["Marcap"], errors="coerce").fillna(0)
            rows.append(lst[lst["Marcap"]>0])
        except Exception as e: print(f"  {m} 파싱 오류: {e}")

    if not rows:
        # fdr 실패 → 캐시 강제 사용
        if os.path.exists(CAND_CACHE):
            try:
                with open(CAND_CACHE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                cands = cache.get("candidates", cache if isinstance(cache, list) else [])
                print(f"  ⚠️ KRX 차단 — 캐시 강제 사용 ({len(cands)}종목)")
                return cands
            except: pass
        return []

    combined = pd.concat(rows, ignore_index=True).sort_values("Marcap", ascending=False)
    result = []; seen = set()
    for _, row in combined.iterrows():
        name   = str(row.get("Name","")).strip()
        ticker = str(row.get("Code","")).zfill(6)
        market = str(row.get("market","KOSPI"))
        if not name or not ticker or name in seen or is_etf(name): continue
        if name.endswith("우") or name.endswith("우B") or name.endswith("우C"): continue
        seen.add(name)
        result.append({"ticker":ticker,"name":name,"market":market})
        if len(result) >= CAND_N: break
    print(f"  → {len(result)}개 후보 확정")

    # 성공 시 날짜+슬롯 포함해서 캐시 저장
    try:
        with open(CAND_CACHE, "w", encoding="utf-8") as f:
            json.dump({
                "date":       (datetime.utcnow() + timedelta(hours=9)).strftime("%Y%m%d"),
                "slot":       slot or "manual",
                "candidates": result,
            }, f, ensure_ascii=False)
    except: pass

    return result

# ── 2단계: KIS 현재가 ─────────────────────────────────────────────
def fetch_price_info(tok, ticker):
    r={"per":0.,"pbr":0.,"eps":0.,"bps":0.,"roe":0.,
       "close":0.,"acml_tr_pbmn":0.,"tvol_today":0,
       "prdy_ctrt":0.,"mktcap":0.}   # ← 전일대비 등락률(ADR용) + 시가총액(대형주 판정용)
    try:
        res=requests.get(f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=H(tok,"FHKST01010100"),timeout=10,
            params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":ticker})
        o=res.json().get("output",{})
        r["close"]        = sf(o.get("stck_prpr"))
        r["acml_tr_pbmn"] = sf(o.get("acml_tr_pbmn",0))
        r["tvol_today"]   = int(r["acml_tr_pbmn"])//100000000
        r["per"]  = sf(o.get("per"))
        r["pbr"]  = sf(o.get("pbr"))
        r["eps"]  = sf(o.get("eps"))
        r["bps"]  = sf(o.get("bps"))
        r["prdy_ctrt"] = sf(o.get("prdy_ctrt"))   # 전일대비 등락률(%)
        r["mktcap"]    = sf(o.get("hts_avls", 0))  # 시가총액(억원) — 대형주 판정용
        if r["bps"]>0: r["roe"]=round(r["eps"]/r["bps"]*100,1)
    except Exception as e: print(f"    현재가오류({ticker}):{e}")
    return r

# ── 3단계: 거래대금 상위 40 (병렬) ──────────────────────────────
def select_top40(tok, candidates):
    print(f"\n[2/3] {len(candidates)}종목 거래대금 동시 조회 중...")
    enriched=[]; done_count=[0]

    def query(c):
        try: return {**c,**fetch_price_info(tok,c["ticker"])}
        except: return {**c,"tvol_today":0,"acml_tr_pbmn":0,"prdy_ctrt":0.}

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures={ex.submit(query,c):c for c in candidates}
        for f in as_completed(futures):
            enriched.append(f.result())
            done_count[0]+=1
            if done_count[0]%30==0: print(f"  {done_count[0]}/{len(candidates)} 완료...")

    print(f"  {len(enriched)}/{len(candidates)} 완료")

    # ── ADR 계산 (전체 500종목 기반) ─────────────────────────────
    valid   = [s for s in enriched if s.get("prdy_ctrt") != 0 or s.get("close",0) > 0]
    adv     = sum(1 for s in valid if s.get("prdy_ctrt", 0) > 0)
    dec     = sum(1 for s in valid if s.get("prdy_ctrt", 0) < 0)
    total_v = adv + dec
    adr_val = round(adv / total_v * 100, 1) if total_v > 0 else 50.0
    adr_data = {"adr": adr_val, "adr_advances": adv, "adr_declines": dec}
    if adr_val >= 80:   adr_data["adr_signal"] = "🟢"; adr_data["adr_desc"] = f"건강 ({adr_val:.0f}%) — 지수 상승이 진짜"
    elif adr_val >= 70: adr_data["adr_signal"] = "🟡"; adr_data["adr_desc"] = f"경계 ({adr_val:.0f}%) — 이탈 징후 점검"
    elif adr_val >= 50: adr_data["adr_signal"] = "🟠"; adr_data["adr_desc"] = f"약세 ({adr_val:.0f}%) — 속에서 썩는 중"
    else:               adr_data["adr_signal"] = "🔴"; adr_data["adr_desc"] = f"투매권 ({adr_val:.0f}%) — 바닥 근접 가능"
    print(f"  ADR 등락비율: {adr_val:.1f}% ({adv}↑ / {dec}↓) {adr_data['adr_signal']}")

    df=(pd.DataFrame(enriched).sort_values("acml_tr_pbmn",ascending=False)
        .head(TOP_N).reset_index(drop=True))
    result=[]
    for i,row in df.iterrows():
        result.append({
            "rank":i+1,"ticker":row["ticker"],"name":row["name"],"market":row["market"],
            "tvol":int(row.get("tvol_today",0)),"per":row.get("per",0.),
            "pbr":row.get("pbr",0.),"eps":row.get("eps",0.),"bps":row.get("bps",0.),
            "roe":row.get("roe",0.),"close":row.get("close",0.),
            "prdy_ctrt":row.get("prdy_ctrt",0.),   # 실시간 전일대비 등락률 (vol_char용)
            "mktcap":row.get("mktcap",0.),         # 시가총액(억원) — 대형주 판정용
        })
    print(f"\n  거래대금 상위 {len(result)}종목:")
    for r in result[:5]: print(f"    {r['rank']:2d}. {r['name']} ({r['market']}) — {r['tvol']:,}억")
    return result, adr_data

# ── 4단계: 외국인 + 기관 순매수 ───────────────────────────────────
def fetch_foreign_net(tok, ticker):
    """외국인·기관 순매수 수량 (같은 inquire-investor 응답에서 동시 추출).
    당일치가 비어있으면(장중 미집계) 최근 확정일로 폴백.
    반환: (외인수량, 기관수량, 기준) — 양수=순매수/음수=순매도/0=데이터없음, 기준='당일'|'전일'|''"""
    try:
        r = requests.get(
            f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-investor",
            headers=H(tok, "FHKST01010900"),
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker},
            timeout=8
        )
        rows = r.json().get("output", [])
        if not rows:
            return 0, 0, ""
        for i, row in enumerate(rows):
            fraw = str(row.get("frgn_ntby_qty", "0")).replace(",", "").strip()
            oraw = str(row.get("orgn_ntby_qty", "0")).replace(",", "").strip()
            fval = int(fraw) if fraw.lstrip("-").isdigit() else 0
            oval = int(oraw) if oraw.lstrip("-").isdigit() else 0
            if fval != 0 or oval != 0:
                return fval, oval, ("당일" if i == 0 else "전일")
        return 0, 0, ""
    except:
        return 0, 0, ""


# ── 5단계: EPS 추세 ───────────────────────────────────────────────
def fetch_eps_trend(tok, ticker, cur_eps):
    r={"eps_trend":"데이터없음","eps_growth":0.,"debt_ratio":None}
    try:
        res=requests.get(f"{BASE}/uapi/domestic-stock/v1/finance/financial-ratio",
            headers=H(tok,"FHKST66430300"),timeout=10,
            params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":ticker,"fid_div_cls_code":"1"})
        items=res.json().get("output",[])
        ev=[sf(x.get("eps")) for x in items[:3] if sf(x.get("eps"))!=0]
        if len(ev)>=2:
            growing=all(ev[i]>=ev[i+1] for i in range(len(ev)-1))
            turnaround = ev[1]<0 and ev[0]>=1  # 적자→흑자 전환
            if (growing or turnaround) and ev[0]>=1:
                r["eps_trend"]="상승"
                r["eps_growth"]=round((ev[0]-ev[1])/abs(ev[1])*100,1) if ev[1]!=0 else 0.
            elif ev[0]>=1: r["eps_trend"]="유지"
            else: r["eps_trend"]="부진"
        else: r["eps_trend"]="유지" if cur_eps>=1 else "부진"
        for item in items[:1]:
            v = sf(item.get("lblt_rate", 0))
            if v > 0:
                r["debt_ratio"] = round(v, 1)
                break
    except: r["eps_trend"]="유지" if cur_eps>=1 else "부진"
    return r

# ── 6단계: 20일 등락 + RSI + MACD ───────────────────────────────
def fetch_ch20(tok, ticker):
    r={"ch20":0.,"ch5":0.,"vol_trend":0.,"rsi":50.0,"macd_line":0.,"signal_line":0.,"macd_bull":None}
    try:
        now=datetime.now()
        s=(now-timedelta(days=60)).strftime("%Y%m%d"); e=now.strftime("%Y%m%d")
        res=requests.get(f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            headers=H(tok,"FHKST01010400"),timeout=10,
            params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":ticker,
                    "fid_org_adj_prc":"1","fid_period_div_code":"D",
                    "fid_input_date_1":s,"fid_input_date_2":e})
        rjson=res.json()
        items=rjson.get("output2",rjson.get("output",[]))
        prices=[sf(x.get("stck_clpr")) for x in items if sf(x.get("stck_clpr"))>0]
        if len(prices)>=20:
            r["ch20"]=round((prices[0]-prices[19])/prices[19]*100,1) if prices[19]>0 else 0.
        if len(prices)>=5:
            r["ch5"]=round((prices[0]-prices[4])/prices[4]*100,1) if prices[4]>0 else 0.
        vols=[sf(x.get("acml_vol")) for x in items]
        if len(vols)>=20:
            avg5=sum(vols[:5])/5; avgA=sum(vols[:20])/20
            r["vol_trend"]=round((avg5-avgA)/avgA*100,1) if avgA>0 else 0.
        if len(prices)>=15:
            p_asc=prices[:15][::-1]
            gains=[max(p_asc[i]-p_asc[i-1],0) for i in range(1,15)]
            losses=[max(p_asc[i-1]-p_asc[i],0) for i in range(1,15)]
            avg_gain=sum(gains)/14; avg_loss=sum(losses)/14
            if avg_loss==0: r["rsi"]=100.0
            else:
                rs=avg_gain/avg_loss
                r["rsi"]=round(100-(100/(1+rs)),1)
        if len(prices)>=35:
            p_asc=prices[:35][::-1]
            def ema(data, n):
                k=2/(n+1); e=data[0]
                for p in data[1:]: e=p*k+e*(1-k)
                return e
            ema12=ema(p_asc,12); ema26=ema(p_asc,26)
            macd_line=ema12-ema26
            macd_vals=[]
            for i in range(9,35):
                e12=ema(p_asc[:i+1],12); e26=ema(p_asc[:i+1],26)
                macd_vals.append(e12-e26)
            signal_line=ema(macd_vals,9)
            r["macd_line"]=round(macd_line,2)
            r["signal_line"]=round(signal_line,2)
            r["macd_bull"]=(macd_line>signal_line)
        else:
            r["macd_line"]=0.; r["signal_line"]=0.; r["macd_bull"]=None
    except: pass
    return r

# ── 금융업종 ──────────────────────────────────────────────────────
FINANCE_TICKERS = {
    "105560","055550","086790","316140","138930","139130","175330",
    "039490","006800","001510","071050","003540","016360","030200",
    "005940","078020","008560","001290","023150","007770","011370",
    "012510","000810","032830","088350","005830","029780",
}

# ── 시가총액 분류 + 시장 폭/대형주 국면 판정 ──────────────────────────
CAP_LARGE_MIN = 10_000   # 대형주 시총 하한 (억원) = 1조
CAP_MID_MIN   = 3_000    # 중형주 시총 하한 (억원) = 3천억

def classify_cap(mktcap: float) -> str:
    """시가총액(억원) → 대형/중형/소형/미상"""
    if not mktcap or mktcap <= 0:
        return "미상"
    if mktcap >= CAP_LARGE_MIN:
        return "대형"
    if mktcap >= CAP_MID_MIN:
        return "중형"
    return "소형"

def classify_breadth_regime(ms: dict) -> dict:
    """KOSPI 흐름 + KOSDAQ 흐름 + ADR → 시장 폭/대형주 국면 판정.
    regime ∈ 광범위강세 / 대형주차별화 / 중소형우위 / 순환조정 / 전면약세 / 혼조
    반환: {"regime","label","desc", + 근거값들}"""
    kospi_ch1  = float(ms.get("kospi_ch1", 0))
    kosdaq_ch1 = float(ms.get("kosdaq_ch1", 0))
    aligned    = ms.get("aligned", "")
    adr        = float(ms.get("adr", 50))

    idx_up   = (kospi_ch1 >= 0.3) or (aligned == "정배열")     # 지수 상승
    idx_down = (kospi_ch1 <= -0.3) or (aligned == "역배열")    # 지수 하락
    large_lead = (kospi_ch1 - kosdaq_ch1) >= 0.5               # 대형주(코스피) 우위
    small_lead = (kosdaq_ch1 - kospi_ch1) >= 0.5               # 중소형(코스닥) 우위
    broad  = adr >= 50                                          # 폭 넓음
    narrow = adr < 35                                           # 폭 좁음

    if idx_up and narrow and large_lead:
        r, lbl, d = "대형주차별화", "대형주차별화 🔵", "지수↑·폭 좁음·대형주 쏠림 — 대형주만"
    elif idx_up and broad:
        r, lbl, d = "광범위강세", "광범위강세 🟢", "지수↑·폭 넓음 — 전 종목 건강"
    elif small_lead and kosdaq_ch1 > 0:
        r, lbl, d = "중소형우위", "중소형우위 🟡", "코스닥>코스피 — 중소형/테마 장"
    elif idx_down and narrow:
        r, lbl, d = "전면약세", "전면약세 🔴", "지수↓·폭 좁음 — 신규매수 보수적"
    elif idx_down and broad:
        r, lbl, d = "순환조정", "순환조정 🟠", "대형주 쉬고 폭은 생존 — 순환매"
    else:
        r, lbl, d = "혼조", "혼조 ⚪", "뚜렷한 쏠림 없음"

    return {"regime": r, "label": lbl, "desc": d,
            "kospi_ch1": round(kospi_ch1, 2), "kosdaq_ch1": round(kosdaq_ch1, 2),
            "adr": round(adr, 1)}

def calc_entry_score(d: dict, kospi_ch1: float = 0.0, adr: float = 50.0) -> dict:
    """진입 타이밍 점수 (0~10점). 지금 사기 좋은 타이밍인지 종합 평가.
    kospi_ch1: KOSPI 당일 등락률 (시장 전체 방향 반영)
    adr: ADR 등락비율 (시장 폭 반영)
    """
    score = 0
    grade       = d.get("grade", "F")
    rsi         = float(d.get("rsi", 50))
    ch5         = float(d.get("ch5", 0))
    frgn_net    = int(d.get("frgn_net", 0) or 0)
    vol_char    = d.get("vol_char", "")
    rank_change = d.get("rank_change")

    # 신규 진입 여부 (타이밍의 핵심)
    if rank_change is None:                                          s = 3
    elif isinstance(rank_change,(int,float)) and rank_change >= 10: s = 2
    elif isinstance(rank_change,(int,float)) and rank_change >= 3:  s = 1
    elif isinstance(rank_change,(int,float)) and rank_change < 0:   s = -1
    else:                                                            s = 0
    score += s

    # RSI
    if rsi < 45:    s = 2
    elif rsi < 55:  s = 1
    elif rsi < 65:  s = 1   # 55~65 안전구간도 +1
    elif rsi < 70:  s = -1
    else:           s = -2
    score += s

    # 5일 수익률 (이미 많이 올랐나?)
    if ch5 < 0:     s = 2
    elif ch5 < 5:   s = 1
    elif ch5 < 10:  s = 0
    elif ch5 < 20:  s = -1
    else:           s = -2
    score += s

    # 등급
    s = {"A": 2, "B": 1, "C": 0, "D": -2}.get(grade, -3)
    score += s

    # 외인 순매수
    # frgn_net=0은 "데이터 없음"일 수 있음
    # 시장이 하락 중이면(ADR≤40 or KOSPI -1% 이하) 외인 매도 가정 → 페널티
    if frgn_net > 100_000:    s = 2
    elif frgn_net > 0:        s = 1
    elif frgn_net < -100_000: s = -2
    elif frgn_net < 0:        s = -1
    else:  # frgn_net == 0 (데이터 없음 또는 실제 0)
        s = -1 if (adr <= 40 or kospi_ch1 <= -1.0) else 0
    score += s

    # 거래성격
    if "매수주도" in vol_char:    s = 1
    elif "매도주도" in vol_char:  s = -1
    else:                         s = 0
    score += s

    # ── 시장 상황 패널티 ──────────────────────────────────────────────
    # KOSPI 당일 -2% 이하: 전체 시장 하락 구간 → -2점
    if kospi_ch1 <= -2.0:   score -= 2
    elif kospi_ch1 <= -1.0: score -= 1

    # ADR 30% 이하: 투매 구간 → -2점 / 40% 이하: 하락 우세 → -1점
    if adr <= 30:   score -= 2
    elif adr <= 40: score -= 1

    score = max(0, min(10, score))

    # D/F등급은 진입점수 표시 안 함
    if grade in ("D", "F"):
        return {"entry_score": score, "entry_stars": "–",
                "entry_label": "등급 부적격"}

    if score >= 8:    stars, label = "★★★★★", "지금 진입"
    elif score >= 6:  stars, label = "★★★★",  "좋은 타이밍"
    elif score >= 4:  stars, label = "★★★",   "보통"
    elif score >= 2:  stars, label = "★★",    "주의"
    else:             stars, label = "★",     "늦음"

    return {"entry_score": score, "entry_stars": stars, "entry_label": label}


def judge(d):
    roe=d.get("roe",0) or 0; per=d.get("per",0) or 0
    eps=d.get("eps",0) or 0; eps_trend=d.get("eps_trend","")
    debt=d.get("debt_ratio",None); ticker=d.get("ticker","")

    # 저평가 성장주 예외: ROE≥15% + EPS상승이면 PER 60배까지 허용
    is_growth_exception = (roe >= 15 and eps_trend == "상승")
    per_limit = 60 if is_growth_exception else 35

    c1=roe>=15; c2=0<per<=per_limit; c3=eps>=1; c4=eps_trend=="상승"
    is_finance = ticker in FINANCE_TICKERS
    c5 = True if is_finance else (debt is not None and debt <= 200)
    score=sum([c1,c2,c3,c4,c5])
    if score==5: grade="A"
    elif score==4: grade="B"
    elif score==3: grade="C"
    elif score==2: grade="D"
    else: grade="F"
    recommended = score >= 4
    return {"roe_ok":c1,"per_ok":c2,"eps_ok":c3,"eps_up":c4,"debt_ok":c5,
            "is_finance":is_finance,"score":score,"grade":grade,"recommended":recommended,
            "is_growth_exception":is_growth_exception,"per_limit":per_limit}

def send_discord(results, date, recs, market_signal):
    pass

def main():
    print("╔══════════════════════════════════╗")
    print("║   StockPilot KR  KIS 스크리닝   ║")
    print("╚══════════════════════════════════╝")
    if not APP_KEY or not APP_SECRET:
        print("❌ KIS_APP_KEY / KIS_APP_SECRET 없음"); return

    now_utc = datetime.utcnow()
    now_kst = now_utc + timedelta(hours=9)
    date = now_kst.strftime("%Y%m%d")
    print(f"  기준일: {date} ({now_kst.strftime('%H:%M')} KST)")
    print(f"  등급: ROE≥15% PER≤35배(저평가 성장주 60배) EPS≥1 EPS상승 부채비율≤200% → 5개 기준 / 4개이상=추천")

    # ── 이전 거래일 순위 로드 (같은 날 여러번 실행해도 기준 고정) ──
    prev_ranks = {}
    try:
        if os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE, "r", encoding="utf-8") as pf:
                prev_data = json.load(pf)
            prev_date = prev_data.get("date", "")

            if prev_date != date:
                # 날짜가 바뀐 경우 → 어제 결과가 새 기준
                for s in prev_data.get("results", []):
                    prev_ranks[s["ticker"]] = s.get("rank", 0)
                print(f"  📊 이전 거래일 순위 로드: {len(prev_ranks)}종목 (기준일: {prev_date})")
            else:
                # 같은 날 재실행 → 저장된 기준 순위 사용 (어제 기준 유지)
                prev_ranks = prev_data.get("reference_ranks", {})
                print(f"  📊 기준 순위 유지: {len(prev_ranks)}종목 (기준일: {prev_data.get('reference_date', prev_date)})")
    except Exception as e:
        print(f"  이전 순위 로드 실패: {e}")

    print("\n[0] KIS 토큰 발급 중...")
    try: tok=get_token()
    except Exception as e: print(f"❌ 토큰 실패: {e}"); return

    print("\n[시장] KOSPI MA5/MA20/MA60 분석 중...")
    market_signal = fetch_market_signal(tok)

    print("\n[미장] S&P500 / NASDAQ 분석 중...")
    us_signal = fetch_us_signal()
    market_signal["us"] = us_signal

    candidates=load_candidates()
    if not candidates:
        print("  ⚠️ KRX 조회 실패 — KIS 거래대금 순위로 폴백")
        candidates = load_candidates_from_kis(tok)
    if not candidates:
        # 마지막 수단: 이전 results.json 재사용
        if os.path.exists(RESULTS_FILE):
            try:
                with open(RESULTS_FILE, "r", encoding="utf-8") as pf:
                    prev = json.load(pf)
                prev_results = prev.get("results", [])
                if prev_results:
                    candidates = [{"ticker": r["ticker"], "name": r["name"],
                                   "market": r.get("market","KOSPI")} for r in prev_results]
                    print(f"  ⚠️ KIS도 실패 — 이전 결과 {len(candidates)}종목 재사용")
            except: pass
    if not candidates: print("❌ 후보 로드 최종 실패"); return

    top40, adr_data = select_top40(tok, candidates)
    if not top40: print("❌ 거래대금 계산 실패"); return

    # market_signal에 ADR 추가 (VKOSPI는 신뢰할 데이터 소스가 없어 제거됨)
    market_signal.update(adr_data)

    # ── 시장 폭/대형주 국면 판정 (KOSPI+KOSDAQ+ADR 종합) ──────────────
    breadth = classify_breadth_regime(market_signal)
    market_signal["breadth"] = breadth
    print(f"  📐 시장 폭 국면: {breadth['label']} — {breadth['desc']}")

    # ── 순위 변동 계산 ─────────────────────────────────────────────
    for item in top40:
        prev_rank = prev_ranks.get(item["ticker"], 0)
        if prev_rank == 0:
            item["rank_change"] = None        # 신규 진입
        else:
            item["rank_change"] = prev_rank - item["rank"]  # 양수=상승, 음수=하락, 0=유지

    print(f"\n[3/3] {len(top40)}종목 상세 분석 중...\n")
    results=[]; ge_map={"A":"🟢","B":"🔵","C":"🟡","D":"🔴"}
    for t in top40:
        tk=t["ticker"]
        rc = t.get("rank_change")
        rc_str = " NEW" if rc is None else (f" ▲{rc}" if rc > 0 else (f" ▼{abs(rc)}" if rc < 0 else " →"))
        print(f"  [{t['rank']:2d}]{rc_str:6s} {t['name']:14s} ({tk})",end=" ... ",flush=True)
        try:
            eps_tr = fetch_eps_trend(tok,tk,t.get("eps",0))
            price  = fetch_ch20(tok,tk)
            frgn_net, orgn_net, frgn_basis = fetch_foreign_net(tok, tk)
            time.sleep(0.2)

            data={**t,**eps_tr,**price,"frgn_net":frgn_net,"orgn_net":orgn_net,"frgn_basis":frgn_basis}
            f=judge(data)
            data.update({"filters":f,"grade":f["grade"],"score":f["score"],"recommended":f["recommended"]})
            # vol_char: prdy_ctrt(실시간 등락률) 기반 거래성격
            ch1_rt = float(t.get("prdy_ctrt", 0))
            if ch1_rt >= 2.0:    vc = "매수주도 🟢"
            elif ch1_rt >= 0.3:  vc = "상승동반 🟡"
            elif ch1_rt <= -2.0: vc = "매도주도 🔴"
            elif ch1_rt <= -0.3: vc = "하락동반 🟠"
            else:                vc = "혼조 ⚪"
            data["vol_char"] = vc
            data["ch1"]      = ch1_rt
            data["cap_class"] = classify_cap(float(data.get("mktcap", 0) or 0))  # 대형/중형/소형/미상

            # 진입 타이밍 점수 계산 (시장 상황 반영)
            _kospi_ch1 = float(market_signal.get("kospi_ch1", 0))
            _adr       = float(adr_data.get("adr", 50))
            entry = calc_entry_score(data, kospi_ch1=_kospi_ch1, adr=_adr)
            data.update(entry)

            results.append(data)

            debt_r = data.get("debt_ratio",None)
            debt_str = f"  부채:{debt_r:.0f}%{'✅' if f['debt_ok'] else '❌'}" if debt_r is not None else "  부채:-"
            print(
                f"{ge_map.get(f['grade'],'⚪')}{f['grade']}등급({f['score']}/5)"
                f"  ROE:{t.get('roe',0):.1f}%{'✅' if f['roe_ok'] else '❌'}"
                f"  PER:{t.get('per',0):.1f}{'✅' if f['per_ok'] else '❌'}"
                f"  EPS:{t.get('eps',0):,.0f}({eps_tr['eps_trend']}){'✅' if f['eps_ok'] and f['eps_up'] else '❌'}"
                f"{debt_str}  5일:{price.get('ch5',0):+.1f}%  20일:{price.get('ch20',0):+.1f}%"
                f"  [{vc}]"
            )
        except Exception: print("오류"); traceback.print_exc()
        time.sleep(0.3)

    # ── 후보군 전체 수급 요약 (top40 묶어서 자금 쏠림 파악) ──────────
    _vc = lambda r: r.get("vol_char", "")
    pool_buy   = sum(1 for r in results if "매수주도" in _vc(r))
    pool_sell  = sum(1 for r in results if "매도주도" in _vc(r))
    pool_up    = sum(1 for r in results if "상승동반" in _vc(r))
    pool_down  = sum(1 for r in results if "하락동반" in _vc(r))
    pool_fpos  = sum(1 for r in results if int(r.get("frgn_net", 0) or 0) > 0)
    pool_fneg  = sum(1 for r in results if int(r.get("frgn_net", 0) or 0) < 0)
    pool_opos  = sum(1 for r in results if int(r.get("orgn_net", 0) or 0) > 0)
    pool_oneg  = sum(1 for r in results if int(r.get("orgn_net", 0) or 0) < 0)
    pool_large = sum(1 for r in results if r.get("cap_class") == "대형")
    market_signal["pool_flow"] = {
        "total": len(results), "buy_led": pool_buy, "sell_led": pool_sell,
        "up_follow": pool_up, "down_follow": pool_down,
        "frgn_pos": pool_fpos, "frgn_neg": pool_fneg,
        "orgn_pos": pool_opos, "orgn_neg": pool_oneg, "large_cap": pool_large,
    }
    _flow = "매수우위" if pool_buy > pool_sell else ("매도우위" if pool_sell > pool_buy else "중립")
    _frgn = "순매수우위" if pool_fpos > pool_fneg else ("순매도우위" if pool_fneg > pool_fpos else "중립")
    _orgn = "순매수우위" if pool_opos > pool_oneg else ("순매도우위" if pool_oneg > pool_opos else "중립")
    print(f"\n  🌊 후보군 수급: 매수주도 {pool_buy} / 매도주도 {pool_sell} ({_flow}) | "
          f"외인 {_frgn}({pool_fpos}↑/{pool_fneg}↓) | 기관 {_orgn}({pool_opos}↑/{pool_oneg}↓) | 대형주 {pool_large}/{len(results)}")
    _fx = market_signal.get("usdkrw", 0)
    if _fx:
        print(f"  💱 원/달러 {_fx:,.1f}원 (당일 {market_signal.get('usdkrw_ch1',0):+.2f}% / 5일 {market_signal.get('usdkrw_ch5',0):+.2f}%)"
              + ("  ⚠️ 환율 급등 — 외인 이탈 주의" if market_signal.get('usdkrw_ch1',0) >= 1.0 else ""))

    recs=[r for r in results if r.get("recommended")]
    print(f"\n{'─'*70}")
    print(f"  시장: {market_signal['signal']} | {market_signal['reason']}")
    print(f"  분석:{len(results)}종목  추천(A·B):{len(recs)}종목")
    for r in recs:
        print(f"  {ge_map.get(r['grade'],'⚪')}{r['grade']}등급 – {r['name']} ({r['market']})"
              f"  ROE {r.get('roe',0):.1f}%  PER {r.get('per',0):.1f}배"
              f"  EPS {r.get('eps',0):,.0f}원({r.get('eps_trend','?')})")

    # reference_ranks: 날짜 바뀔 때만 갱신 (같은 날 재실행해도 기준 고정)
    try:
        if os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE, "r", encoding="utf-8") as pf:
                _pd = json.load(pf)
            if _pd.get("date", "") != date:
                # 날짜 바뀜 → 어제 결과를 새 기준으로
                reference_ranks = {s["ticker"]: s.get("rank", 0) for s in _pd.get("results", [])}
                reference_date  = _pd.get("date", "")
            else:
                # 같은 날 → 기존 기준 유지
                reference_ranks = _pd.get("reference_ranks", {})
                reference_date  = _pd.get("reference_date", date)
        else:
            reference_ranks = {}
            reference_date  = date
    except:
        reference_ranks = {}
        reference_date  = date

    json.dump({
        "date":            date,
        "generated_at":    now_kst.isoformat(),
        "reference_date":  reference_date,
        "reference_ranks": reference_ranks,
        "total":           len(results),
        "market_signal":   market_signal,
        "results":         results,
        "recommended":     recs,
    }, open(RESULTS_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=2, default=str)
    print("\n  💾 results.json 저장 완료")

    # ── 시장 흐름 히스토리 저장 (최근 60거래일) ──────────────────────
    HISTORY_FILE = "market_history.json"
    try:
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        today_entry = {
            "date":      date,
            "kospi":     market_signal.get("kospi_close", 0),
            "kosdaq":    market_signal.get("kosdaq_close", 0),
            "adr":       adr_data.get("adr", 50),
            "rsi":       round(float(market_signal.get("rsi_14", 50)), 1),
            "vix":       round(float(market_signal.get("us", {}).get("vix_close", 0)), 1),
            "kospi_ch1": round(float(market_signal.get("kospi_ch1", 0)), 2),
        }
        # 같은 날이면 덮어쓰기, 새 날이면 추가
        if history and history[-1]["date"] == date:
            history[-1] = today_entry
        else:
            history.append(today_entry)
        history = history[-60:]   # 최근 60거래일만 유지
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        print("  💾 market_history.json 저장 완료")
    except Exception as e:
        print(f"  ⚠️ 히스토리 저장 실패: {e}")

    send_discord(results, date, recs, market_signal)
    print("\n✅ 완료!")

if __name__=="__main__": main()
