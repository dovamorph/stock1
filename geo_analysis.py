#!/usr/bin/env python3
"""
StockPilot KR — 세계정세 분석 (geo_analysis.py)
5개 뉴스 소스 RSS 크롤링 → 키워드 룰 기반 5대 정세 분석
소스: 연합뉴스 / 한국경제 / 매일경제 / 네이버금융 / Reuters
"""
import json, datetime, re, time
from zoneinfo import ZoneInfo

try:
    import urllib.request as urllib_req
    import urllib.error
except:
    pass

KST      = ZoneInfo("Asia/Seoul")
OUT_FILE = "geo_result.json"

# ── 뉴스 소스 (RSS) ────────────────────────────────────────────────────
NEWS_SOURCES = [
    {
        "name": "연합뉴스",
        "url":  "https://www.yna.co.kr/rss/news.xml",
        "weight": 1.5   # 속보 신뢰도 높음
    },
    {
        "name": "한국경제",
        "url":  "https://www.hankyung.com/rss/finance.xml",
        "weight": 1.3
    },
    {
        "name": "매일경제",
        "url":  "https://www.mk.co.kr/rss/30100041.xml",
        "weight": 1.2
    },
    {
        "name": "네이버금융",
        "url":  "https://finance.naver.com/news/news_list.naver?mode=LSS2D&section_id=101&section_id2=258",
        "weight": 1.0
    },
    {
        "name": "Reuters",
        "url":  "https://feeds.reuters.com/reuters/businessNews",
        "weight": 1.4   # 글로벌 지정학 신뢰도 높음
    },
]

# ── 5대 카테고리 키워드 ────────────────────────────────────────────────
CATEGORIES = {
    "geopolitical": {
        "name": "① 지정학적 충돌",
        "desc": "전쟁·군사충돌·국가안보",
        "up":   "방산·유가·금·달러",
        "down": "성장주·기술주·반도체·자동차",
        "keywords": {
            "위험": [
                "전쟁", "군사 충돌", "미사일 발사", "공습", "폭격", "침공",
                "military strike", "war declared", "missile attack", "airstrike",
                "retaliation", "armed conflict", "군사 개입", "전면전"
            ],
            "경고": [
                "군사 훈련", "무력 시위", "군사 옵션", "도발", "긴장 고조",
                "military option", "military exercises", "escalation", "sanctions imposed",
                "핵 위협", "영해 침범", "분쟁", "봉쇄", "해상 봉쇄"
            ],
            "주의": [
                "외교 갈등", "회담 결렬", "군사 경계", "안보 위협", "철수",
                "diplomatic tension", "military alert", "우크라이나", "이란", "이스라엘",
                "하마스", "중동", "대만 해협", "남중국해", "북한 도발"
            ],
            "신호": [
                "방산", "방위산업", "유가 급등", "금 급등", "달러 급등",
                "defense stock", "oil surge", "gold surge"
            ]
        }
    },
    "macro": {
        "name": "② 거시/통화 정책",
        "desc": "금리결정·중앙은행·무역정책",
        "up":   "금융·가치주",
        "down": "고평가 성장주·건설·인프라",
        "keywords": {
            "위험": [
                "금리 인상", "긴축 강화", "외국인 순매도", "환율 급등", "달러 강세",
                "rate hike", "aggressive tightening", "capital outflow",
                "환율 전고점", "외환위기", "채권 금리 급등"
            ],
            "경고": [
                "금리 동결", "인플레이션 우려", "CPI 상승", "Fed 매파",
                "hawkish", "inflation concern", "10년물 금리", "4% 돌파",
                "금리인하 지연", "피벗 후퇴", "달러 강세"
            ],
            "주의": [
                "금리", "연준", "Fed", "한국은행", "기준금리", "통화정책",
                "인플레", "물가", "CPI", "PPI", "환율", "원달러",
                "interest rate", "central bank", "monetary policy", "inflation"
            ],
            "신호": [
                "외국인 선물 매도", "환율 1400", "환율 1500", "금리선물"
            ]
        }
    },
    "disaster": {
        "name": "③ 환경·대규모 재난",
        "desc": "팬데믹·기후변화·이상기후",
        "up":   "필수품목·생필품·헬스케어",
        "down": "공급망 노출 기업·항공·여행",
        "keywords": {
            "위험": [
                "팬데믹", "감염병 확산", "대유행", "봉쇄 조치", "공장 폐쇄",
                "pandemic", "lockdown", "epidemic", "outbreak declared",
                "대규모 홍수", "강진", "쓰나미", "원전 사고"
            ],
            "경고": [
                "신종 바이러스", "변이 바이러스", "집단감염", "공급망 차질",
                "new variant", "supply chain disruption", "태풍 상륙",
                "폭염 비상", "가뭄 심화", "기후 위기"
            ],
            "주의": [
                "바이러스", "감염", "전염병", "홍수", "태풍", "지진",
                "virus", "flood", "typhoon", "earthquake", "climate",
                "이상기후", "자연재해", "재난 경보"
            ],
            "신호": [
                "마스크", "백신", "방역", "quarantine"
            ]
        }
    },
    "trade": {
        "name": "④ 보호무역·공급망",
        "desc": "관세·수출규제·무역제재",
        "up":   "반사이익 국내기업·대체공급망 수혜",
        "down": "규제대상 노출기업·자동차·반도체",
        "keywords": {
            "위험": [
                "관세 부과", "수출 금지", "무역 전쟁", "공급망 차단",
                "tariff imposed", "export ban", "trade war", "sanctions",
                "반도체 수출 규제", "중국 제재", "디커플링 가속"
            ],
            "경고": [
                "관세 인상", "수출 규제 강화", "무역 제재", "공급망 재편",
                "tariff hike", "export control", "trade restriction",
                "미중 갈등", "반도체 규제", "배터리 규제", "자동차 관세"
            ],
            "주의": [
                "관세", "무역", "수출입", "공급망", "제재",
                "tariff", "trade", "supply chain", "sanction",
                "중국", "미국 규제", "WTO", "FTA 재협상"
            ],
            "신호": [
                "수입 금지", "블랙리스트", "화웨이", "TSMC 규제",
                "반도체법", "IRA", "인플레 감축법"
            ]
        }
    },
    "policy": {
        "name": "⑤ 대형 정책 프로젝트",
        "desc": "국가예산투입·수주확정·법개정",
        "up":   "수주 관련 섹터 (매수 기회)",
        "down": "없음",
        "keywords": {
            "기회": [
                "수주 확정", "수주 발표", "프로젝트 착공", "예산 확정",
                "법안 통과", "원전 수주", "방산 수출 확정",
                "대규모 투자 발표", "국책 사업 확정"
            ],
            "주의": [
                "논의 중", "검토 중", "수주 추진", "예산 심의",
                "프로젝트 발표", "로봇법", "새만금", "원전", "방산 수출",
                "K-방산", "반도체 지원", "배터리 보조금"
            ],
            "신호": [
                "수주", "프로젝트", "국책", "정부 지원", "보조금",
                "contract", "government project", "subsidy"
            ]
        }
    }
}

