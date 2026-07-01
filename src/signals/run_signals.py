"""신호 엔진 실행 진입점.

실행:
  python src/signals/run_signals.py            # 오늘 기준
  python src/signals/run_signals.py 20260620   # 특정일 기준
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from db import connect  # noqa: E402

from signals.confirmation import calc_confirmation
from signals.leading import calc_leading
from signals.cycle_classifier import classify_all, save_signals
from signals import timing
from signals import l0_industry
from signals import events

log = logging.getLogger(__name__)


def run(as_of: date | None = None) -> None:
    ref = as_of or date.today()
    log.info("신호 계산 기준일: %s", ref)

    con = connect()
    try:
        # 커버리지 밀도별 윈도우 분리
        industries = con.execute(
            "SELECT level2_id, signal_window_weeks, is_signal_eligible FROM industries"
        ).fetchall()

        all_conf: dict[str, dict] = {}
        all_lead: dict[str, dict] = {}

        # 4주 윈도우 (coverage_density=3)
        for ww in (4, 8):
            eligible = [r[0] for r in industries if r[1] == ww and r[2] == 1]
            if not eligible:
                continue
            conf = calc_confirmation(con, ref, ww)
            lead = calc_leading(con, ref, ww)
            # 해당 윈도우 산업만 추출
            all_conf.update({k: v for k, v in conf.items() if k in eligible})
            all_lead.update({k: v for k, v in lead.items() if k in eligible})

        records = classify_all(con, ref, all_conf, all_lead)
        n = save_signals(con, records)
        log.info("cycle_signals INSERT/REPLACE: %d건", n)

        # 결과 미리보기 (상위 5)
        top5 = records[:5]
        for r in top5:
            log.info(
                "  [%s] %s  score=%.3f  n=%d",
                r["cycle_phase"], r["level2_id"], r["composite_score"], r["n_reports"],
            )

        # L0 업황 동인 (수출·재고순환) — 가장 먼저 도는 신호
        l0_industry.run(con=con, calc_date=ref.isoformat())

        # 매수 타이밍 (L0 업황 + 방향 cycle_signals + 가격 확인) — 같은 커넥션 재사용
        timing.run(con=con, calc_date=ref.isoformat())

        # 신호 변화 이벤트 감지 (알림 피드) — timing/cycle 저장 후
        events.run(con=con, calc_date=ref.isoformat())
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    as_of = datetime.strptime(arg, "%Y%m%d").date() if arg else None
    run(as_of)
