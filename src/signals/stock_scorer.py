"""종목 펀더멘털 모멘텀 스코어러.

두 축:
  eps_rev_z    — report_events의 EPS 리비전 z-score (선행, 4주 윈도우)
  margin_trend — company_fundamentals의 영업이익률 2분기 추세 (+1/0/-1)
  composite    — 0.5 * norm(eps_rev_z) + 0.5 * margin_trend  (-1~1)

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
    parts: list[float] = []
    if eps_z_norm is not None:
        parts.append(eps_z_norm * 0.5)
    if mtrend is not None:
        parts.append(float(mtrend) * 0.5)
    if not parts:
        return None
    # 데이터가 한 축만 있으면 전체 가중치로 대체
    scale = 1.0 / len(parts) * 1.0 if len(parts) < 2 else 1.0
    return sum(parts) * scale if len(parts) == 2 else parts[0] * (1.0 / 0.5)


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

        # 산업별 composite 순위 계산
        by_level2: dict[str, list[tuple[str, float]]] = {}
        scores: dict[str, dict] = {}

        for ticker, level2_id in companies:
            eps_z_raw  = eps_z_map.get(ticker)
            eps_z_norm = _norm_z(eps_z_raw)
            mtrend     = mtrend_map.get(ticker)
            comp       = _composite(eps_z_norm, mtrend)
            scores[ticker] = {
                "level2_id":   level2_id,
                "eps_rev_z":   round(eps_z_raw,  3) if eps_z_raw  is not None else None,
                "margin_trend": mtrend,
                "composite":   round(comp, 4) if comp is not None else None,
            }
            if comp is not None:
                by_level2.setdefault(level2_id, []).append((ticker, comp))

        # 산업 내 순위 (composite 내림차순)
        rank_map: dict[str, int] = {}
        for level2_id, pairs in by_level2.items():
            sorted_pairs = sorted(pairs, key=lambda x: x[1], reverse=True)
            for rank, (ticker, _) in enumerate(sorted_pairs, start=1):
                rank_map[ticker] = rank

        # 저장
        saved = 0
        for ticker, s in scores.items():
            rank = rank_map.get(ticker)
            con.execute(
                """
                INSERT OR REPLACE INTO stock_scores
                    (ticker, calc_date, eps_rev_z, margin_trend, composite, rank_in_level2)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ticker, calc_date, s["eps_rev_z"], s["margin_trend"], s["composite"], rank),
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
