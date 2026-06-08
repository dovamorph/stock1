#!/usr/bin/env python3
"""
event_tracker.py — 이벤트 리스크 트래커
레이어1: 예정 이벤트 캘린더 (날짜 기반, 수급 영향 설명)
레이어2: 뉴스 악재 누적 트래커 (섹터별 호재/악재 카운트)
"""
import os, json, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# ── 섹터 키워드 매핑 ─────────────────────────────────────────────────
SECTOR_KEYWORDS = {
    "반도체": {
        "positive": [
            "HBM", "메모리 가격 상승", "AI 서버 수요", "엔비디아 호실적",
            "반도체 수출 증가", "D램 강세", "TSMC 호조", "반도체 수주",
            "반도체 슈퍼사이클", "엔비디아 실적", "nvidia 호실적"
        ],
        "negative": [
            "브로드컴", "broadcom", "Broadcom", "반도체 제재", "메모리 가격 하락",
            "반도체 재고", "반도체 수출 규제", "반도체 쇼크", "반도체 위기",
            "엔비디아 실망", "마이크론 부진", "인텔 실망"
        ]
    },
    "건설": {
        "positive": [
            "휴전", "재건", "중동 재건", "인프라 투자", "건설 수주",
            "해외 건설", "평화 협정", "전후 복구", "종전", "정전"
        ],
        "negative": [
            "전쟁 확대", "PF 부실", "건설 경기 침체", "부동산 위기",
            "부동산 PF", "건설사 부도", "공사 중단", "분쟁 격화"
        ]
    },
    "방산": {
        "positive": [
            "전쟁 확대", "긴장 고조", "무기 수출", "방산 수주",
            "군비 증강", "분쟁 격화", "군사 충돌", "교전", "공습"
        ],
        "negative": [
            "휴전", "평화 협정", "군비 축소", "방산 예산 삭감",
            "종전", "평화 협상", "정전 합의"
        ]
    },
    "전력전선": {
        "positive": [
            "AI 데이터센터", "전력 수요", "에너지 전환", "원전 확대",
            "송전 투자", "전선 수주", "케이블 수요", "전력망 투자"
        ],
        "negative": [
            "전력 규제", "에너지 가격 하락", "원전 반대", "탈원전"
        ]
    },
    "로봇": {
        "positive": [
            "피지컬 AI", "로봇 수요", "자동화", "엔비디아 로봇",
            "로보틱스", "젠슨황 로봇", "AI 로봇", "휴머노이드"
        ],
        "negative": [
            "로봇 규제", "자동화 제한", "로봇 거품", "로봇 실망"
        ]
    },
    "금융": {
        "positive": [
            "금리 인하", "경기 회복", "금융 실적 호조", "배당 증가",
            "Fed 피봇", "통화완화", "기준금리 인하"
        ],
        "negative": [
            "금리 인상", "신용 위기", "부실 대출", "금융 규제",
            "Fed 긴축", "뱅크런", "기준금리 인상"
        ]
    },
    "우주ETF": {
        "positive": [
            "스페이스X 성공", "우주 개발", "스타링크", "발사 성공",
            "NASA", "우주 수주", "SPCX"
        ],
        "negative": [
            "발사 실패", "우주 규제", "스페이스X 문제"
        ]
    },
    "전체시장": {
        "positive": [
            "금리 인하", "경기 연착륙", "무역 협상 타결",
            "인플레이션 둔화", "외국인 순매수", "증시 반등"
        ],
        "negative": [
            "전쟁 확대", "금융위기", "경기침체", "Fed 긴축",
            "외국인 대규모 매도", "패닉셀", "블랙먼데이",
            "이란", "중동 전쟁", "공급망 위기"
        ]
    }
}

