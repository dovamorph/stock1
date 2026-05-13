# 옵션 만기일 D-day 계산 및 expiry_result.json 저장
import json, datetime, os
from zoneinfo import ZoneInfo

KST      = ZoneInfo("Asia/Seoul")
OUT_FILE = "expiry_result.json"

def get_next_expiry():
    """매월 두 번째 목요일 계산"""
    today = datetime.date.today()
    y, m  = today.year, today.month
    for _ in range(3):
        first      = datetime.date(y, m, 1)
        thu_offset = (3 - first.weekday()) % 7
        second_thu = first + datetime.timedelta(days=thu_offset + 7)
        if second_thu >= today:
            return second_thu
        m += 1
        if m > 12:
            m = 1; y += 1
    return None

def main():
    now         = datetime.datetime.now(KST)
    today       = datetime.date.today()
    expiry_date = get_next_expiry()
    d_day       = (expiry_date - today).days

    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 옵션만기 분석  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}")
    print(f"  다음 만기일: {expiry_date} (D-{d_day})")

    result = {
        "expiry_date": str(expiry_date),
        "d_day":       d_day,
        "updated":     now.strftime("%Y-%m-%d %H:%M"),
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"  💾 {OUT_FILE} 저장 완료")
    print(f"\n✅ 완료!")

if __name__ == "__main__":
    main()
