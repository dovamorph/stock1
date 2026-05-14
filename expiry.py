# 옵션 만기일 D-day 계산 및 expiry_result.json 저장
# trader.py가 직접 읽어 만기일 방어에 활용
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

def get_guard(d_day: int, now_time: datetime.time) -> dict:
    """D-day 기반 매매 제한 파라미터 반환"""
    if d_day < 0:
        return {
            "allow_new_longterm": True,
            "allow_new_daytrend": True,
            "sell_priority":      False,
            "score_penalty":      0,
            "pos_mult_adj":       0.0,
            "note":               "만기 경과 — 정상",
        }
    if d_day == 0:
        if datetime.time(9, 0) <= now_time < datetime.time(10, 30):
            note = "만기일 09:00~10:30 프로그램 매물 — 매수 전면 금지"
            allow_dt = False
        elif datetime.time(10, 30) <= now_time < datetime.time(13, 0):
            note = "만기일 10:30~13:00 방향 확인 — 관망"
            allow_dt = False
        elif datetime.time(13, 0) <= now_time < datetime.time(14, 0):
            note = "만기일 13:00~14:00 — 단타 소규모만 허용"
            allow_dt = True
        else:
            note = "만기일 14:00 이후 — 익절 우선, 신규매수 금지"
            allow_dt = False
        return {
            "allow_new_longterm": False,
            "allow_new_daytrend": allow_dt,
            "sell_priority":      now_time >= datetime.time(14, 0),
            "score_penalty":      3,
            "pos_mult_adj":       -0.5,
            "note":               note,
        }
    if d_day <= 2:
        return {
            "allow_new_longterm": False,
            "allow_new_daytrend": False,
            "sell_priority":      False,
            "score_penalty":      2,
            "pos_mult_adj":       -0.4,
            "note":               f"만기일 D-{d_day} — 신규매수 금지",
        }
    if d_day <= 5:
        return {
            "allow_new_longterm": False,
            "allow_new_daytrend": True,
            "sell_priority":      False,
            "score_penalty":      1,
            "pos_mult_adj":       -0.2,
            "note":               f"만기일 D-{d_day} — 장투 보류, 단타 소극적",
        }
    return {
        "allow_new_longterm": True,
        "allow_new_daytrend": True,
        "sell_priority":      False,
        "score_penalty":      0,
        "pos_mult_adj":       0.0,
        "note":               f"만기일 D-{d_day} — 영향 없음",
    }

def main():
    now         = datetime.datetime.now(KST)
    today       = datetime.date.today()
    expiry_date = get_next_expiry()
    d_day       = (expiry_date - today).days
    guard       = get_guard(d_day, now.time())

    # 만기일 당일 마감까지 남은 시간 (15:20 = 옵션 마지막 매매)
    deadline_str = ""
    if d_day == 0:
        deadline = now.replace(hour=15, minute=20, second=0, microsecond=0)
        remaining = deadline - now
        if remaining.total_seconds() > 0:
            h, rem = divmod(int(remaining.total_seconds()), 3600)
            m, s   = divmod(rem, 60)
            deadline_str = f"{h}시간 {m}분 후 마감" if h > 0 else f"{m}분 후 마감"
        else:
            deadline_str = "오늘 만기 마감 완료 (15:20)"

    print(f"\n{'='*50}")
    print(f"  StockPilot KR — 옵션만기 분석  {now.strftime('%Y%m%d %H:%M KST')}")
    print(f"{'='*50}")
    print(f"  다음 만기일 : {expiry_date} (D-{d_day})")
    print(f"  매매 제한   : {guard['note']}")
    print(f"  장투 허용   : {guard['allow_new_longterm']}")
    print(f"  단타 허용   : {guard['allow_new_daytrend']}")
    if deadline_str:
        print(f"  마감까지    : {deadline_str}")

    result = {
        "expiry_date":   str(expiry_date),
        "d_day":         d_day,
        "updated":       now.strftime("%Y-%m-%d %H:%M"),
        "deadline_time": "15:20",
        "deadline_str":  deadline_str,
        "guard":         guard,
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"  💾 {OUT_FILE} 저장 완료\n✅ 완료!")

if __name__ == "__main__":
    main()
