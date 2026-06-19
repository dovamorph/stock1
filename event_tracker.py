#!/usr/bin/env python3
"""
event_tracker.py — 이벤트 리스크 트래커
레이어1: 예정 이벤트 캘린더 (날짜 기반, 수급 영향 설명)
레이어2: 뉴스 악재 누적 트래커 (섹터별 호재/악재 카운트)

[9개 섹터 체계]
반도체·AI / 건설 / 방산 / 전력·에너지 / 로봇 / 금융 / 우주ETF / 양자컴퓨팅 / 전체시장

[2단계 분류]
1) geo_analysis.py가 이미 분류한 5대 카테고리(geopolitical/macro/disaster/trade/policy)의
   level(안전/주의/경고/위험)을 9개 섹터에 거시적으로 매핑 (전체시장/방산/건설/금융 등)
2) SECTOR_KEYWORDS로 헤드라인별 구체적 테마(반도체·AI/우주ETF/전력·에너지/양자컴퓨팅 등) 태깅
   — 영문 키워드 대거 포함 (geo_analysis 수집 뉴스의 절반 이상이 영문)
"""
import os, json, datetime, re
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

SECTORS = ["반도체·AI", "건설", "방산", "전력·에너지", "로봇", "금융", "우주ETF", "양자컴퓨팅", "전체시장"]

