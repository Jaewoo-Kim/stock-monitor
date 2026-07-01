"""선행 레이어 신호 — 컨센서스 EPS 리비전 (L2).

eps_consensus(네이버 기업실적분석 forward EPS) 스냅샷의 시간 변화 = EPS 추정치 리비전.
같은 fwd_year에 대해 최근 스냅샷 vs N주 전 스냅샷의 EPS 변화율로 산출.

  lead_breadth    : (EPS 상향 − 하향) / 전체  (-1 ~ +1)
  lead_magnitude  : 상향 종목의 EPS 변화율 평균 (%)
  lead_accel      : 이번 윈도우 breadth − 직전 윈도우 breadth (가속도)
  lead_first_turn : 직전 음(≤0) → 이번 양(>0) 최초 전환 (0/1)

스냅샷이 윈도우 기간만큼 누적되기 전에는 빈 dict 반환 (graceful skip).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean

log = logging.getLogger(__name__)


def _load_series(con) -> tuple[dict, dict]:
    """(ticker, fwd_year) → [(snapshot, eps)] (오름차순) + → level2_id."""
    rows = con.execute(
        """
        SELECT c.level2_id, ec.ticker, ec.fwd_year, ec.snapshot, ec.eps
        FROM eps_consensus ec
        JOIN companies c ON ec.ticker = c.ticker
        WHERE c.level2_id IS NOT NULL AND ec.eps IS NOT NULL
        ORDER BY ec.ticker, ec.fwd_year, ec.snapshot
        """
    ).fetchall()
    series: dict[tuple, list] = defaultdict(list)
    meta: dict[tuple, str] = {}
    for level2_id, ticker, fwd_year, snap, eps in rows:
        series[(ticker, fwd_year)].append((snap, eps))
        meta[(ticker, fwd_year)] = level2_id
    return series, meta


def _snap_at(lst: list[tuple[str, float]], cutoff: str):
    """cutoff 이전(<=) 가장 최근 스냅샷."""
    found = None
    for snap, eps in lst:
        if snap <= cutoff:
            found = (snap, eps)
    return found


def _breadth_between(series, meta, new_cut: str, old_cut: str) -> dict[str, dict]:
    """new_cut 시점 EPS vs old_cut 시점 EPS → level2별 {breadth, magnitude, n}."""
    revs: dict[str, list] = defaultdict(list)
    for key, lst in series.items():
        new = _snap_at(lst, new_cut)
        old = _snap_at(lst, old_cut)
        if new and old and old[1] and old[1] != 0 and new[0] > old[0]:
            revs[meta[key]].append((new[1] - old[1]) / abs(old[1]) * 100)
    out: dict[str, dict] = {}
    for level2_id, rv in revs.items():
        n = len(rv)
        up = sum(1 for r in rv if r > 0)
        dn = sum(1 for r in rv if r < 0)
        ups = [r for r in rv if r > 0]
        out[level2_id] = {
            "breadth":   (up - dn) / n,
            "magnitude": mean(ups) if ups else 0.0,
            "n":         n,
        }
    return out


def calc_leading(con, calc_date: date, window_weeks: int) -> dict[str, dict]:
    """{level2_id: {lead_breadth, lead_magnitude, lead_accel, lead_first_turn}}."""
    series, meta = _load_series(con)
    if not series:
        return {}

    until    = calc_date.isoformat()
    w1       = (calc_date - timedelta(weeks=window_weeks)).isoformat()
    w2       = (calc_date - timedelta(weeks=window_weeks * 2)).isoformat()

    curr = _breadth_between(series, meta, until, w1)
    prev = _breadth_between(series, meta, w1, w2)

    if not curr:
        log.debug("컨센서스 EPS 리비전 데이터 부족 — 선행 레이어 skip")
        return {}

    result: dict[str, dict] = {}
    for level2_id, c in curr.items():
        p = prev.get(level2_id)
        accel = (c["breadth"] - p["breadth"]) if p else None
        first_turn = 1 if (p and p["breadth"] <= 0 and c["breadth"] > 0) else 0
        result[level2_id] = {
            "lead_breadth":    c["breadth"],
            "lead_magnitude":  c["magnitude"],
            "lead_accel":      accel,
            "lead_first_turn": first_turn,
        }

    log.info("선행 레이어(EPS 리비전) 계산 완료: %d개 산업", len(result))
    return result
