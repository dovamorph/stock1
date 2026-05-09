#!/usr/bin/env python3
"""
StockPilot KR — 세계정세 분석 (geo_analysis.py)
stockplus 속보 뉴스 크롤링 → Claude AI로 5대 정세 분석
"""
import os, json, datetime, requests
from zoneinfo import ZoneInfo

KST       = ZoneInfo("Asia/Seoul")
NEWS_URL  = "https://newsroom.stockplus.com/breaking-news"
OUT_FILE  = "geo_result.json"

CATEGORIES = {
    "geopolitical": {
        "name": "① 지정학적 충돌",
        "desc": "전쟁·군사충돌·국가안보",
        "up":   "방산·유가·금·달러",
        "down": "성장주·기술주·반도체·자동차"
    },
    "macro": {
        "name": "② 거시/통화 정책",
        "desc": "금리결정·중앙은행·무역정책",
        "up":   "금융·가치주",
        "down": "고평가 성장주·건설·인프라"
    },
    "disaster": {
        "name": "③ 환경·대규모 재난",
        "desc": "팬데믹·기후변화·이상기후",
        "up":   "필수품목·생필품",
        "down": "공급망 노출 기업"
    },
    "trade": {
        "name": "④ 보호무역·공급망",
        "desc": "관세·수출규제·무역제재",
        "up":   "반사이익 국내기업·대체공급망",
        "down": "규제대상 노출기업·자동차"
    },
    "policy": {
        "name": "⑤ 대형 정책 프로젝트",
        "desc": "국가 예산투입·수주확정·법개정",
        "up":   "수주 관련 섹터",
        "down": "없음 (매수 기회)"
    }
}

SYSTEM_PROMPT = """당신은 한국 주식시장 전문 세계정세 분석가입니다.
제공된 뉴스 헤드라인들을 분석하여 주가에 영향을 미치는 5대 정세 카테고리별로 평가하세요.

반드시 다음 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{
  "categories": {
    "geopolitical": {"level": "안전|주의|경고|위험", "color": "green|yellow|orange|red", "signal": "✅|⚠️|🟠|🔴", "summary": "한줄요약", "action": "행동지침"},
    "macro":        {"level": "안전|주의|경고|위험", "color": "green|yellow|orange|red", "signal": "✅|⚠️|🟠|🔴", "summary": "한줄요약", "action": "행동지침"},
    "disaster":     {"level": "안전|주의|경고|위험", "color": "green|yellow|orange|red", "signal": "✅|⚠️|🟠|🔴", "summary": "한줄요약", "action": "행동지침"},
    "trade":        {"level": "안전|주의|경고|위험", "color": "green|yellow|orange|red", "signal": "✅|⚠️|🟠|🔴", "summary": "한줄요약", "action": "행동지침"},
    "policy":       {"level": "안전|주의|경고|위험", "color": "green|yellow|orange|red", "signal": "✅|⚠️|🟠|🔴", "summary": "한줄요약", "action": "행동지침"}
  },
  "overall": {"level": "안전|주의|경고|위험", "color": "green|yellow|orange|red", "summary": "종합 한줄요약", "action": "종합 행동지침"}
}

평가 기준:
- 안전(green): 해당 리스크 없음, 시장 우호적
- 주의(yellow): 모니터링 필요, 직접 영향 아직 없음
- 경고(orange): 영향 가시화, 포지션 점검 권고
- 위험(red): 즉각 대응 필요, 포지션 축소/현금화 고려"""

def fetch_news():
    """stockplus 속보 뉴스 크롤링"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": "https://newsroom.stockplus.com/"
    }
    try:
        r = requests.get(NEWS_URL, headers=headers, timeout=15)
        r.raise_for_status()

        # BeautifulSoup으로 파싱
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")

            headlines = []
            # 다양한 셀렉터 시도
            selectors = [
                "article h2", "article h3", ".news-title", ".headline",
                "h2", "h3", ".title", "[class*='title']", "[class*='headline']",
                "a[href*='news']"
            ]
            for sel in selectors:
                items = soup.select(sel)
                if items:
                    for item in items[:20]:
                        text = item.get_text(strip=True)
                        if len(text) > 10 and text not in headlines:
                            headlines.append(text)
                    if len(headlines) >= 5:
                        break

            if not headlines:
                # 텍스트 전체에서 한국어 문장 추출
                all_text = soup.get_text(separator="\n")
                for line in all_text.split("\n"):
                    line = line.strip()
                    if len(line) > 15 and any("\uAC00" <= c <= "\uD7A3" for c in line):
                        headlines.append(line)
                    if len(headlines) >= 20:
                        break

            return headlines[:25]

        except ImportError:
            # bs4 없으면 정규식으로
            import re
            patterns = [
                r'<h[23][^>]*>([^<]{10,})</h[23]>',
                r'"title"\s*:\s*"([^"]{10,})"',
                r'<a[^>]*>([가-힣][^<]{10,})</a>'
            ]
            headlines = []
            for pat in patterns:
                matches = re.findall(pat, r.text)
                headlines.extend(matches[:10])
            return list(set(headlines))[:25]

    except Exception as e:
        print(f"  뉴스 크롤링 실패: {e}")
        return []

def analyze_with_claude(headlines):
    """Claude API로 세계정세 분석"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  ⚠️ ANTHROPIC_API_KEY 없음")
        return None

    news_text = "\n".join(f"- {h}" for h in headlines)
    user_msg  = f"다음은 오늘 한국 주식 뉴스 속보 헤드라인입니다:\n\n{news_text}\n\n위 뉴스를 5대 정세 카테고리별로 분석해주세요."

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}]
            },
            timeout=30
        )
        r.raise_for_status()
        resp = r.json()
        text = resp["content"][0]["text"].strip()

        # JSON 추출
        import re
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(text)

    except Exception as e:
        print(f"  Claude API 실패: {e}")
        return None

def default_result():
    """API 실패 시 기본값"""
    cats = {}
    for key in CATEGORIES:
        cats[key] = {"level":"알 수 없음","color":"gray","signal":"—","summary":"데이터 없음","action":"–"}
    return {
        "categories": cats,
        "overall": {"level":"알 수 없음","color":"gray","summary":"뉴스 분석 실패","action":"–"}
    }

def main():
    now = datetime.datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 세계정세 분석  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}")

    # 1. 뉴스 크롤링
    print("\n  [1/2] 속보 뉴스 크롤링 중...")
    headlines = fetch_news()
    print(f"  → {len(headlines)}건 수집")
    for h in headlines[:5]:
        print(f"    · {h[:60]}")

    if not headlines:
        print("  ⚠️ 뉴스 없음 — 기본값 저장")
        analysis = default_result()
    else:
        # 2. Claude 분석
        print("\n  [2/2] Claude AI 세계정세 분석 중...")
        analysis = analyze_with_claude(headlines)
        if not analysis:
            analysis = default_result()
        else:
            print("  ✅ 분석 완료")
            overall = analysis.get("overall", {})
            print(f"  종합: {overall.get('level','?')} — {overall.get('summary','')}")

    # 3. 저장
    result = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "news_count": len(headlines),
        "news_sample": headlines[:5],
        "categories": analysis.get("categories", {}),
        "overall": analysis.get("overall", {})
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n  💾 {OUT_FILE} 저장 완료")
    print(f"\n✅ 세계정세 분석 완료!")

if __name__ == "__main__":
    main()