# ── 섹터 키워드 매핑 (구체적 테마 태깅용) ────────────────────────────
SECTOR_KEYWORDS = {
    "반도체·AI": {
        "positive": [
            "HBM", "메모리 가격 상승", "AI 서버 수요", "엔비디아 호실적",
            "반도체 수출 증가", "D램 강세", "TSMC 호조", "반도체 수주",
            "반도체 슈퍼사이클", "엔비디아 실적", "nvidia 호실적",
            # 영문/구체 키워드
            "Nvidia", "TSMC", "chipmaker", "chip price", "AI chip",
            "semiconductor demand", "memory price", "AI data center",
            "OpenAI", "ChatGPT", "LLM", "generative AI",
            # AI보안
            "cybersecurity", "AI security", "정보보호", "사이버보안", "보안 솔루션"
        ],
        "negative": [
            "브로드컴", "broadcom", "Broadcom", "반도체 제재", "메모리 가격 하락",
            "반도체 재고", "반도체 수출 규제", "반도체 쇼크", "반도체 위기",
            "엔비디아 실망", "마이크론 부진", "인텔 실망",
            # 영문/구체 키워드
            "chip export ban", "chip restriction", "semiconductor sanction",
            "AI bubble", "tech selloff", "tech fears",
            # AI 규제/차단 (모델 접근 차단·금지 뉴스는 악재)
            "AI regulation", "AI ban", "block AI", "AI export control",
            "사이버 공격", "데이터 유출", "랜섬웨어", "data breach", "cyberattack"
        ]
    },
    "건설": {
        "positive": [
            "재건", "중동 재건", "인프라 투자", "건설 수주",
            "해외 건설", "전후 복구",
            "reconstruction", "infrastructure investment"
        ],
        "negative": [
            "전쟁 확대", "PF 부실", "건설 경기 침체", "부동산 위기",
            "부동산 PF", "건설사 부도", "공사 중단", "분쟁 격화",
            "real estate crisis", "construction halt"
        ]
    },
    "방산": {
        "positive": [
            "전쟁 확대", "긴장 고조", "무기 수출", "방산 수주",
            "군비 증강", "분쟁 격화", "군사 충돌", "교전", "공습",
            # 영문 — 현재 이란/이스라엘/우크라이나 분쟁 핵심 키워드
            # 국가명 단독("Iran","Israel" 등) 및 'war' 단독은 무관 기사/무역전쟁에도
            # 걸리므로 제외하고, 명확한 군사 충돌 표현만 사용
            "warfare", "military strike", "airstrike", "missile attack",
            "armed conflict", "ceasefire collapse", "military conflict",
            "military exercises", "troops deployed", "naval blockade",
            "weapons export", "defense spending", "arms deal"
        ],
        "negative": [
            "휴전", "평화 협정", "군비 축소", "방산 예산 삭감",
            "종전", "평화 협상", "정전 합의",
            "peace talks", "ceasefire agreement", "defense budget cut",
            # 긴장 완화/방향 전환 신호
            "타격 취소", "긴장 완화", "확전 자제", "철군", "공격 중단",
            "cancels strike", "strike canceled", "calls off",
            "de-escalation", "tensions ease", "truce"
        ]
    },
    "전력·에너지": {
        "positive": [
            # 전력전선
            "AI 데이터센터", "전력 수요", "에너지 전환", "송전 투자",
            "전선 수주", "케이블 수요", "전력망 투자",
            "power grid investment", "AI data center power",
            # 원전
            "원전 확대", "원전 수출", "SMR", "소형모듈원자로", "원자력 수출",
            "nuclear export", "nuclear power expansion",
            # 태양광/대체에너지
            "태양광 수출", "재생에너지 투자", "ESS 수주", "풍력 발전",
            "solar export", "renewable energy investment",
            # 유가 (전력원가 영향)
            "유가 상승", "oil price surge"
        ],
        "negative": [
            "전력 규제", "에너지 가격 하락", "원전 반대", "탈원전",
            "nuclear opposition", "renewable subsidy cut",
            "태양광 관세", "solar tariff"
        ]
    },
    "로봇": {
        "positive": [
            "피지컬 AI", "로봇 수요", "자동화", "엔비디아 로봇",
            "로보틱스", "젠슨황 로봇", "AI 로봇", "휴머노이드",
            "humanoid robot", "robotics demand", "physical AI"
        ],
        "negative": [
            "로봇 규제", "자동화 제한", "로봇 거품", "로봇 실망",
            "robot regulation"
        ]
    },
    "금융": {
        "positive": [
            "금리 인하", "경기 회복", "금융 실적 호조", "배당 증가",
            "Fed 피봇", "통화완화", "기준금리 인하",
            "rate cut", "Fed pivot", "monetary easing"
        ],
        "negative": [
            "금리 인상", "신용 위기", "부실 대출", "금융 규제",
            "Fed 긴축", "뱅크런", "기준금리 인상",
            "rate hike", "Fed tightening", "bank run", "credit crisis"
        ]
    },
    "우주ETF": {
        "positive": [
            "스페이스X 성공", "우주 개발", "스타링크", "발사 성공",
            "NASA", "우주 수주", "SPCX",
            "SpaceX", "Starlink", "rocket launch", "Artemis",
            "space stock", "stock market blast-off"
        ],
        "negative": [
            "발사 실패", "우주 규제", "스페이스X 문제",
            "launch failure", "SpaceX lawsuit", "rocket explosion"
        ]
    },
    "양자컴퓨팅": {
        "positive": [
            "양자컴퓨터", "양자컴퓨팅", "퀀텀", "양자 기술 개발",
            "quantum computing", "quantum chip", "quantum breakthrough",
            "quantum supremacy"
        ],
        "negative": [
            "양자 기술 실패", "quantum setback"
        ]
    },
    "전체시장": {
        "positive": [
            "금리 인하", "경기 연착륙", "무역 협상 타결",
            "인플레이션 둔화", "외국인 순매수", "증시 반등",
            "trade deal", "soft landing", "inflation cooling",
            # 지정학 긴장 완화 (시장 전체 호재)
            "타격 취소", "긴장 완화", "휴전", "종전",
            "cancels strike", "de-escalation", "tensions ease", "truce"
        ],
        "negative": [
            "전쟁 확대", "금융위기", "경기침체", "Fed 긴축",
            "외국인 대규모 매도", "패닉셀", "블랙먼데이",
            "중동 전쟁", "이란 핵", "이란 제재", "공급망 위기",
            # 관세/무역분쟁 (트럼프 관세 2.0 등)
            "관세", "트럼프 관세", "무역법 301조", "강제노동 관세", "US tariff",
            "tariff", "trade war", "trade tension", "Trump tariff",
            # 지정학 일반
            "financial crisis", "recession", "supply chain crisis",
            "stock market jitters", "market selloff",
            "생산자물가", "producer price", "인플레이션 쇼크", "inflation shock"
        ]
    }
}

# ── geo_analysis.py 5대 카테고리 → 9개 섹터 매핑 ─────────────────────
# level별 점수: 위험=-3, 경고=-2, 주의=-1, 안전=0
GEO_LEVEL_SCORE = {"위험": -3, "경고": -2, "주의": -1, "안전": 0}

