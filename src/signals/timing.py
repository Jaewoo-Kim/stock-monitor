"""매수 타이밍 엔진.

설계 (사용자 결정): 애널리스트 신호(방향) + 가격 확인(진입 타이밍) 결합.

  방향  = cycle_signals (목표가/EPS 리비전 → cycle_phase, composite_score)
  확인  = industry_index_history 가격 지표
          (MA20/MA60 정배열, 4주·12주 모멘텀, RSI(14), 8주 신고가 돌파)

판정:
  buy     — 방향 상승전환 + 가격 정배열 + 모멘텀 양(+)  → 매수 적기
  watch   — 방향은 상승이나 가격 미확인(정배열 전)      → 관찰
  hold    — 방향 신호 약함                              → 관망
  caution — 둔화·침체                                   → 주의
  관측부족 — 신호/가격 데이터 부족

산출물: timing_signals 테이블.
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from db import connect

log = logging.getLogger(__name__)

MIN_DAYS_TREND = 60   # MA60 계산 최소 거래일
MIN_DAYS_MOM   = 20   # 4주 모멘텀 최소 거래일
RSI_PERIOD     = 14
HIGH_WINDOW    = 40   # 8주 ≈ 40 거래일


# ─────────────────────────────────────
# 가격 지표 계산
# ─────────────────────────────────────

def _closes(con, level2_id: str) -> list[float]:
    """industry_index_history 종가 시계열 (오름차순)."""
    rows = con.execute(
        "SELECT close FROM industry_index_history WHERE level2_id=? ORDER BY date ASC",
        (level2_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _return_pct(values: list[float], lookback: int) -> float | None:
    if len(values) <= lookback:
        return None
    base = values[-1 - lookback]
    if base <= 0:
        return None
    return round((values[-1] - base) / base * 100, 2)


def _rsi(values: list[float], period: int = RSI_PERIOD) -> float | None:
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(len(values) - period, len(values)):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def _high_break(values: list[float], window: int = HIGH_WINDOW) -> int | None:
    if len(values) < 2:
        return None
    recent = values[-window:] if len(values) >= window else values
    return 1 if values[-1] >= max(recent) else 0


def calc_price_indicators(con, level2_id: str) -> dict:
    closes = _closes(con, level2_id)
    n = len(closes)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    trend_up = None
    if ma20 is not None and ma60 is not None:
        trend_up = 1 if ma20 > ma60 else 0
    return {
        "idx_ma20":       round(ma20, 2) if ma20 else None,
        "idx_ma60":       round(ma60, 2) if ma60 else None,
        "idx_trend_up":   trend_up,
        "idx_ret_4w":     _return_pct(closes, MIN_DAYS_MOM),
        "idx_ret_12w":    _return_pct(closes, MIN_DAYS_TREND),
        "idx_rsi14":      _rsi(closes),
        "idx_high_break": _high_break(closes),
        "price_days":     n,
    }


# ─────────────────────────────────────
# 방향(애널리스트) + 가격 → 타이밍 판정
# ─────────────────────────────────────

def _latest_cycle(con, level2_id: str) -> dict | None:
    row = con.execute(
        """
        SELECT cycle_phase, composite_score, lead_first_turn, lead_accel,
               conf_breadth, n_reports
        FROM cycle_signals
        WHERE level2_id=?
        ORDER BY calc_date DESC LIMIT 1
        """,
        (level2_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "phase": row[0], "composite": row[1], "lead_first_turn": row[2],
        "lead_accel": row[3], "conf_breadth": row[4], "n_reports": row[5],
    }


def _direction_up(cyc: dict | None) -> bool:
    """애널리스트 신호상 상승 방향인가."""
    if not cyc:
        return False
    if cyc["phase"] in ("전환", "확장"):
        return True
    if cyc.get("lead_first_turn") == 1 and (cyc.get("lead_accel") or 0) > 0:
        return True
    if (cyc.get("composite") or 0) > 0.15:
        return True
    return False


def classify_timing(cyc: dict | None, px: dict) -> tuple[str, float]:
    """(timing_state, timing_score 0~100).

    방향(애널리스트)과 가격 확인을 결합. 리포트가 부족해 방향이 비어도
    가격 추세가 강하면 watch(가격만)으로 노출 — UI에서 근거 구분 표시.
    """
    days = px["price_days"]
    price_ready = days >= MIN_DAYS_MOM

    # 둔화·침체는 가격과 무관하게 주의
    phase = cyc["phase"] if cyc else "관측부족"
    if phase in ("둔화", "침체"):
        return "caution", _score(cyc, px, base=20)

    direction_up = _direction_up(cyc)

    # 가격 확인 요소
    trend_up = px["idx_trend_up"] == 1
    mom_up   = (px["idx_ret_4w"] or 0) > 0
    high_brk = px["idx_high_break"] == 1
    price_up = price_ready and trend_up and mom_up

    if direction_up and price_up:
        # 방향·가격 모두 확인 → 매수 적기
        return "buy", _score(cyc, px, base=70)
    if direction_up and not price_up:
        # 방향○ 가격 대기 → 관찰
        return "watch", _score(cyc, px, base=55)
    if price_up:
        # 가격○ 방향(애널리스트) 대기 → 관찰(가격만)
        return "watch", _score(cyc, px, base=48)

    # 근거 없음
    if not price_ready and phase == "관측부족":
        return "관측부족", _score(cyc, px, base=10)
    if phase == "관측부족" and (cyc is None or (cyc.get("n_reports") or 0) == 0) \
       and not trend_up:
        return "관측부족", _score(cyc, px, base=12)
    return "hold", _score(cyc, px, base=35)


def _score(cyc: dict | None, px: dict, base: float) -> float:
    """base에 방향 강도·가격 확인을 가산해 0~100."""
    s = base
    if cyc and cyc.get("composite") is not None:
        s += max(-15, min(15, cyc["composite"] * 15))
    if px["idx_trend_up"] == 1:
        s += 6
    if (px["idx_ret_4w"] or 0) > 0:
        s += 4
    if px["idx_high_break"] == 1:
        s += 4
    rsi = px["idx_rsi14"]
    if rsi is not None and rsi > 75:   # 과매수 → 추격매수 감점
        s -= 6
    return round(max(0, min(100, s)), 1)


# ─────────────────────────────────────
# 메인
# ─────────────────────────────────────

def run(con=None, calc_date: str | None = None) -> None:
    own = con is None
    if own:
        con = connect()
    if calc_date is None:
        calc_date = date.today().isoformat()

    try:
        level2_ids = [r[0] for r in con.execute(
            "SELECT level2_id FROM industries ORDER BY level2_id"
        ).fetchall()]

        saved = 0
        for level2_id in level2_ids:
            px  = calc_price_indicators(con, level2_id)
            cyc = _latest_cycle(con, level2_id)
            state, score = classify_timing(cyc, px)
            con.execute(
                """
                INSERT OR REPLACE INTO timing_signals
                    (level2_id, calc_date, idx_ma20, idx_ma60, idx_trend_up,
                     idx_ret_4w, idx_ret_12w, idx_rsi14, idx_high_break,
                     price_days, timing_state, timing_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    level2_id, calc_date, px["idx_ma20"], px["idx_ma60"],
                    px["idx_trend_up"], px["idx_ret_4w"], px["idx_ret_12w"],
                    px["idx_rsi14"], px["idx_high_break"], px["price_days"],
                    state, score,
                ),
            )
            saved += 1
        con.commit()
        log.info("timing_signals 저장 완료 — %d건 (calc_date=%s)", saved, calc_date)
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
