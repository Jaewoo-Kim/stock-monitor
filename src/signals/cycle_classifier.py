"""사이클 분류기.

확인 + 선행 레이어 신호 → 5단계 사이클 상태 + composite_score.

사이클 단계 결정 규칙 (docs/04 1.3):
  1순위 발굴: lead_first_turn=1 + lead_accel > 0       → '전환'  (선행만 켜짐)
  2순위 확인: conf_breadth ≥ 0.6 + conf_upside_gap=1   → '확장'  (확인 강)
  3순위:      conf_breadth > 0   + conf_upside_gap=0   → '전환'  (확인 약)
  4순위:      conf_breadth < 0   + conf_upside_gap=1   → '둔화'  (꺾이는 중)
  5순위:      conf_breadth ≤ -0.4                       → '침체'
  6순위:      conf_breadth < 0   (mild)                 → '바닥'
  데이터 부족 (n_reports < 3): → '관측부족'
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

MIN_REPORTS = 3  # 신호 계산 최소 리포트 수


def _composite_score(conf: dict, lead: dict) -> float:
    """
    -1 ~ +1 종합 점수.
    확인 레이어 60% + 선행 레이어 40% (선행 없으면 확인만 100%).
    """
    cb = conf.get("breadth") or 0.0
    ug = conf.get("upside_gap") or 0

    # 확인 점수 (-1 ~ 1)
    conf_score = cb * 0.7 + ug * 0.3

    lb = lead.get("lead_breadth")
    la = lead.get("lead_accel")
    ft = lead.get("lead_first_turn") or 0

    if lb is not None:
        lead_score = lb * 0.5 + (la or 0) * 0.3 + ft * 0.2
        return conf_score * 0.6 + lead_score * 0.4
    else:
        return conf_score


def _classify(conf: dict, lead: dict, n: int) -> str:
    if n < MIN_REPORTS:
        return "관측부족"

    cb = conf.get("breadth")
    ug = conf.get("upside_gap") or 0
    ft = lead.get("lead_first_turn") or 0
    la = lead.get("lead_accel")

    if cb is None:
        return "관측부족"

    # 선행 레이어 최우선 발굴
    if ft == 1 and la is not None and la > 0:
        return "전환"

    # 확인 레이어
    if cb >= 0.6 and ug == 1:
        return "확장"
    if cb > 0:
        return "전환"
    if cb < 0 and ug == 1:
        return "둔화"
    if cb <= -0.4:
        return "침체"
    return "바닥"


def _confidence(n: int, has_lead: bool) -> float:
    """데이터 충분도 0~1."""
    base = min(n / 10, 1.0)  # 10건이면 1.0
    return base * (1.05 if has_lead else 1.0)


def classify_all(
    con,
    calc_date: date,
    confirmation: dict[str, dict],
    leading: dict[str, dict],
) -> list[dict]:
    """
    모든 산업의 사이클 신호 결과 리스트 반환 (cycle_signals 삽입용).

    Returns:
        [{ level2_id, calc_date, window_weeks, conf_*, lead_*, composite_score,
           cycle_phase, phase_confidence, n_reports }, ...]
    """
    # signal_window_weeks 참조
    windows: dict[str, int] = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT level2_id, signal_window_weeks FROM industries"
        ).fetchall()
    }

    all_ids = set(confirmation.keys()) | set(leading.keys())
    records = []

    for level2_id in all_ids:
        conf = confirmation.get(level2_id, {})
        lead = leading.get(level2_id, {})
        n    = conf.get("n_reports", 0)
        ww   = windows.get(level2_id, 4)

        phase = _classify(conf, lead, n)
        score = _composite_score(conf, lead)
        conf_val = _confidence(n, bool(lead))

        records.append({
            "level2_id":        level2_id,
            "calc_date":        calc_date.isoformat(),
            "window_weeks":     ww,
            "conf_breadth":     conf.get("breadth"),
            "conf_magnitude":   conf.get("magnitude"),
            "conf_upside_gap":  conf.get("upside_gap"),
            "lead_breadth":     lead.get("lead_breadth"),
            "lead_magnitude":   lead.get("lead_magnitude"),
            "lead_accel":       lead.get("lead_accel"),
            "lead_first_turn":  lead.get("lead_first_turn"),
            "composite_score":  round(score, 4),
            "cycle_phase":      phase,
            "phase_confidence": round(min(conf_val, 1.0), 3),
            "n_reports":        n,
        })

    records.sort(key=lambda r: r["composite_score"], reverse=True)
    log.info("사이클 분류 완료: %d개 산업", len(records))
    return records


def save_signals(con, records: list[dict]) -> int:
    """cycle_signals 테이블 INSERT OR REPLACE. 반환: upsert 건수."""
    cur = con.cursor()
    inserted = 0
    for r in records:
        cur.execute(
            """INSERT OR REPLACE INTO cycle_signals
               (level2_id, calc_date, window_weeks,
                conf_breadth, conf_magnitude, conf_upside_gap,
                lead_breadth, lead_magnitude, lead_accel, lead_first_turn,
                composite_score, cycle_phase, phase_confidence, n_reports)
               VALUES(:level2_id, :calc_date, :window_weeks,
                      :conf_breadth, :conf_magnitude, :conf_upside_gap,
                      :lead_breadth, :lead_magnitude, :lead_accel, :lead_first_turn,
                      :composite_score, :cycle_phase, :phase_confidence, :n_reports)""",
            r,
        )
        inserted += cur.rowcount
    con.commit()
    return inserted