# 각 geo 카테고리가 영향을 주는 섹터와 가중치
GEO_CATEGORY_SECTOR_MAP = {
    "geopolitical": {  # 지정학적 충돌 → 방산(+), 건설(-), 전체시장(-)
        "방산":     -1.0,  # 위험도가 음수(위험=-3)이므로 -1.0을 곱하면 방산은 +3 (호재)
        "건설":      0.5,  # 동일하게 위험도가 음수이므로 0.5를 곱하면 건설은 -1.5 (악재)
        "전체시장":   1.0,  # 위험도가 음수이므로 1.0을 곱하면 전체시장은 -3 (악재)
    },
    "macro": {  # 거시경제 → 금융, 전체시장 (위험도 높을수록 악재)
        "금융":    1.0,
        "전체시장": 1.0,
    },
    "disaster": {  # 자연재해 → 전체시장, 건설 (위험도 높을수록 악재)
        "전체시장": 1.0,
        "건설":    0.5,
    },
    "trade": {  # 무역분쟁/관세 → 전체시장(-), 반도체·AI(-) (위험도 높을수록 악재)
        "전체시장":  1.0,
        "반도체·AI": 0.5,
    },
    "policy": {  # 정책/규제 → 전체시장 (위험도 높을수록 악재)
        "전체시장": 1.0,
    },
}

# ── 예정 이벤트 캘린더 ────────────────────────────────────────────────
# [관리 방법]
#  · 지난 이벤트는 화면에서 자동으로 숨겨짐(D-day 필터). 목록에 남겨둬도 되지만 깔끔하게 지워도 됨.
#  · 새 이벤트는 미래 날짜로 추가. date는 "YYYY-MM-DD" 형식.
#  · "result_text"(선택): 이벤트 당일에 결과를 보여주고 싶을 때 한 줄 적으면
#    그날 화면에 "✅ 결과: ..."로 표시됨 (없으면 예고 문구만 표시). 결과는 직접 확인해서 기입.
CALENDAR_EVENTS = [
    {
        "id": "nvidia_q2_2026",
        "name": "NVIDIA 2분기 실적",
        "date": "2026-06-25",
        "type": "earnings",
        "risk_level": "high",
        "description": "AI 수요 확인의 가늠자. 어닝 서프라이즈 시 반도체 전체 랠리. 실망 시 AI테마 전반 조정.",
        "benefit_text": "호실적: 반도체·AI ▲▲▲  소부장 ▲▲",
        "damage_text": "실망: 반도체·AI ▼▼  AI테마 ▼▼",
        "result_text": "",   # 당일에 결과 기입 시 화면에 "✅ 결과: ..." 표시 (예: "어닝 서프라이즈, 가이던스 상향")
        "sector_impacts": {"반도체·AI": 3, "로봇": 1, "전력·에너지": 1}
    },
]

_WORD_CACHE = {}
def _kw_matches(kw: str, text_lower: str, text_orig: str) -> bool:
    """키워드 매칭. 영문(라틴 문자)은 단어 경계(\\b)로 매칭해
    'war'가 'warns'/'warship'/'warning'에 잘못 걸리는 부분문자열 오매칭을 막는다.
    한글 키워드는 조사가 붙는 특성상(전쟁이/전쟁을) 부분 매칭을 유지한다."""
    kl = kw.lower()
    # 라틴 알파벳을 포함하면 영문 키워드로 보고 단어 경계 매칭
    if any('a' <= c <= 'z' for c in kl):
        rx = _WORD_CACHE.get(kl)
        if rx is None:
            rx = re.compile(r'\b' + re.escape(kl) + r'\b')
            _WORD_CACHE[kl] = rx
        return rx.search(text_lower) is not None
    # 한글 등 비라틴 키워드는 부분 문자열 매칭
    return kl in text_lower or kw in text_orig

def classify_news(headline: str, content: str = "") -> dict:
    """키워드 기반 섹터별 호재/악재 분류 (구체적 테마 태깅).
    한 헤드라인이 여러 키워드에 매칭돼도 섹터별 점수는 ±1로 캡한다."""
    text_lower = (headline + " " + content).lower()
    text_orig  = headline + " " + content
    impacts = {}
    for sector, kws in SECTOR_KEYWORDS.items():
        score = 0
        for kw in kws["positive"]:
            if _kw_matches(kw, text_lower, text_orig):
                score += 1
        for kw in kws["negative"]:
            if _kw_matches(kw, text_lower, text_orig):
                score -= 1
        if score > 0:
            impacts[sector] = 1
        elif score < 0:
            impacts[sector] = -1
    return impacts

