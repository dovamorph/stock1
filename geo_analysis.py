#!/usr/bin/env python3
"""
StockPilot KR — 세계정세 분석 (geo_analysis.py)
국제 + 국내 뉴스 RSS 크롤링 → 키워드 룰 기반 5대 정세 분석
국제: BBC World News / Al Jazeera
국내: 연합뉴스 / 한국경제 / 매일경제 / 파이낸셜뉴스 / StockPlus
"""
import json, datetime, re, time
import urllib.request as urllib_req
from zoneinfo import ZoneInfo

KST      = ZoneInfo("Asia/Seoul")
OUT_FILE = "geo_result.json"

# ── 뉴스 소스 ─────────────────────────────────────────────────────────
NEWS_SOURCES = [
    # ── 국제 (지정학·세계 정세 핵심) ──
    {
        "name": "BBC World",
        "url":  "http://feeds.bbci.co.uk/news/world/rss.xml",
        "weight": 1.5,
        "type": "international"
    },
    {
        "name": "Al Jazeera",
        "url":  "https://www.aljazeera.com/xml/rss/all.xml",
        "weight": 1.4,
        "type": "international"
    },
    # ── 국내 ──
    {
        "name": "연합뉴스",
        "url":  "https://www.yna.co.kr/rss/news.xml",
        "weight": 1.5,
        "type": "domestic"
    },
    {
        "name": "한국경제",
        "url":  "http://rss.hankyung.com/economy.xml",
        "weight": 1.3,
        "type": "domestic"
    },
    {
        "name": "매일경제",
        "url":  "http://file.mk.co.kr/news/rss/rss_40300001.xml",
        "weight": 1.2,
        "type": "domestic"
    },
    {
        "name": "파이낸셜뉴스",
        "url":  "http://www.fnnews.com/rss/fn_realnews_all.xml",
        "weight": 1.1,
        "type": "domestic"
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
                "전쟁", "군사 충돌", "미사일 발사", "공습", "폭격", "침공", "전면전",
                "war declared", "military strike", "missile attack", "airstrike",
                "armed conflict", "invasion", "nuclear threat", "핵 공격"
            ],
            "경고": [
                "군사 훈련", "무력 시위", "도발", "긴장 고조", "봉쇄", "핵 위협",
                "military option", "military exercises", "escalation", "sanctions imposed",
                "naval blockade", "troops deployed", "분쟁", "해상 봉쇄", "국경 충돌"
            ],
            "주의": [
                "외교 갈등", "군사 경계", "안보 위협", "회담 결렬",
                "diplomatic tension", "military alert", "ceasefire", "peace talks",
                "우크라이나", "러시아", "이스라엘", "하마스", "이란", "중동",
                "대만 해협", "남중국해", "북한", "Gaza", "Ukraine", "Iran", "Taiwan"
            ],
            "신호": [
                "방산", "방위산업", "유가 급등", "금 급등", "달러 급등",
                "defense spending", "oil surge", "gold rally", "safe haven"
            ]
        }
    },
    "macro": {
        "name": "② 거시/통화 정책",
        "desc": "금리결정·중앙은행·통화정책",
        "up":   "금융·가치주",
        "down": "고평가 성장주·건설·인프라",
        "keywords": {
            "위험": [
                "금리 인상", "긴축 강화", "외국인 순매도", "환율 급등", "외환위기",
                "rate hike", "aggressive tightening", "capital outflow", "currency crisis",
                "채권 금리 급등", "달러 강세 심화"
            ],
            "경고": [
                "금리 동결", "인플레이션 우려", "CPI 상승", "Fed 매파",
                "hawkish", "inflation concern", "10년물 금리", "4% 돌파",
                "금리인하 지연", "피벗 후퇴", "stagflation", "스태그플레이션"
            ],
            "주의": [
                "금리", "연준", "Fed", "한국은행", "기준금리", "통화정책",
                "인플레", "물가", "CPI", "PPI", "환율", "원달러",
                "interest rate", "central bank", "monetary policy", "inflation",
                "Federal Reserve", "ECB", "BOJ", "달러 인덱스", "DXY"
            ],
            "신호": [
                "외국인 선물 매도", "환율 1400", "환율 1500",
                "yield curve", "bond yield", "treasury"
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
                # 주가에 실제 영향 → 공급망·봉쇄·대피 수준
                "팬데믹", "감염병 대유행", "봉쇄 조치", "국경 봉쇄", "공장 폐쇄",
                "pandemic", "lockdown", "global outbreak", "epidemic declared",
                "원전 사고", "nuclear accident", "방사능 유출", "radiation leak",
                "대규모 공급망 붕괴", "supply chain collapse"
            ],
            "경고": [
                # 확산 가능성 있는 신종 질병
                "신종 바이러스", "변이 바이러스", "집단감염", "감염병 확산",
                "new virus", "new variant", "outbreak spreading", "quarantine zone",
                # 공급망에 실제 영향 주는 극단적 기상
                "슈퍼 태풍", "mega typhoon", "category 5",
                "대규모 산불", "wildfire spreading",
                "엘니뇨 심화", "극한 가뭄"
            ],
            "주의": [
                # 주가 영향 거의 없는 일상적 재난 → 제거
                # 공급망 이슈 가능성 있는 것만
                "초대형 홍수", "대규모 지진", "쓰나미", "tsunami",
                "massive earthquake", "major flood"
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
                "반도체 수출 규제", "중국 제재", "디커플링 가속", "decoupling"
            ],
            "경고": [
                "관세 인상", "수출 규제 강화", "무역 제재", "공급망 재편",
                "tariff hike", "export control tightened", "trade restriction",
                "미중 갈등", "반도체 규제", "배터리 규제", "자동차 관세",
                "chip ban", "semiconductor sanctions"
            ],
            "주의": [
                "관세", "무역", "수출입", "공급망", "제재",
                "tariff", "trade", "supply chain", "sanction",
                "WTO", "FTA", "중국", "미중", "US-China",
                "protectionism", "보호무역"
            ],
            "신호": [
                "수입 금지", "블랙리스트", "화웨이", "TSMC",
                "CHIPS Act", "IRA", "반도체법", "Huawei"
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
                "대규모 투자 발표", "국책 사업 확정", "infrastructure bill passed"
            ],
            "주의": [
                "논의 중", "검토 중", "수주 추진", "예산 심의",
                "프로젝트 발표", "로봇법", "새만금", "원전", "방산 수출",
                "K-방산", "반도체 지원", "배터리 보조금",
                "green new deal", "stimulus package", "인프라 투자"
            ],
            "신호": [
                "수주", "프로젝트", "국책", "정부 지원", "보조금",
                "contract awarded", "government project", "subsidy announced"
            ]
        }
    }
}

