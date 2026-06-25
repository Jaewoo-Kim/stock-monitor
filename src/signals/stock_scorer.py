"""종목 매수후보 스코어러.

펀더멘털 모멘텀(composite) + 종합 매수후보 점수(buy_score).

  모멘텀 축:
    eps_rev_z    — EPS 리비전 z-score (선행, 4주 윈도우)
    margin_trend — 영업이익률 2분기 추세 (+1/0/-1)
    composite    — 0.5*norm(eps_rev_z) + 0.5*margin_trend  (-1~1)

  매수후보 종합 (사용자 결정: 상승여력 + 모멘텀 + 점유율):
    upside_pct   — 최신 평균 목표가 대비 현재가 상승여력 %
    share_bonus  — 글로벌 점유율 가산 (0~1)
    buy_score    — 0~100, 가중: 상승여력 0.45 + 모멘텀 0.30 + 점유율 0.25

출력: stock_scores 테이블
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, stdev

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from db import connect

log = logging.getLogger(__name__)

MIN_REVISIONS = 2   # z-score 계산에 필요한 최소 리비전 건수


# ─────────────────────────────────────
# EPS 리비전 z-score
# ─────────────────────────────────────

def _eps_revisions(con, calc_date: str, window_weeks: int = 4) -> dict[str, list[float]]:
    """ticker → 윈도우 내 EPS 리비전 % 목록."""
    since = (date.fromisoformat(calc_date) - timedelta(weeks=window_weeks)).isoformat()
    rows = con.execute(
        """
        SELECT ticker, eps_estimate, prev_eps_est
        FROM report_events
        WHERE published_date BETWEEN ? AND ?
          AND eps_estimate IS NOT NULL
          AND prev_eps_est IS NOT NULL
          AND prev_eps_est != 0
        """,
        (since, calc_date),
    ).fetchall()

    revs: dict[str, list[float]] = {}
    for ticker, eps, prev in rows:
        pct = (eps - prev) / abs(prev) * 100
        revs.setdefault(ticker, []).append(pct)
    return revs


def _calc_eps_z(revisions: dict[str, list[float]]) -> dict[str, float | None]:
    """산업 내 EPS 리비전 평균의 z-score. 종목별 평균을 먼저 구한 뒤 z-score."""
    avgs: dict[str, float] = {}
    for ticker, vals in revisions.items():
        if len(vals) >= 1:
            avgs[ticker] = mean(vals)

    if len(avgs) < MIN_REVISIONS:
        return {t: None for t in revisions}

    vals_list = list(avgs.values())
    mu  = mean(vals_list)
    sd  = stdev(vals_list) if len(vals_list) > 1 else 1.0
    if sd == 0:
        sd = 1.0

    return {ticker: (avg - mu) / sd for ticker, avg in avgs.items()}


# ─────────────────────────────────────
# 영업이익률 추세
# ─────────────────────────────────────

def _margin_trend(con, ticker: str) -> int | None:
    """최근 3분기 영업이익률로 추세 판단. +1(상승) / 0(보합) / -1(하락) / None(데이터없음)."""
    rows = con.execute(
        """
        SELECT op_margin FROM company_fundamentals
        WHERE ticker = ? AND op_margin IS NOT NULL AND source = 'DART'
        ORDER BY period_end DESC
        LIMIT 3
        """,
        (ticker,),
    ).fetchall()

    margins = [r[0] for r in rows]
    if len(margins) < 2:
        return None

    # 최근 2개 분기 비교 (rows[0]=최신, rows[1]=전분기)
    diff = margins[0] - margins[1]
    if diff > 0.5:
        return 1
    elif diff < -0.5:
        return -1
    return 0


# ─────────────────────────────────────
# 점수 정규화 (-1~1 클리핑)
# ─────────────────────────────────────

def _norm_z(z: float | None, cap: float = 2.0) -> float | None:
    """z-score를 [-1, 1]로 클리핑·정규화."""
    if z is None:
        return None
    return max(-1.0, min(1.0, z / cap))


def _composite(eps_z_norm: float | None, mtrend: int | None) -> float | None:
    """있는 축의 단순 평균 (-1~1). 한 축만 있으면 그 값 그대로 (증폭 없음)."""
    parts: list[float] = []
    if eps_z_norm is not None:
        parts.append(eps_z_norm)
    if mtrend is not None:
        parts.append(float(mtrend))
    if not parts:
        return None
    return sum(parts) / len(parts)


# ─────────────────────────────────────
# 상승여력 (목표가 대비 현재가)
# ─────────────────────────────────────

def _latest_close(con, ticker: str) -> float | None:
    row = con.execute(
        "SELECT close FROM price_history WHERE ticker=? ORDER BY date DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return row[0] if row else None


def _upside(con, ticker: str, calc_date: str, weeks: int = 12) -> tuple[float | None, int]:
    """최근 12주 평균 목표가 대비 현재가 상승여력 %. (upside_pct, n_targets)."""
    since = (date.fromisoformat(calc_date) - timedelta(weeks=weeks)).isoformat()
    rows = con.execute(
        """SELECT target_price FROM report_events
           WHERE ticker=? AND target_price IS NOT NULL AND target_price > 0
             AND published_date >= ?""",
        (ticker, since),
    ).fetchall()
    targets = [r[0] for r in rows]
    close = _latest_close(con, ticker)
    if not targets or not close or close <= 0:
        return None, 0
    avg_target = mean(targets)
    return round((avg_target - close) / close * 100, 1), len(targets)


# ─────────────────────────────────────
# 글로벌 점유율 가산
# ─────────────────────────────────────

def _share_bonus(con, ticker: str) -> float | None:
    """글로벌 점유율 순위 → 가산점 0~1. 데이터 없으면 None."""
    n = con.execute(
        "SELECT COUNT(*) FROM company_market_share WHERE ticker=?", (ticker,)
    ).fetchone()[0]
    if n == 0:
        return None
    best_rank = con.execute(
        "SELECT MIN(global_rank) FROM company_market_share WHERE ticker=?", (ticker,)
    ).fetchone()[0]
    if best_rank is None:
        return 0.5          # 점유율 데이터는 있으나 순위 미상 (선두권 표기 등)
    if best_rank == 1:
        return 1.0
    if best_rank <= 3:
        return 0.7
    return 0.4


# ─────────────────────────────────────
# 종합 매수후보 점수
# ─────────────────────────────────────

def _buy_score(upside_pct: float | None, composite: float | None,
               share_bonus: float | None) -> float | None:
    """상승여력 0.45 + 모멘텀 0.30 + 점유율 0.25 → 0~100.

    누락 축은 중립값 0.5로 처리(한 축만으로 만점 방지). 실제 데이터가
    하나도 없으면 None.
    """
    real = 0
    u = 0.5
    if upside_pct is not None:
        u = max(0.0, min(1.0, upside_pct / 40.0))   # 40%+ → 1.0, 음수 → 0
        real += 1
    m = 0.5
    if composite is not None:
        m = (composite + 1) / 2                       # -1~1 → 0~1
        real += 1
    s = 0.5
    if share_bonus is not None:
        s = share_bonus
        real += 1
    if real == 0:
        return None
    val = u * 0.45 + m * 0.30 + s * 0.25
    return round(val * 100, 1)


# ─────────────────────────────────────
# 메인
# ─────────────────────────────────────

def run(con=None, calc_date: str | None = None) -> None:
    own_con = con is None
    if own_con:
        con = connect()

    if calc_date is None:
        calc_date = date.today().isoformat()

    try:
        # 대표 종목 + 산업
        companies = con.execute(
            """
            SELECT c.ticker, c.level2_id
            FROM companies c
            WHERE c.is_representative = 1 AND c.is_etf = 0
              AND c.level2_id IS NOT NULL
            """
        ).fetchall()

        if not companies:
            log.warning("대표 종목 없음 — stock_scorer 스킵")
            return

        log.info("종목 점수 계산 시작 — %d개 종목, calc_date=%s", len(companies), calc_date)

        # EPS 리비전 z-score (전체 종목 공통 계산)
        revisions = _eps_revisions(con, calc_date)
        eps_z_map = _calc_eps_z(revisions)

        # 영업이익률 추세 (종목별)
        mtrend_map: dict[str, int | None] = {}
        for ticker, _ in companies:
            mtrend_map[ticker] = _margin_trend(con, ticker)

        # 종목별 점수 계산 (모멘텀 + 상승여력 + 점유율 → buy_score)
        by_level2: dict[str, list[tuple[str, float]]] = {}
        scores: dict[str, dict] = {}

        for ticker, level2_id in companies:
            eps_z_raw  = eps_z_map.get(ticker)
            eps_z_norm = _norm_z(eps_z_raw)
            mtrend     = mtrend_map.get(ticker)
            comp       = _composite(eps_z_norm, mtrend)
            upside, n_tg = _upside(con, ticker, calc_date)
            sbonus     = _share_bonus(con, ticker)
            bscore     = _buy_score(upside, comp, sbonus)
            scores[ticker] = {
                "level2_id":    level2_id,
                "eps_rev_z":    round(eps_z_raw, 3) if eps_z_raw is not None else None,
                "margin_trend": mtrend,
                "composite":    round(comp, 4) if comp is not None else None,
                "upside_pct":   upside,
                "n_targets":    n_tg,
                "share_bonus":  round(sbonus, 3) if sbonus is not None else None,
                "buy_score":    bscore,
            }
            if bscore is not None:
                by_level2.setdefault(level2_id, []).append((ticker, bscore))

        # 산업 내 순위 (buy_score 내림차순)
        rank_map: dict[str, int] = {}
        for level2_id, pairs in by_level2.items():
            sorted_pairs = sorted(pairs, key=lambda x: x[1], reverse=True)
            for rank, (ticker, _) in enumerate(sorted_pairs, start=1):
                rank_map[ticker] = rank

        # 저장
        saved = 0
        for ticker, s in scores.items():
            con.execute(
                """
                INSERT OR REPLACE INTO stock_scores
                    (ticker, calc_date, eps_rev_z, margin_trend, composite,
                     upside_pct, n_targets, share_bonus, buy_score, rank_in_level2)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ticker, calc_date, s["eps_rev_z"], s["margin_trend"], s["composite"],
                 s["upside_pct"], s["n_targets"], s["share_bonus"], s["buy_score"],
                 rank_map.get(ticker)),
            )
            saved += 1

        con.commit()
        log.info("stock_scores 저장 완료 — %d건", saved)

    finally:
        if own_con:
            con.close()


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