# ── 예정 이벤트 캘린더 ────────────────────────────────────────────────
# 이벤트 지나면 날짜 업데이트 필요
CALENDAR_EVENTS = [
    {
        "id": "spacex_ipo_2026",
        "name": "스페이스X IPO",
        "date": "2026-06-12",
        "type": "ipo",
        "risk_level": "high",
        "description": "역대 최대 공모(750억달러)로 글로벌 자금이 쏠리면서 기존 보유 주식을 팔아 현금을 확보하는 움직임 예고. 반도체·성장주 중심으로 외국인 매도세 강화 가능성.",
        "benefit_text": "우주ETF ▲▲▲",
        "damage_text": "반도체 ▼▼  성장주 ▼",
        "sector_impacts": {"반도체": -2, "우주ETF": 3, "전체시장": -1}
    },
    {
        "id": "options_expiry_jun2026",
        "name": "옵션 만기일",
        "date": "2026-06-11",
        "type": "expiry",
        "risk_level": "medium",
        "description": "월물 옵션 만기. 프로그램 매매 영향으로 변동성 확대 가능. 장 마감 앞두고 수급 급변 주의.",
        "benefit_text": "",
        "damage_text": "전체 변동성 확대 주의",
        "sector_impacts": {"전체시장": -1}
    },
    {
        "id": "fed_jun2026",
        "name": "Fed 금리 결정",
        "date": "2026-06-18",
        "type": "fed",
        "risk_level": "medium",
        "description": "금리 동결 예상. 인하 시그널이 나오면 성장주·금융주 강세. 예상 밖 인상 시그널 시 전체 시장 충격 가능.",
        "benefit_text": "인하 시: 금융 ▲▲  건설 ▲  성장주 ▲",
        "damage_text": "인상 시: 전체장 ▼▼",
        "sector_impacts": {"금융": 2, "건설": 1, "전체시장": 1}
    },
    {
        "id": "nvidia_q2_2026",
        "name": "NVIDIA 2분기 실적",
        "date": "2026-06-25",
        "type": "earnings",
        "risk_level": "high",
        "description": "AI 수요 확인의 가늠자. 어닝 서프라이즈 시 반도체 전체 랠리. 실망 시 AI테마 전반 조정.",
        "benefit_text": "호실적: 반도체 ▲▲▲  소부장 ▲▲",
        "damage_text": "실망: 반도체 ▼▼  AI테마 ▼▼",
        "sector_impacts": {"반도체": 3, "로봇": 1, "전력전선": 1}
    },
]

def classify_news(headline: str, content: str = "") -> dict:
    """키워드 기반 섹터별 호재/악재 분류. 점수 반환."""
    text_lower = (headline + " " + content).lower()
    text_orig  = headline + " " + content
    impacts = {}
    for sector, kws in SECTOR_KEYWORDS.items():
        score = 0
        for kw in kws["positive"]:
            if kw.lower() in text_lower or kw in text_orig:
                score += 1
        for kw in kws["negative"]:
            if kw.lower() in text_lower or kw in text_orig:
                score -= 1
        if score != 0:
            impacts[sector] = score
    return impacts

def load_geo_result() -> list:
    GEO_FILE = "geo_result.json"
    if not os.path.exists(GEO_FILE):
        return []
    try:
        with open(GEO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("events", data.get("news", data.get("items", [])))
    except:
        return []

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

    # ── 캘린더 D-day 계산 (D-3 ~ D+30만 표시) ────────────────────────
    calendar = []
    for ev in CALENDAR_EVENTS:
        try:
            ev_date = datetime.date.fromisoformat(ev["date"])
            d_day   = (ev_date - today).days
            if -3 <= d_day <= 30:
                calendar.append({**ev, "d_day": d_day})
        except:
            pass
    calendar.sort(key=lambda x: x["d_day"])

    print(f"\n  [캘린더] 향후 이벤트: {len(calendar)}개")
    for ev in calendar:
        tag = f"D-{ev['d_day']}" if ev["d_day"] >= 0 else f"D+{abs(ev['d_day'])}"
        print(f"    {tag:5s}  {ev['name']} ({ev['date']})")

    # ── 뉴스 분류 ────────────────────────────────────────────────────
    geo_news    = load_geo_result()
    history     = load_event_history()

    today_events = []
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
                "source":   item.get("source", ""),
            })

    print(f"\n  [분류] 뉴스 {len(geo_news)}건 → 섹터 영향 {len(today_events)}건 감지")

    # ── 히스토리 업데이트 (14일 유지) ────────────────────────────────
    history = [h for h in history if h.get("date") != date]
    history.extend(today_events)
    cutoff  = (today - datetime.timedelta(days=14)).strftime("%Y%m%d")
    history = [h for h in history if h.get("date", "0") >= cutoff]

    # ── 섹터별 집계 ──────────────────────────────────────────────────
    sector_stats = {}
    for sector in SECTOR_KEYWORDS:
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
        sector_stats[sector] = {
            "score": score, "positive_count": pos, "negative_count": neg,
            "recent_events": sorted(recent, key=lambda x: x["date"], reverse=True)[:5]
        }

    # ── 전체 리스크 레벨 ─────────────────────────────────────────────
    total_neg = sum(1 for ev in history if any(v < 0 for v in ev.get("impacts", {}).values()))
    total_pos = sum(1 for ev in history if any(v > 0 for v in ev.get("impacts", {}).values()))
    if total_neg >= 5:    risk_level = "EXTREME"
    elif total_neg >= 3:  risk_level = "HIGH"
    elif total_neg >= 1:  risk_level = "MEDIUM"
    else:                 risk_level = "LOW"

    # ── 저장 ─────────────────────────────────────────────────────────
    output = {
        "updated_at":      now.isoformat(),
        "date":            date,
        "window_days":     14,
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
