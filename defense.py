"""
defense.py — StockPilot KR 방어 로직 모듈
==========================================
갭다운 / 서킷브레이커 / 연속손실 / 종목급락 / 유동성부족 /
블랙리스트 / 일일손실한도 / 매매정지를 통합 관리합니다.

상태는 defense_state.json에 영속 저장됩니다.
"""

import json
import os
import requests
from datetime import datetime, date, timedelta
from typing import Optional

# ── 설정 ──────────────────────────────────────────────────────────────
STATE_FILE       = "defense_state.json"
DISCORD_WEBHOOK  = os.environ.get("DISCORD_WEBHOOK", "")

# 임계값 설정
THRESHOLD = {
    "gap_down_pct":          -0.020,   # -2%: 당일 갭다운 신규매수 금지
    "gap_down_tight_sl_pct": -0.030,   # -3%: 갭다운 심각, 손절선 상향
    "circuit_breaker_pct":   -0.080,   # -8%: 서킷브레이커 (KOSPI)
    "stock_crash_pct":       -0.070,   # -7%: 개별 종목 급락
    "stock_crash_hard_pct":  -0.120,   # -12%: 개별 종목 폭락 (즉시 시장가 손절)
    "consecutive_losses":     3,        # 3연패 → 48시간 정지
    "daily_loss_limit":      -150_000,  # 일일 손실 한도 (원) — 단타 자본 150만의 10%
    "total_loss_limit":      -500_000,  # 총 손실 한도 (원) — 발동 시 전면 중단
    "volume_drop_pct":        0.30,     # 거래량 30% 이하 → 유동성 부족
    "blacklist_hours":        72,       # 급락 종목 블랙리스트 시간
    "suspend_hours":          48,       # 연속손실 정지 시간
}


# ── 상태 로드/저장 ─────────────────────────────────────────────────────
def _default_state() -> dict:
    return {
        "consecutive_losses":    0,
        "trading_suspended":     False,
        "suspend_reason":        "",
        "suspend_until":         None,
        "blacklist":             {},       # {ticker: iso_datetime}
        "daily_loss":            0,
        "total_loss":            0,
        "last_reset_date":       str(date.today()),
        "gap_down_today":        False,
        "circuit_breaker_today": False,
        "hard_stop_active":      False,   # 총 손실 한도 초과 시 전면 중단
        "log":                   [],       # 최근 이벤트 로그 (최대 50건)
    }


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            # 날짜 리셋 처리
            if state.get("last_reset_date") != str(date.today()):
                state["daily_loss"]            = 0
                state["gap_down_today"]        = False
                state["circuit_breaker_today"] = False
                state["last_reset_date"]       = str(date.today())
            return state
        except Exception:
            pass
    return _default_state()


