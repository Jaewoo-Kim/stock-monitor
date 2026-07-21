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

보조 신호 — 단기 낙폭과대(oversold_flag):
  서비스 취지(CLAUDE.md)상 일간 등락 자체를 헤드라인 신호로 쓰지 않는다. 다만
  "그날그날 주가에 따라 타이밍이 좋아질 수 있다"(단기 과매도 되돌림 후보)는
  현실을 반영하기 위해, 1주 급락 + RSI 과매도 + 20일선 이격 과다가 동시에
  나타날 때만 별도 플래그로 노출한다. 위 buy/watch/hold/caution 판정(사이클
  방향 기반)은 그대로 두고, timing_score에 소폭 가산 + UI에 별도 배지로만
  표시 — 사이클 하강 중에도 "매수 적기"로 둔갑시키지 않는다.

  백테스트 검증(src/analysis/backtest.py `_run_oversold`, 이벤트 36건, 20개 산업
  분산): forward 5·10거래일은 baseline 대비 초과수익(+1.9%p/+0.0%p, 승률
  63.9%/63.9% vs 53.6%/50.9%)이 확인되나, forward 20거래일은 baseline보다
  오히려 낮음(-0.58% vs +2.76%, 승률 50.0% vs 46.7% — 거의 우위 없음). 즉
  효과는 초단기(~1주)에 한정되고 1개월 시계에서는 반전 위험이 있다 — 그래서
  timing_state를 바꾸지 않고 소폭 가산에 그친다.

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

# 단기 낙폭과대 판정 임계값 — 세 조건 동시 충족 시에만 플래그(노이즈 최소화)
OVERSOLD_RET5D_MAX = -7.0   # 5거래일(1주) 수익률 -7% 이하
OVERSOLD_RSI_MAX   = 32.0   # RSI(14) 32 이하 (과매도)
OVERSOLD_DEV20_MAX = -8.0   # 20일선 대비 이격도 -8% 이하


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


def _dev_from_ma(values: list[float], ma: float | None) -> float | None:
    """최근 종가의 n일 이동평균 대비 이격도 %."""
    if ma is None or ma == 0 or not values:
        return None
    return round((values[-1] - ma) / ma * 100, 2)


def _oversold_flag(ret_5d: float | None, rsi14: float | None, dev_ma20: float | None) -> int | None:
    """1주 급락 + RSI 과매도 + 20일선 이격 과다 동시 충족 시 1 (단기 낙폭과대).

    사이클 방향과 무관한 보조 신호 — 세 조건 모두 있어야 판정 가능.
    """
    if ret_5d is None or rsi14 is None or dev_ma20 is None:
        return None
    return 1 if (
        ret_5d <= OVERSOLD_RET5D_MAX
        and rsi14 <= OVERSOLD_RSI_MAX
        and dev_ma20 <= OVERSOLD_DEV20_MAX
    ) else 0


def _high_break(values: list[float], window: int = HIGH_WINDOW) -> int | None:
    if len(values) < 2:
        return None
    recent = values[-window:] if len(values) >= window else values
    return 1 if values[-1] >= max(recent) else 0


def _market_closes(con, code: str = "_UNIV") -> list[float]:
    rows = con.execute(
        "SELECT close FROM market_index_history WHERE code=? ORDER BY date ASC", (code,)
    ).fetchall()
    return [r[0] for r in rows]


def _ret_offset(values: list[float], lookback: int, offset: int) -> float | None:
    """offset일 전 시점 기준 lookback 수익률 %."""
    i = len(values) - 1 - offset
    j = i - lookback
    if j < 0 or values[j] <= 0:
        return None
    return (values[i] - values[j]) / values[j] * 100


def _rep_stock_closes(con, level2_id: str) -> list[list[float]]:
    """산업 내 대표종목별 종가 시계열 리스트."""
    rows = con.execute(
        """SELECT ph.ticker, ph.close FROM price_history ph
           JOIN companies c ON ph.ticker = c.ticker
           WHERE c.is_representative=1 AND c.is_etf=0 AND c.level2_id=?
           ORDER BY ph.ticker, ph.date""",
        (level2_id,),
    ).fetchall()
    from collections import OrderedDict
    series: "OrderedDict[str, list]" = OrderedDict()
    for ticker, close in rows:
        series.setdefault(ticker, []).append(close)
    return list(series.values())


def _breadth(stock_series: list[list[float]], ma: int = 60, offset: int = 0) -> float | None:
    """offset일 전 기준, 종가 > ma일선 종목 비율 (%)."""
    above = total = 0
    for s in stock_series:
        end = len(s) - offset
        if end < ma + 1:
            continue
        window = s[end - ma:end]
        if not window:
            continue
        cur = s[end - 1]
        avg = sum(window) / len(window)
        total += 1
        if cur > avg:
            above += 1
    if total == 0:
        return None
    return round(above / total * 100, 1)


