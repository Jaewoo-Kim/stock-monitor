"""선행 레이어 신호 계산.

EPS 추정치 리비전 기반 3지표:
  - lead_breadth    : (EPS 상향 - 하향) / 전체  (-1 ~ +1)
  - lead_magnitude  : 상향 리포트의 EPS 변화율 평균 (%)
  - lead_accel      : 이번 윈도우 breadth - 직전 윈도우 breadth (가속도)
  - lead_first_turn : breadth 가 음→양으로 전환된 최근 4주 이내 플래그 (0/1)

EPS 데이터 부족 시 모두 None 반환 (graceful skip).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)


def calc_leading(
    con,
    calc_date: date,
    window_weeks: int,
) -> dict[str, dict]:
    """
    Returns:
        {level2_id: {lead_breadth, lead_magnitude, lead_accel, lead_first_turn}}
    """
    since = (calc_date - timedelta(weeks=window_weeks)).isoformat()
    until = calc_date.isoformat()
    # 직전 윈도우 (가속도 계산용)
    prev_since = (calc_date - timedelta(weeks=window_weeks * 2)).isoformat()
    prev_until = (calc_date - timedelta(weeks=window_weeks)).isoformat()

    def _fetch_eps_revisions(from_: str, to_: str) -> dict[str, list[float]]:
        """level2_id → [eps 변화율 %, ...] (eps_estimate + prev_eps_est 모두 있는 리포트)"""
        rows = con.execute(
            """
            SELECT c.level2_id,
                   re.eps_estimate,
                   re.prev_eps_est
            FROM report_events re
            JOIN companies c ON re.ticker = c.ticker
            WHERE re.published_date BETWEEN ? AND ?
              AND re.eps_estimate IS NOT NULL
              AND re.prev_eps_est IS NOT NULL
              AND re.prev_eps_est <> 0
              AND c.level2_id IS NOT NULL
            """,
            (from_, to_),
        ).fetchall()
        result: dict[str, list[float]] = {}
        for level2_id, eps, prev in rows:
            pct = (eps - prev) / abs(prev) * 100
            result.setdefault(level2_id, []).append(pct)
        return result

    curr = _fetch_eps_revisions(since, until)
    prev = _fetch_eps_revisions(prev_since, prev_until)

    if not curr:
        log.debug("EPS 리비전 데이터 없음 — 선행 레이어 skip")
        return {}

    result: dict[str, dict] = {}
    all_ids = set(curr) | set(prev)

    for level2_id in all_ids:
        curr_changes = curr.get(level2_id, [])
        prev_changes = prev.get(level2_id, [])

        # 현재 윈도우 breadth
        if curr_changes:
            n = len(curr_changes)
            up   = sum(1 for c in curr_changes if c > 0)
            down = sum(1 for c in curr_changes if c < 0)
            lb   = (up - down) / n
            mag_list  = [c for c in curr_changes if c > 0]
            lm   = sum(mag_list) / len(mag_list) if mag_list else 0.0
        else:
            lb = None
            lm = None

        # 직전 윈도우 breadth (가속도 계산)
        if prev_changes:
            pn  = len(prev_changes)
            pup = sum(1 for c in prev_changes if c > 0)
            pdn = sum(1 for c in prev_changes if c < 0)
            pb  = (pup - pdn) / pn
        else:
            pb = None

        # 가속도: 이번 breadth - 직전 breadth
        accel = (lb - pb) if (lb is not None and pb is not None) else None

        # 최초 전환 플래그: 직전이 음(≤0), 이번이 양(>0)
        first_turn = (
            1 if (lb is not None and pb is not None and pb <= 0 and lb > 0)
            else 0
        )

        result[level2_id] = {
            "lead_breadth":   lb,
            "lead_magnitude": lm,
            "lead_accel":     accel,
            "lead_first_turn": first_turn,
        }

    log.info("선행 레이어 계산 완료: %d개 산업", len(result))
    return result
