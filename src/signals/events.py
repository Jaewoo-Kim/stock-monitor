"""신호 변화 이벤트 감지 (알림 피드).

직전 계산일 대비 상태 변화를 감지해 signal_events에 기록.
  timing_buy   : 매수적기로 신규 전환
  timing_watch : 관찰로 신규 전환 (매수적기 아님)
  phase_turn   : 사이클 국면이 전환/확장으로 진입

멱등: (event_date, level2_id, event_type) PK로 중복 방지.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from db import connect

log = logging.getLogger(__name__)


def _two_dates(con, table: str) -> tuple[str, str] | None:
    rows = [r[0] for r in con.execute(
        f"SELECT DISTINCT calc_date FROM {table} ORDER BY calc_date DESC LIMIT 2"
    ).fetchall()]
    if len(rows) < 2 or rows[0] == rows[1]:
        return None
    return rows[0], rows[1]


def detect_events(con, calc_date: str | None = None) -> int:
    if calc_date is None:
        calc_date = date.today().isoformat()

    inserted = 0

    # ── 타이밍 전환 (buy / watch) ──
    td = _two_dates(con, "timing_signals")
    if td:
        curr_d, prev_d = td
        curr = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute(
            "SELECT level2_id, timing_state, timing_score, idx_rs_3m, breadth_pct "
            "FROM timing_signals WHERE calc_date=?", (curr_d,))}
        prev = {r[0]: r[1] for r in con.execute(
            "SELECT level2_id, timing_state FROM timing_signals WHERE calc_date=?", (prev_d,))}
        for l2, (cs, score, rs, breadth) in curr.items():
            ps = prev.get(l2)
            if ps is None:
                continue
            ev = None
            if cs == "buy" and ps != "buy":
                ev = "timing_buy"
            elif cs == "watch" and ps not in ("watch", "buy"):
                ev = "timing_watch"
            if ev:
                detail = json.dumps({"timing_score": score, "rs_3m": rs, "breadth_pct": breadth},
                                    ensure_ascii=False)
                cur = con.execute(
                    """INSERT OR IGNORE INTO signal_events
                       (event_date, level2_id, event_type, from_state, to_state, detail)
                       VALUES (?,?,?,?,?,?)""",
                    (curr_d, l2, ev, ps, cs, detail))
                inserted += cur.rowcount

    # ── 국면 전환 (전환/확장 진입) ──
    cd = _two_dates(con, "cycle_signals")
    if cd:
        curr_d, prev_d = cd
        curr = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT level2_id, cycle_phase, composite_score FROM cycle_signals WHERE calc_date=?", (curr_d,))}
        prev = {r[0]: r[1] for r in con.execute(
            "SELECT level2_id, cycle_phase FROM cycle_signals WHERE calc_date=?", (prev_d,))}
        for l2, (cp, comp) in curr.items():
            pp = prev.get(l2)
            if pp is None:
                continue
            if cp in ("전환", "확장") and pp not in ("전환", "확장"):
                detail = json.dumps({"composite_score": comp}, ensure_ascii=False)
                cur = con.execute(
                    """INSERT OR IGNORE INTO signal_events
                       (event_date, level2_id, event_type, from_state, to_state, detail)
                       VALUES (?,?,?,?,?,?)""",
                    (curr_d, l2, "phase_turn", pp, cp, detail))
                inserted += cur.rowcount

    con.commit()
    log.info("signal_events 감지: 신규 %d건", inserted)
    return inserted


def run(con=None, calc_date: str | None = None) -> None:
    own = con is None
    if own:
        con = connect()
    try:
        detect_events(con, calc_date)
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