def calc_price_indicators(con, level2_id: str) -> dict:
    closes = _closes(con, level2_id)
    n = len(closes)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    trend_up = None
    if ma20 is not None and ma60 is not None:
        trend_up = 1 if ma20 > ma60 else 0

    # 상대강도(RS): 업종 3M수익률 − 시장 프록시 3M수익률
    mkt = _market_closes(con)
    rs_3m = rs_up = None
    if n > MIN_DAYS_TREND and len(mkt) > MIN_DAYS_TREND:
        ind_r = _ret_offset(closes, MIN_DAYS_TREND, 0)
        mkt_r = _ret_offset(mkt,    MIN_DAYS_TREND, 0)
        if ind_r is not None and mkt_r is not None:
            rs_3m = round(ind_r - mkt_r, 2)
            ind_p = _ret_offset(closes, MIN_DAYS_TREND, MIN_DAYS_MOM)
            mkt_p = _ret_offset(mkt,    MIN_DAYS_TREND, MIN_DAYS_MOM)
            if ind_p is not None and mkt_p is not None:
                rs_up = 1 if rs_3m > (ind_p - mkt_p) else 0

    # 폭(Breadth): 산업 내 60일선 위 종목 비율 + 1개월 전 대비 상승 여부
    stock_series = _rep_stock_closes(con, level2_id)
    breadth = _breadth(stock_series, ma=60, offset=0)
    breadth_prev = _breadth(stock_series, ma=60, offset=MIN_DAYS_MOM)
    breadth_up = None
    if breadth is not None and breadth_prev is not None:
        breadth_up = 1 if breadth > breadth_prev else 0

    # 단기 낙폭과대(과매도 되돌림 후보) — 사이클 방향과 별개의 보조 신호
    rsi14 = _rsi(closes)
    ret_5d = _return_pct(closes, 5)
    dev_ma20 = _dev_from_ma(closes, ma20)

    return {
        "idx_ma20":       round(ma20, 2) if ma20 else None,
        "idx_ma60":       round(ma60, 2) if ma60 else None,
        "idx_trend_up":   trend_up,
        "idx_ret_4w":     _return_pct(closes, MIN_DAYS_MOM),
        "idx_ret_12w":    _return_pct(closes, MIN_DAYS_TREND),
        "idx_rsi14":      rsi14,
        "idx_high_break": _high_break(closes),
        "idx_rs_3m":      rs_3m,
        "idx_rs_up":      rs_up,
        "breadth_pct":    breadth,
        "breadth_up":     breadth_up,
        "idx_ret_5d":     ret_5d,
        "idx_dev_ma20":   dev_ma20,
        "oversold_flag":  _oversold_flag(ret_5d, rsi14, dev_ma20),
        "price_days":     n,
    }


# ─────────────────────────────────────
# 방향(애널리스트) + 가격 → 타이밍 판정
# ─────────────────────────────────────

def _latest_l0(con, level2_id: str) -> dict | None:
    row = con.execute(
        "SELECT driver_state, driver_score FROM l0_signals WHERE level2_id=?",
        (level2_id,),
    ).fetchone()
    if row is None:
        return None
    return {"driver_state": row[0], "driver_score": row[1]}


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


def _direction_up(cyc: dict | None, l0: dict | None = None) -> bool:
    """상승 방향인가 — L0 업황(가장 빠름) + 애널리스트 신호."""
    # L0 업황 동인이 바닥 통과/상승이면 방향 ON (목표가보다 먼저)
    if l0 and l0.get("driver_state") in ("turning_up", "rising"):
        return True
    if not cyc:
        return False
    if cyc["phase"] in ("전환", "확장"):
        return True
    if cyc.get("lead_first_turn") == 1 and (cyc.get("lead_accel") or 0) > 0:
        return True
    if (cyc.get("composite") or 0) > 0.15:
        return True
    return False


def classify_timing(cyc: dict | None, px: dict, l0: dict | None = None) -> tuple[str, float]:
    """(timing_state, timing_score 0~100).

    방향(L0 업황 + 애널리스트)과 가격 확인을 결합. 리포트가 부족해 방향이 비어도
    가격 추세가 강하면 watch(가격만)으로 노출 — UI에서 근거 구분 표시.
    """
    days = px["price_days"]
    price_ready = days >= MIN_DAYS_MOM

    # 둔화·침체는 가격과 무관하게 주의 (단 L0가 바닥 통과면 예외적으로 관찰)
    phase = cyc["phase"] if cyc else "관측부족"
    l0_up = bool(l0 and l0.get("driver_state") in ("turning_up", "rising"))
    if phase in ("둔화", "침체") and not l0_up:
        return "caution", _score(cyc, px, base=20)

    direction_up = _direction_up(cyc, l0)

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
    # L1 산업 전체 전환: 상대강도·폭
    if (px.get("idx_rs_3m") or 0) > 0:      # 시장 대비 초과상승
        s += 5
    if px.get("idx_rs_up") == 1:            # 상대강도 개선 중
        s += 4
    if (px.get("breadth_pct") or 0) >= 60 and px.get("breadth_up") == 1:  # 산업 전반 참여 확대
        s += 5
    rsi = px["idx_rsi14"]
    if rsi is not None and rsi > 75:   # 과매수 → 추격매수 감점
        s -= 6
    if px.get("oversold_flag") == 1:   # 단기 낙폭과대 → 되돌림 여지 소폭 가산(주 신호는 아님)
        s += 8
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
            l0  = _latest_l0(con, level2_id)
            state, score = classify_timing(cyc, px, l0)
            con.execute(
                """
                INSERT OR REPLACE INTO timing_signals
                    (level2_id, calc_date, idx_ma20, idx_ma60, idx_trend_up,
                     idx_ret_4w, idx_ret_12w, idx_rsi14, idx_high_break,
                     idx_rs_3m, idx_rs_up, breadth_pct, breadth_up,
                     idx_ret_5d, idx_dev_ma20, oversold_flag,
                     price_days, timing_state, timing_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    level2_id, calc_date, px["idx_ma20"], px["idx_ma60"],
                    px["idx_trend_up"], px["idx_ret_4w"], px["idx_ret_12w"],
                    px["idx_rsi14"], px["idx_high_break"],
                    px["idx_rs_3m"], px["idx_rs_up"], px["breadth_pct"], px["breadth_up"],
                    px["idx_ret_5d"], px["idx_dev_ma20"], px["oversold_flag"],
                    px["price_days"], state, score,
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
