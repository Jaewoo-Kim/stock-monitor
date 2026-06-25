"""확인 레이어 신호 계산.

목표가 리비전 기반 3지표:
  - Breadth   : (상향 - 하향) / 전체 리포트 수  (-1 ~ +1)
  - Magnitude : 상향 리포트의 목표가 변화율 평균 (%)
  - Upside Gap: 업종지수가 최근 N주 최고가를 돌파했는가 (0/1)

입력: report_events + industry_index_history (SQLite)
출력: dict per level2_id
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)


def calc_confirmation(
    con,
    calc_date: date,
    window_weeks: int,
) -> dict[str, dict]:
    """
    Returns:
        {level2_id: {breadth, magnitude, upside_gap, n_reports}}
    """
    since = (calc_date - timedelta(weeks=window_weeks)).isoformat()
    until = calc_date.isoformat()

    # ── 목표가 리비전 집계 ──
    # 같은 종목에 여러 리포트가 있을 경우 최신 리포트와 직전 리포트의 차를 사용
    # 단순화: target_price vs prev_target 가 모두 있는 리포트만 사용
    rows = con.execute(
        """
        SELECT c.level2_id,
               re.target_price,
               re.prev_target
        FROM report_events re
        JOIN companies c ON re.ticker = c.ticker
        WHERE re.published_date BETWEEN ? AND ?
          AND re.target_price IS NOT NULL
          AND re.prev_target  IS NOT NULL
          AND c.level2_id     IS NOT NULL
          AND re.target_price > 0
          AND re.prev_target  > 0
        """,
        (since, until),
    ).fetchall()

    # level2_id → [(target, prev), ...]
    by_industry: dict[str, list[tuple[float, float]]] = {}
    for level2_id, tp, prev in rows:
        by_industry.setdefault(level2_id, []).append((tp, prev))

    # ── 업종지수 Upside Gap ──
    gap_rows = con.execute(
        """
        SELECT a.level2_id,
               a.close AS today_close,
               MAX(b.close) AS peak_close
        FROM industry_index_history a
        JOIN industry_index_history b
          ON a.level2_id = b.level2_id
         AND b.date BETWEEN ? AND ?
        WHERE a.date = (
            SELECT MAX(date) FROM industry_index_history
            WHERE level2_id = a.level2_id AND date <= ?
        )
        GROUP BY a.level2_id
        """,
        (since, until, until),
    ).fetchall()

    upside_map: dict[str, int] = {
        row[0]: (1 if row[1] >= row[2] else 0)
        for row in gap_rows
    }

    # ── 종합 ──
    result: dict[str, dict] = {}

    all_level2 = set(by_industry.keys()) | set(upside_map.keys())
    for level2_id in all_level2:
        pairs = by_industry.get(level2_id, [])
        n = len(pairs)

        if n == 0:
            breadth = None
            magnitude = None
        else:
            changes = [(tp - prev) / prev * 100 for tp, prev in pairs]
            up   = sum(1 for c in changes if c > 0)
            down = sum(1 for c in changes if c < 0)
            breadth   = (up - down) / n  # -1 ~ +1
            mag_list  = [c for c in changes if c > 0]
            magnitude = sum(mag_list) / len(mag_list) if mag_list else 0.0

        result[level2_id] = {
            "breadth":    breadth,
            "magnitude":  magnitude,
            "upside_gap": upside_map.get(level2_id),
            "n_reports":  n,
        }

    log.info(
        "확인 레이어 계산 완료: %d개 산업, 기간 %s~%s",
        len(result), since, until,
    )
    return result