def save_state(state: dict):
    # 로그 최대 50건 유지
    if len(state.get("log", [])) > 50:
        state["log"] = state["log"][-50:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _log_event(state: dict, event: str, level: str = "INFO"):
    state.setdefault("log", []).append({
        "time":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "event": event,
    })


# ── Discord 알림 ───────────────────────────────────────────────────────
def _discord(message: str, urgent: bool = False):
    if not DISCORD_WEBHOOK:
        return
    prefix = "🚨 **[긴급]**" if urgent else "⚠️ **[경고]**"
    payload = {"content": f"{prefix} {message}"}
    try:
        requests.post(DISCORD_WEBHOOK, json=payload, timeout=5)
    except Exception as e:
        print(f"[defense] Discord 전송 실패: {e}")


# ════════════════════════════════════════════════════════════════════════
# ① 갭다운 감지
# ════════════════════════════════════════════════════════════════════════
def check_gap_down(
    open_price:  float,
    prev_close:  float,
    ticker:      str = "KOSPI",
) -> dict:
    """
    장 시작 시 갭다운 감지.
    반환: {"is_gap_down": bool, "gap_pct": float, "severity": str}
    severity: "none" / "moderate" / "severe"
    """
    if prev_close <= 0:
        return {"is_gap_down": False, "gap_pct": 0.0, "severity": "none"}

    gap_pct = (open_price - prev_close) / prev_close

    state = load_state()
    if gap_pct <= THRESHOLD["gap_down_pct"]:
        state["gap_down_today"] = True
        severity = "severe" if gap_pct <= THRESHOLD["gap_down_tight_sl_pct"] else "moderate"

        msg = (
            f"갭다운 감지 [{ticker}]\n"
            f"전일종가 {prev_close:,.0f} → 시초가 {open_price:,.0f} "
            f"({gap_pct:+.2%})\n"
            f"⛔ 당일 신규매수 전면 금지"
            + (" | 손절선 즉시 상향" if severity == "severe" else "")
        )
        _log_event(state, f"갭다운 {gap_pct:+.2%} [{ticker}]", "WARN")
        save_state(state)
        _discord(msg, urgent=(severity == "severe"))
        return {"is_gap_down": True, "gap_pct": gap_pct, "severity": severity}

    return {"is_gap_down": False, "gap_pct": gap_pct, "severity": "none"}


# ════════════════════════════════════════════════════════════════════════
# ② 서킷브레이커 감지
# ════════════════════════════════════════════════════════════════════════
def check_circuit_breaker(kospi_change_pct: float) -> bool:
    """
    KOSPI 등락률(소수점) 전달. -0.08 이하 → 서킷브레이커.
    True 반환 시 caller는 모든 주문을 취소해야 합니다.
    """
    if kospi_change_pct <= THRESHOLD["circuit_breaker_pct"]:
        state = load_state()
        state["circuit_breaker_today"] = True
        state["trading_suspended"]     = True
        state["suspend_reason"]        = "서킷브레이커 발동"
        # 익일 10:00 이후 재개
        resume = datetime.now().replace(hour=10, minute=0, second=0) + timedelta(days=1)
        state["suspend_until"]         = resume.isoformat()
        _log_event(state, f"서킷브레이커 {kospi_change_pct:+.2%}", "CRITICAL")
        save_state(state)
        _discord(
            f"서킷브레이커 발동! KOSPI {kospi_change_pct:+.2%}\n"
            f"모든 매매 즉시 중단. 재개 예정: {resume.strftime('%m/%d %H:%M')}",
            urgent=True,
        )
        return True
    return False


# ════════════════════════════════════════════════════════════════════════
# ③ 개별 종목 급락 감지
# ════════════════════════════════════════════════════════════════════════
def check_stock_crash(
    ticker:       str,
    current_price: float,
    prev_close:   float,
) -> dict:
    """
    종목 급락 감지. 반환: {"crashed": bool, "hard_crash": bool, "change_pct": float}
    - crashed:    -7% 이하 → 블랙리스트 + 경고
    - hard_crash: -12% 이하 → 즉시 시장가 손절 권고
    """
    if prev_close <= 0:
        return {"crashed": False, "hard_crash": False, "change_pct": 0.0}

    change = (current_price - prev_close) / prev_close
    result = {"crashed": False, "hard_crash": False, "change_pct": change}

    if change <= THRESHOLD["stock_crash_pct"]:
        result["crashed"] = True
        hours = THRESHOLD["blacklist_hours"]
        add_to_blacklist(ticker, hours=hours)

        if change <= THRESHOLD["stock_crash_hard_pct"]:
            result["hard_crash"] = True
            _discord(
                f"종목 폭락 [{ticker}] {change:+.2%}\n"
                f"⛔ 즉시 시장가 손절 권고 + {hours}h 블랙리스트",
                urgent=True,
            )
        else:
            _discord(
                f"종목 급락 [{ticker}] {change:+.2%}\n"
                f"⚠️ 손절 검토 + {hours}h 블랙리스트"
            )

        state = load_state()
        _log_event(state, f"종목급락 {ticker} {change:+.2%}", "WARN")
        save_state(state)

    return result


# ════════════════════════════════════════════════════════════════════════
# ④ 연속 손실 관리
# ════════════════════════════════════════════════════════════════════════
def record_trade_result(
    is_loss:     bool,
    amount:      float,   # 손익 금액 (손실이면 음수)
    ticker:      str = "",
) -> dict:
    """
    거래 결과 기록.
    반환: {"suspended": bool, "daily_loss": float, "consecutive_losses": int}
    """
    state = load_state()

    if is_loss:
        state["consecutive_losses"] += 1
        state["daily_loss"]         += amount   # amount는 음수
        state["total_loss"]         += amount
    else:
        state["consecutive_losses"]  = 0        # 수익 시 연패 리셋

    # 연속 손실 → 정지
    n = THRESHOLD["consecutive_losses"]
    if state["consecutive_losses"] >= n and not state["trading_suspended"]:
        hours  = THRESHOLD["suspend_hours"]
        resume = datetime.now() + timedelta(hours=hours)
        state["trading_suspended"] = True
        state["suspend_reason"]    = f"{n}연속 손실"
        state["suspend_until"]     = resume.isoformat()
        _log_event(state, f"{n}연속 손실 → {hours}h 정지", "CRITICAL")
        _discord(
            f"{n}연속 손실 발생! ({ticker})\n"
            f"자동매매 {hours}시간 일시정지\n"
            f"재개 예정: {resume.strftime('%m/%d %H:%M')}",
            urgent=True,
        )

    # 일일 손실 한도 초과 → 당일 정지
    if state["daily_loss"] <= THRESHOLD["daily_loss_limit"] and not state["trading_suspended"]:
        state["trading_suspended"] = True
        state["suspend_reason"]    = "일일 손실 한도 초과"
        resume = datetime.now().replace(hour=9, minute=0, second=0) + timedelta(days=1)
        state["suspend_until"]     = resume.isoformat()
        _log_event(state, f"일일 손실한도 초과: {state['daily_loss']:,.0f}원", "CRITICAL")
        _discord(
            f"일일 손실 한도 초과!\n"
            f"오늘 손실: {state['daily_loss']:+,.0f}원\n"
            f"당일 매매 중단. 내일 09:00 재개",
            urgent=True,
        )

    # 총 손실 한도 초과 → 전면 중단 (수동 해제 필요)
    if state["total_loss"] <= THRESHOLD["total_loss_limit"] and not state["hard_stop_active"]:
        state["hard_stop_active"]  = True
        state["trading_suspended"] = True
        state["suspend_reason"]    = "총 손실 한도 초과 (수동 해제 필요)"
        state["suspend_until"]     = None
        _log_event(state, f"총 손실한도 초과 {state['total_loss']:,.0f}원 — 전면중단", "CRITICAL")
        _discord(
            f"⛔ **총 손실 한도 초과**\n"
            f"누적 손실: {state['total_loss']:+,.0f}원\n"
            f"자동매매 전면 중단. 수동으로 defense_state.json 수정 필요!",
            urgent=True,
        )

    _log_event(
        state,
        f"{'손실' if is_loss else '수익'} {amount:+,.0f}원 [{ticker}] | "
        f"연패={state['consecutive_losses']} 일손실={state['daily_loss']:,.0f}",
        "INFO" if not is_loss else "WARN",
    )
    save_state(state)
    return {
        "suspended":          state["trading_suspended"],
        "daily_loss":         state["daily_loss"],
        "consecutive_losses": state["consecutive_losses"],
        "hard_stop":          state["hard_stop_active"],
    }


# ════════════════════════════════════════════════════════════════════════
# ⑤ 블랙리스트 관리
# ════════════════════════════════════════════════════════════════════════
def add_to_blacklist(ticker: str, hours: int = 72):
    state = load_state()
    expire = (datetime.now() + timedelta(hours=hours)).isoformat()
    state["blacklist"][ticker] = expire
    _log_event(state, f"블랙리스트 추가: {ticker} ({hours}h)")
    save_state(state)


def remove_from_blacklist(ticker: str):
    state = load_state()
    if ticker in state["blacklist"]:
        del state["blacklist"][ticker]
        _log_event(state, f"블랙리스트 해제: {ticker}")
        save_state(state)


def is_blacklisted(ticker: str) -> bool:
    state = load_state()
    if ticker in state["blacklist"]:
        expire_str = state["blacklist"][ticker]
        expire     = datetime.fromisoformat(expire_str)
        if datetime.now() < expire:
            remaining = expire - datetime.now()
            hrs = int(remaining.total_seconds() / 3600)
            print(f"[defense] {ticker} 블랙리스트 (잔여 {hrs}h)")
            return True
        else:
            # 만료 → 자동 제거
            del state["blacklist"][ticker]
            save_state(state)
    return False


def get_blacklist() -> dict:
    """현재 블랙리스트 반환 {ticker: 남은시간(시)}"""
    state = load_state()
    now   = datetime.now()
    result = {}
    for ticker, exp_str in list(state["blacklist"].items()):
        exp = datetime.fromisoformat(exp_str)
        if now < exp:
            result[ticker] = round((exp - now).total_seconds() / 3600, 1)
        else:
            del state["blacklist"][ticker]
    save_state(state)
    return result


# ════════════════════════════════════════════════════════════════════════
# ⑥ 매매 정지 확인
# ════════════════════════════════════════════════════════════════════════
def is_trading_suspended() -> dict:
    """
    매매 정지 여부 확인. 만료 시 자동 해제.
    반환: {"suspended": bool, "reason": str, "resume_at": str|None}
    """
    state = load_state()

    if state.get("hard_stop_active"):
        return {
            "suspended": True,
            "reason":    "총 손실 한도 초과 (수동 해제 필요)",
            "resume_at": None,
        }

    if state["trading_suspended"]:
        if state["suspend_until"]:
            until = datetime.fromisoformat(state["suspend_until"])
            if datetime.now() > until:
                # 자동 해제
                state["trading_suspended"]  = False
                state["suspend_reason"]     = ""
                state["consecutive_losses"] = 0
                _log_event(state, "자동매매 정지 해제 (시간 만료)")
                save_state(state)
                _discord("✅ 자동매매 재개 (정지 시간 만료)")
                return {"suspended": False, "reason": "", "resume_at": None}

        return {
            "suspended": True,
            "reason":    state.get("suspend_reason", "알 수 없음"),
            "resume_at": state.get("suspend_until"),
        }

    return {"suspended": False, "reason": "", "resume_at": None}


def manual_resume():
    """수동 정지 해제 (hard_stop 포함). CLI에서 호출 가능."""
    state = load_state()
    state["trading_suspended"]  = False
    state["hard_stop_active"]   = False
    state["suspend_reason"]     = ""
    state["suspend_until"]      = None
    state["consecutive_losses"] = 0
    _log_event(state, "수동 정지 해제", "INFO")
    save_state(state)
    _discord("✅ 자동매매 수동 재개")
    print("[defense] 매매 정지 수동 해제 완료")


# ════════════════════════════════════════════════════════════════════════
# ⑦ 유동성 부족 감지
# ════════════════════════════════════════════════════════════════════════
def check_liquidity(
    ticker:         str,
    current_volume: int,
    prev_volume:    int,
) -> bool:
    """
    거래량이 전일 대비 30% 이하면 True (매수 보류).
    매도 시에는 분할 매도 권장.
    """
    if prev_volume <= 0:
        return False
    ratio = current_volume / prev_volume
    if ratio < THRESHOLD["volume_drop_pct"]:
        print(f"[defense] {ticker} 유동성 부족: 거래량 {ratio:.0%} (전일 대비)")
        return True
    return False


# ════════════════════════════════════════════════════════════════════════
# ⑧ 종합 매수 가능 여부 판단 (모든 방어 조건 통합)
# ════════════════════════════════════════════════════════════════════════
def can_buy(ticker: str, trade_type: str = "daytrend") -> dict:
    """
    trader.py에서 매수 전 단일 호출로 모든 방어 조건 확인.

    trade_type: "longterm" | "daytrend"

    반환:
    {
      "allowed": bool,
      "reason":  str,    # 거부 사유 (allowed=True이면 "OK")
    }
    """
    # 1. 전면 중단 / 정지 확인
    suspend = is_trading_suspended()
    if suspend["suspended"]:
        return {"allowed": False, "reason": f"매매정지: {suspend['reason']}"}

    # 2. 블랙리스트 확인
    if is_blacklisted(ticker):
        return {"allowed": False, "reason": f"{ticker} 블랙리스트"}

    # 3. 갭다운 당일
    state = load_state()
    if state.get("gap_down_today"):
        return {"allowed": False, "reason": "갭다운 감지 — 당일 신규매수 금지"}

    # 4. 서킷브레이커 당일
    if state.get("circuit_breaker_today"):
        return {"allowed": False, "reason": "서킷브레이커 발동 — 당일 매매 중단"}

    return {"allowed": True, "reason": "OK"}


# ════════════════════════════════════════════════════════════════════════
# ⑨ 현황 요약 출력
# ════════════════════════════════════════════════════════════════════════
def get_status_summary() -> dict:
    state  = load_state()
    sus    = is_trading_suspended()
    bl     = get_blacklist()
    return {
        "trading_suspended":  sus["suspended"],
        "suspend_reason":     sus["reason"],
        "resume_at":          sus["resume_at"],
        "consecutive_losses": state["consecutive_losses"],
        "daily_loss":         state["daily_loss"],
        "total_loss":         state["total_loss"],
        "gap_down_today":     state.get("gap_down_today", False),
        "circuit_breaker":    state.get("circuit_breaker_today", False),
        "hard_stop":          state.get("hard_stop_active", False),
        "blacklist":          bl,
        "recent_log":         state.get("log", [])[-10:],
    }


# ── 단독 실행 / 관리 CLI ───────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        s = get_status_summary()
        print("=" * 55)
        print("  defense.py 현황")
        print("=" * 55)
        print(f"  매매정지   : {'YES (' + s['suspend_reason'] + ')' if s['trading_suspended'] else 'NO'}")
        print(f"  재개예정   : {s['resume_at'] or '-'}")
        print(f"  연속손실   : {s['consecutive_losses']}회")
        print(f"  일일손실   : {s['daily_loss']:+,.0f}원")
        print(f"  누적손실   : {s['total_loss']:+,.0f}원")
        print(f"  갭다운     : {s['gap_down_today']}")
        print(f"  서킷브레이커: {s['circuit_breaker']}")
        print(f"  전면중단   : {s['hard_stop']}")
        print(f"  블랙리스트 : {s['blacklist'] or '없음'}")
        print(f"\n  최근 이벤트:")
        for log in s["recent_log"]:
            print(f"    [{log['level']}] {log['time']} {log['event']}")

    elif cmd == "resume":
        manual_resume()

    elif cmd == "blacklist":
        # python defense.py blacklist 005380 72
        ticker = sys.argv[2] if len(sys.argv) > 2 else ""
        hours  = int(sys.argv[3]) if len(sys.argv) > 3 else 72
        if ticker:
            add_to_blacklist(ticker, hours)
            print(f"블랙리스트 추가: {ticker} ({hours}h)")

    elif cmd == "unblacklist":
        ticker = sys.argv[2] if len(sys.argv) > 2 else ""
        if ticker:
            remove_from_blacklist(ticker)
            print(f"블랙리스트 해제: {ticker}")

    else:
        print("사용법: python defense.py [status|resume|blacklist|unblacklist]")