# ── RSS 파싱 ───────────────────────────────────────────────────────────
def fetch_rss(source):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; StockPilotBot/1.0; +https://github.com)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"
    }
    try:
        req = urllib_req.Request(source["url"], headers=headers)
        with urllib_req.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            # 인코딩 자동 감지
            try:
                content = raw.decode("utf-8", errors="ignore")
            except:
                content = raw.decode("euc-kr", errors="ignore")

        # <title> 추출
        titles = re.findall(
            r'<title[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>',
            content, re.DOTALL
        )
        headlines = []
        skip_words = {"RSS", "피드", "뉴스", "News", "Feed", source["name"],
                      "BBC News", "Al Jazeera", "한국경제", "매일경제", "파이낸셜뉴스"}
        for t in titles:
            t = re.sub(r'<[^>]+>', '', t).strip()
            t = t.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')\
                 .replace('&quot;', '"').replace('&#39;', "'")
            if len(t) > 8 and t not in skip_words and t not in headlines:
                headlines.append(t)

        return headlines[:30]

    except Exception as e:
        print(f"    {source['name']} 실패: {type(e).__name__}: {str(e)[:60]}")
        return []


def fetch_stockplus():
    try:
        req = urllib_req.Request(
            "https://newsroom.stockplus.com/breaking-news",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib_req.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
        text_blocks = re.findall(r'>[^<]{10,100}<', content)
        headlines = []
        for block in text_blocks:
            text = block[1:-1].strip()
            if any('\uAC00' <= c <= '\uD7A3' for c in text) and len(text) > 8:
                headlines.append(text)
        return list(dict.fromkeys(headlines))[:25]
    except Exception as e:
        print(f"    StockPlus 실패: {e}")
        return []


# ── 키워드 분석 ────────────────────────────────────────────────────────
COLOR_MAP  = {"위험": "red", "경고": "orange", "주의": "yellow",
              "기회": "green", "신호": "yellow", "안전": "green", "알 수 없음": "gray"}
SIGNAL_MAP = {"위험": "🔴", "경고": "🟠", "주의": "🟡",
              "기회": "🟢", "신호": "🟡", "안전": "🟢", "알 수 없음": "⚪"}
ACTION_MAP = {
    "위험": "포지션 현금화 고려 / 즉각 대응",
    "경고": "포지션 축소 검토 / 모니터링 강화",
    "주의": "신규 매수 자제 / 추이 관찰",
    "기회": "관련 섹터 매수 기회 탐색",
    "신호": "관련 지표 추가 확인 후 판단",
    "안전": "정상 운용 유지",
    "알 수 없음": "데이터 부족"
}

def score_text(text, keywords_dict, weight=1.0):
    scores = {}
    text_lower = text.lower()
    for level, kws in keywords_dict.items():
        for kw in kws:
            if kw.lower() in text_lower:
                scores[level] = scores.get(level, 0) + weight
    return scores

def analyze_category(all_news_weighted, cat_key):
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
            "level": "안전", "color": "green", "signal": "🟢",
            "summary": "관련 이슈 없음", "action": ACTION_MAP["안전"], "matched": 0, "score": 0
        }

    top_level = "안전"
    for lv in ["위험", "경고", "주의", "기회", "신호"]:
        if lv in total_scores and total_scores[lv] > 0:
            top_level = lv
            break

    sample = matched_headlines[:1]
    summary = sample[0][0][:45] + ("..." if len(sample[0][0]) > 45 else "") if sample else "관련 이슈 없음"

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
    level_priority = {"위험": 4, "경고": 3, "주의": 2, "기회": 1, "신호": 1, "안전": 0}
    max_level, max_score = "안전", 0
    for result in cat_results.values():
        lv = result["level"]
        sc = level_priority.get(lv, 0) * (result.get("score", 1) + 1)
        if sc > max_score:
            max_score, max_level = sc, lv

    danger_cats = [CATEGORIES[k]["name"] for k, v in cat_results.items()
                   if v["level"] in ["위험", "경고"]]
    opportunity_cats = [CATEGORIES[k]["name"] for k, v in cat_results.items()
                        if v["level"] == "기회"]

    if danger_cats:
        summary = f"주의 필요: {', '.join(c.split(' ', 1)[1] for c in danger_cats[:2])}"
    elif opportunity_cats:
        summary = f"매수 기회: {', '.join(c.split(' ', 1)[1] for c in opportunity_cats[:2])}"
        max_level = "기회"
    else:
        summary = "전반적 안정. 정상 운용 유지"
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

    print("\n  [1/2] 뉴스 수집 중...")
    all_news_weighted = []
    source_stats = {}

    for source in NEWS_SOURCES:
        tag = "🌍" if source["type"] == "international" else "🇰🇷"
        print(f"    {tag} {source['name']} ...", end=" ", flush=True)
        headlines = fetch_rss(source)
        print(f"{len(headlines)}건")
        for h in headlines:
            all_news_weighted.append((h, source["weight"]))
        source_stats[source["name"]] = len(headlines)
        time.sleep(0.5)

    print(f"    🇰🇷 StockPlus ...", end=" ", flush=True)
    sp = fetch_stockplus()
    print(f"{len(sp)}건")
    for h in sp:
        all_news_weighted.append((h, 1.2))
    source_stats["StockPlus"] = len(sp)

    total = len(all_news_weighted)
    intl_count = sum(v for k, v in source_stats.items() if k in ["BBC World", "Al Jazeera"])
    domestic_count = total - intl_count
    print(f"\n  총 {total}건 (국제 {intl_count}건 / 국내 {domestic_count}건)")

    print("\n  [2/2] 5대 카테고리 키워드 분석 중...")
    cat_results = {}
    for cat_key in CATEGORIES:
        result = analyze_category(all_news_weighted, cat_key)
        cat_results[cat_key] = result
        print(f"    {result['signal']} {CATEGORIES[cat_key]['name']}: {result['level']} (매칭 {result['matched']}건)")

    overall = calc_overall(cat_results)
    print(f"\n  📊 종합: {overall['level']} — {overall['summary']}")

    seen = set()
    samples = []
    for h, _ in all_news_weighted:
        if h not in seen and len(samples) < 5:
            seen.add(h); samples.append(h)

    result = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "news_count": total,
        "intl_count": intl_count,
        "domestic_count": domestic_count,
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
