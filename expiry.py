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
            note        = "만기일 오전 — 프로그램 매물 집중"
            action_note = "🔴 신규진입 금지. 프로그램 매물이 쏟아지는 구간입니다. 매수하면 하락에 그대로 노출됩니다."
            allow_dt    = False
        elif datetime.time(10, 30) <= now_time < datetime.time(13, 0):
            note        = "만기일 오전장 후반 — 방향 확인 중"
            action_note = "🟡 관망하세요. 방향이 정해지지 않은 구간입니다. 기존 보유 유지, 신규는 13시 이후 확인 후 판단하세요."
            allow_dt    = False
        elif datetime.time(13, 0) <= now_time < datetime.time(14, 0):
            note        = "만기일 오후 — 방향 확인 후 소규모 단타 가능"
            action_note = "🟡 방향이 잡혔다면 단타 소규모만 가능합니다. 무리한 진입은 금물, 빠른 익절이 원칙입니다."
            allow_dt    = True
        else:
            note        = "만기일 마감 전 — 익절 청산 구간"
            action_note = "🔴 신규진입 금지. 지금 들어가면 마감 변동성에 휘말립니다. 보유 중이라면 익절 가능한 것부터 정리하세요."
            allow_dt    = False
        return {
            "allow_new_longterm": False,
            "allow_new_daytrend": allow_dt,
            "sell_priority":      now_time >= datetime.time(14, 0),
            "score_penalty":      3,
            "pos_mult_adj":       -0.5,
            "note":               note,
            "action_note":        action_note,
        }
    if d_day <= 2:
        return {
            "allow_new_longterm": False,
            "allow_new_daytrend": False,
            "sell_priority":      False,
            "score_penalty":      2,
            "pos_mult_adj":       -0.4,
            "note":               f"만기일 D-{d_day} — 변동성 최고조",
            "action_note":        f"🔴 관망하세요. 만기일 {d_day}일 전으로 기관/외인 포지션 조정이 집중됩니다. 신규진입 시 예상치 못한 방향으로 튈 수 있습니다.",
        }
    if d_day <= 5:
        return {
            "allow_new_longterm": False,
            "allow_new_daytrend": True,
            "sell_priority":      False,
            "score_penalty":      1,
            "pos_mult_adj":       -0.2,
            "note":               f"만기일 D-{d_day} — 경계 구간",
            "action_note":        f"🟡 장투 신규매수는 자제하세요. 단타는 소규모로만 접근 가능합니다. 만기일이 {d_day}일 남아 변동성이 서서히 높아지는 구간입니다.",
        }
    return {
        "allow_new_longterm": True,
        "allow_new_daytrend": True,
        "sell_priority":      False,
        "score_penalty":      0,
        "pos_mult_adj":       0.0,
        "note":               f"만기일 D-{d_day} — 영향 없음",
        "action_note":        f"✅ 전략대로 진행하세요. 만기일까지 {d_day}일 여유가 있어 만기 영향은 없습니다.",
    }

def main():
    now         = datetime.datetime.now(KST)
    today       = datetime.date.today()
    expiry_date = get_next_expiry()
    if expiry_date is None:
        print("  ⚠️ 만기일 계산 실패 — 기본값 사용")
        expiry_date = datetime.date.today() + datetime.timedelta(days=30)
    d_day = (expiry_date - today).days
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