def classify_geo_categories(categories: dict) -> dict:
    """geo_analysis.py의 5대 카테고리 결과를 9개 섹터에 거시 매핑.
    level(안전/주의/경고/위험)에 따라 가중치 적용한 점수 반환."""
    impacts = {}
    for cat_name, cat_data in categories.items():
        level = cat_data.get("level", "안전")
        base_score = GEO_LEVEL_SCORE.get(level, 0)
        if base_score == 0:
            continue
        sector_map = GEO_CATEGORY_SECTOR_MAP.get(cat_name, {})
        for sector, weight in sector_map.items():
            # weight가 음수면 위험할수록 호재(방산처럼) → 부호 반전
            score = base_score * weight
            score = round(score)
            if score != 0:
                impacts[sector] = impacts.get(sector, 0) + score
    return impacts

def load_geo_result():
    """geo_result.json 전체 로드 (헤드라인 리스트 + categories 둘 다 필요)"""
    GEO_FILE = "geo_result.json"
    if not os.path.exists(GEO_FILE):
        return [], {}
    try:
        with open(GEO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [{"title": h} if isinstance(h, str) else h for h in data], {}

        categories = data.get("categories", {})

        # geo_analysis.py 실제 저장 구조: all_headlines (전체), news_sample (샘플)
        headlines = data.get("all_headlines", [])
        if headlines:
            return [{"title": h} for h in headlines], categories

        # 하위 호환: 구버전 키
        for key in ("events", "news", "items", "news_sample"):
            val = data.get(key, [])
            if val:
                if isinstance(val[0], str):
                    return [{"title": h} for h in val], categories
                return val, categories
        return [], categories
    except Exception:
        return [], {}

def load_event_history() -> list:
    RISK_FILE = "sector_risk.json"
    if not os.path.exists(RISK_FILE):
        return []
    try:
        with open(RISK_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("event_history", [])
    except:
        return []

def main():
    now   = datetime.datetime.now(KST)
    date  = now.strftime("%Y%m%d")
    today = now.date()

    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 이벤트 트래커  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}")

    # ── 캘린더 D-day 계산 (오늘 D-0 ~ 미래 D-30만 표시, 지난 이벤트는 숨김) ──
    # d_day > 0: 미래(D-N), d_day == 0: 오늘(당일), d_day < 0: 과거(숨김)
    # 지난 이벤트는 화면에서 제거. 단, 당일(d_day==0)이고 result_text가 있으면 결과 표시.
    calendar = []
    for ev in CALENDAR_EVENTS:
        try:
            ev_date = datetime.date.fromisoformat(ev["date"])
            d_day   = (ev_date - today).days
            if 0 <= d_day <= 30:
                calendar.append({**ev, "d_day": d_day})
        except:
            pass
    calendar.sort(key=lambda x: x["d_day"])

    print(f"\n  [캘린더] 예정 이벤트: {len(calendar)}개")
    for ev in calendar:
        tag = "D-DAY" if ev["d_day"] == 0 else f"D-{ev['d_day']}"
        # 당일이고 결과(result_text)가 있으면 결과를, 없으면 예정 표시
        if ev["d_day"] == 0 and ev.get("result_text"):
            print(f"    {tag:6s} {ev['name']} ({ev['date']}) → ✅ 결과: {ev['result_text']}")
        else:
            print(f"    {tag:6s} {ev['name']} ({ev['date']})")

    # ── 뉴스 분류 ────────────────────────────────────────────────────
    geo_news, geo_categories = load_geo_result()
    history = load_event_history()

    today_events = []

    # 1) 헤드라인별 키워드 테마 태깅
    for item in geo_news:
        headline = item.get("title", item.get("headline", item.get("summary", "")))
        content  = item.get("content", item.get("body", ""))
        if not headline:
            continue
        impacts = classify_news(headline, content)
        if impacts:
            today_events.append({
                "date":     date,
                "headline": headline[:100],
                "impacts":  impacts,
                "source":   item.get("source", "keyword"),
            })

    # 2) geo_analysis 5대 카테고리 → 9개 섹터 거시 매핑 (1건으로 추가)
    geo_impacts = classify_geo_categories(geo_categories)
    if geo_impacts:
        # 가장 위험도 높은 카테고리의 summary를 헤드라인으로 사용
        worst_summary = ""
        worst_level   = "안전"
        level_rank    = {"위험": 3, "경고": 2, "주의": 1, "안전": 0}
        for cat_name, cat_data in geo_categories.items():
            if level_rank.get(cat_data.get("level","안전"), 0) > level_rank.get(worst_level, 0):
                worst_level   = cat_data.get("level", "안전")
                worst_summary = cat_data.get("summary", "")
        today_events.append({
            "date":     date,
            "headline": f"[거시분석] {worst_summary[:80]}" if worst_summary else "[거시분석] 카테고리 리스크 반영",
            "impacts":  geo_impacts,
            "source":   "geo_categories",
        })

    print(f"\n  [분류] 뉴스 {len(geo_news)}건 → 키워드 테마 영향 {len([e for e in today_events if e['source']!='geo_categories'])}건 감지")
    if geo_impacts:
        print(f"  [거시] geo_analysis 카테고리 매핑: {geo_impacts}")

    # ── 히스토리 업데이트 (14일 유지) ────────────────────────────────
    history = [h for h in history if h.get("date") != date]
    history.extend(today_events)
    cutoff  = (today - datetime.timedelta(days=14)).strftime("%Y%m%d")
    history = [h for h in history if h.get("date", "0") >= cutoff]

    # ── 섹터별 집계 ──────────────────────────────────────────────────
    SECTOR_SCORE_CAP = 10   # 누적 점수 상한 (한 섹터 뉴스 폭주로 점수가 과대해지는 것 방지)
    sector_stats = {}
    for sector in SECTORS:
        pos, neg, score, recent = 0, 0, 0, []
        for ev in history:
            s = ev.get("impacts", {}).get(sector, 0)
            if s > 0:
                pos += 1; score += s
                recent.append({"date": ev["date"], "type": "positive",
                               "headline": ev["headline"], "score": s})
            elif s < 0:
                neg += 1; score += s
                recent.append({"date": ev["date"], "type": "negative",
                               "headline": ev["headline"], "score": s})
        # 점수 상한: -10 ~ +10 (호재/악재 횟수는 그대로, 누적 점수만 캡)
        score = max(-SECTOR_SCORE_CAP, min(SECTOR_SCORE_CAP, score))
        sector_stats[sector] = {
            "score": score, "positive_count": pos, "negative_count": neg,
            "recent_events": sorted(recent, key=lambda x: x["date"], reverse=True)[:5]
        }

    # ── 전체 리스크 레벨 (전체시장 섹터 누적 점수 기준, 상한 적용 전 원점수) ──
    total_neg = sum(1 for ev in history for v in ev.get("impacts", {}).values() if v < 0)
    total_pos = sum(1 for ev in history for v in ev.get("impacts", {}).values() if v > 0)

    market_raw = sum(ev.get("impacts", {}).get("전체시장", 0) for ev in history)
    if market_raw <= -5:    risk_level = "EXTREME"
    elif market_raw <= -3:  risk_level = "HIGH"
    elif market_score <= -1:  risk_level = "MEDIUM"
    else:                     risk_level = "LOW"

    # ── 저장 ─────────────────────────────────────────────────────────
    output = {
        "updated_at":      now.isoformat(),
        "date":            date,
        "window_days":     14,
        "sectors":         SECTORS,
        "calendar_events": calendar,
        "sector_stats":    sector_stats,
        "event_history":   history,
        "total_negatives": total_neg,
        "total_positives": total_pos,
        "risk_level":      risk_level,
    }
    with open("sector_risk.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n  전체 악재: {total_neg}건 / 호재: {total_pos}건 → 리스크: {risk_level}")
    for sector, st in sector_stats.items():
        if st["positive_count"] + st["negative_count"] > 0:
            print(f"    {sector:10s}: 호재 {st['positive_count']}회 / 악재 {st['negative_count']}회 / {st['score']:+d}점")
    print("\n  💾 sector_risk.json 저장 완료")
    print("\n✅ 완료!")

if __name__ == "__main__":
    main()