# ── RSS 파싱 ───────────────────────────────────────────────────────────
def fetch_rss(source):
    """RSS XML 파싱, 헤드라인 리스트 반환"""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; StockPilotBot/1.0)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*"
    }
    try:
        req = urllib_req.Request(source["url"], headers=headers)
        with urllib_req.urlopen(req, timeout=12) as resp:
            content = resp.read().decode("utf-8", errors="ignore")

        # <title> 태그에서 헤드라인 추출
        titles = re.findall(r'<title[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>', content, re.DOTALL)
        items  = re.findall(r'<description[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</description>', content, re.DOTALL)

        headlines = []
        for t in titles:
            t = re.sub(r'<[^>]+>', '', t).strip()
            t = t.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
            if len(t) > 5 and t not in ["RSS", "피드", "뉴스", source["name"]]:
                headlines.append(t)

        # description도 추가 (영문 Reuters 등)
        for d in items[:10]:
            d = re.sub(r'<[^>]+>', '', d).strip()
            d = d.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
            if len(d) > 15 and d not in headlines:
                headlines.append(d)

        return headlines[:30]

    except Exception as e:
        print(f"    {source['name']} 로딩 실패: {e}")
        return []


def fetch_stockplus():
    """stockplus 속보 직접 크롤링"""
    try:
        req = urllib_req.Request(
            "https://newsroom.stockplus.com/breaking-news",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib_req.urlopen(req, timeout=12) as resp:
            content = resp.read().decode("utf-8", errors="ignore")

        # 한국어 텍스트 추출
        text_blocks = re.findall(r'>[^<]{10,100}<', content)
        headlines = []
        for block in text_blocks:
            text = block[1:-1].strip()
            if any('\uAC00' <= c <= '\uD7A3' for c in text) and len(text) > 8:
                headlines.append(text)
        return list(dict.fromkeys(headlines))[:25]
    except Exception as e:
        print(f"    StockPlus 로딩 실패: {e}")
        return []


# ── 키워드 분석 ────────────────────────────────────────────────────────
LEVEL_ORDER = ["위험", "경고", "주의", "기회", "신호", "안전"]
COLOR_MAP   = {"위험": "red", "경고": "orange", "주의": "yellow",
               "기회": "green", "신호": "yellow", "안전": "green", "알 수 없음": "gray"}
SIGNAL_MAP  = {"위험": "🔴", "경고": "🟠", "주의": "⚠️",
               "기회": "✅", "신호": "⚠️", "안전": "✅", "알 수 없음": "—"}
ACTION_MAP  = {
    "위험": "포지션 현금화 고려 / 즉각 대응",
    "경고": "포지션 축소 검토 / 모니터링 강화",
    "주의": "신규 매수 자제 / 추이 관찰",
    "기회": "관련 섹터 매수 기회 탐색",
    "신호": "관련 지표 추가 확인 후 판단",
    "안전": "정상 운용 유지",
    "알 수 없음": "데이터 부족"
}

def score_text(text, keywords_dict, weight=1.0):
    """텍스트에서 키워드 매칭 → 레벨별 점수 반환"""
    scores = {}
    text_lower = text.lower()
    for level, kws in keywords_dict.items():
        for kw in kws:
            if kw.lower() in text_lower:
                scores[level] = scores.get(level, 0) + weight
    return scores

def analyze_category(all_news_weighted, cat_key):
    """카테고리별 뉴스 분석"""
    cat = CATEGORIES[cat_key]
    total_scores = {}
    matched_headlines = []

    for headline, weight in all_news_weighted:
        scores = score_text(headline, cat["keywords"], weight)
        for level, s in scores.items():
            total_scores[level] = total_scores.get(level, 0) + s
        if scores:
            matched_headlines.append((headline, scores))

    if not total_scores:
        return {
            "level": "안전", "color": "green", "signal": "✅",
            "summary": "관련 이슈 없음", "action": ACTION_MAP["안전"],
            "matched": 0
        }

    # 최고 위험 레벨 결정
    top_level = "안전"
    for lv in ["위험", "경고", "주의", "기회", "신호"]:
        if lv in total_scores and total_scores[lv] > 0:
            top_level = lv
            break

    # 매칭된 헤드라인 요약
    sample = matched_headlines[:2]
    if sample:
        summary = sample[0][0][:40] + ("..." if len(sample[0][0]) > 40 else "")
    else:
        summary = "관련 이슈 없음"

    return {
        "level": top_level,
        "color": COLOR_MAP[top_level],
        "signal": SIGNAL_MAP[top_level],
        "summary": summary,
        "action": ACTION_MAP[top_level],
        "matched": len(matched_headlines),
        "score": round(total_scores.get(top_level, 0), 1)
    }

def calc_overall(cat_results):
    """전체 위험도 종합"""
    level_priority = {"위험": 4, "경고": 3, "주의": 2, "기회": 1, "신호": 1, "안전": 0}
    max_level = "안전"
    max_score = 0
    for cat_key, result in cat_results.items():
        lv = result["level"]
        sc = level_priority.get(lv, 0) * result.get("score", 1)
        if sc > max_score:
            max_score = sc
            max_level = lv

    danger_cats = [CATEGORIES[k]["name"] for k, v in cat_results.items()
                   if v["level"] in ["위험", "경고"]]
    opportunity_cats = [CATEGORIES[k]["name"] for k, v in cat_results.items()
                        if v["level"] == "기회"]

    if danger_cats:
        summary = f"주의 필요: {', '.join(danger_cats[:2])}"
    elif opportunity_cats:
        summary = f"매수 기회: {', '.join(opportunity_cats[:2])}"
        max_level = "기회"
    else:
        summary = "전반적 안정. 정상 운용"
        max_level = "안전"

    return {
        "level": max_level,
        "color": COLOR_MAP.get(max_level, "gray"),
        "summary": summary,
        "action": ACTION_MAP.get(max_level, "–")
    }


# ── 메인 ──────────────────────────────────────────────────────────────
def main():
    now = datetime.datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 세계정세 분석  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}")

    # 1. 뉴스 수집
    print("\n  [1/2] 뉴스 수집 중...")
    all_news_weighted = []   # [(headline, weight), ...]
    source_stats = {}

    # RSS 소스들
    for source in NEWS_SOURCES:
        print(f"    → {source['name']} ...", end=" ")
        headlines = fetch_rss(source)
        print(f"{len(headlines)}건")
        for h in headlines:
            all_news_weighted.append((h, source["weight"]))
        source_stats[source["name"]] = len(headlines)
        time.sleep(0.3)

    # StockPlus 속보
    print(f"    → StockPlus ...", end=" ")
    sp_headlines = fetch_stockplus()
    print(f"{len(sp_headlines)}건")
    for h in sp_headlines:
        all_news_weighted.append((h, 1.2))
    source_stats["StockPlus"] = len(sp_headlines)

    total_news = len(all_news_weighted)
    print(f"\n  총 {total_news}건 수집 완료")
    if total_news == 0:
        print("  ⚠️ 뉴스 없음 — 기본값 저장")

    # 2. 키워드 분석
    print("\n  [2/2] 5대 카테고리 키워드 분석 중...")
    cat_results = {}
    for cat_key in CATEGORIES:
        result = analyze_category(all_news_weighted, cat_key)
        cat_results[cat_key] = result
        icon = result["signal"]
        print(f"    {icon} {CATEGORIES[cat_key]['name']}: {result['level']} (매칭 {result['matched']}건)")

    overall = calc_overall(cat_results)
    print(f"\n  📊 종합: {overall['level']} — {overall['summary']}")

    # 3. 저장
    # 샘플 뉴스 (상위 5개, 중복 제거)
    seen = set()
    samples = []
    for h, _ in all_news_weighted:
        if h not in seen and len(samples) < 5:
            seen.add(h)
            samples.append(h)

    result = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "news_count": total_news,
        "source_stats": source_stats,
        "news_sample": samples,
        "categories": cat_results,
        "overall": overall
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n  💾 {OUT_FILE} 저장 완료")
    print(f"\n✅ 세계정세 분석 완료!")

if __name__ == "__main__":
    main()
