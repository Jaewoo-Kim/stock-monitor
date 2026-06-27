"""L0 업황 동인 신호.

산업의 "업황 그 자체"가 도는 걸 가장 먼저 포착하는 0단계.
입력: industry_indicators (월간 시계열 — 수출액·재고순환·산업가격)
출력: l0_signals (driver_state, driver_score)

판정 (지표별 → 산업 종합):
  turning_up : 수출 YoY 음→양 전환 (또는 종합 점수>0 & 모멘텀>0) — 업황 바닥 통과
  rising     : 종합 점수 뚜렷한 양 — 업황 상승 진행
  bottoming  : 아직 음이나 개선 중 — 바닥 다지는 중
  falling    : 음 & 악화 — 하강
  관측부족    : 데이터 부족(YoY 계산에 13개월 미만)
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

MIN_MONTHS_YOY = 13   # YoY 계산 최소 개월 수 (t, t-12)
TYPE_WEIGHT = {"export": 0.6, "inv_cycle": 0.25, "price": 0.15}


def _series(con, level2_id: str, indicator_id: str) -> list[tuple[str, float]]:
    rows = con.execute(
        """SELECT period, value FROM industry_indicators
           WHERE level2_id=? AND indicator_id=? AND value IS NOT NULL
           ORDER BY period ASC""",
        (level2_id, indicator_id),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _eval_export(vals: list[float]) -> dict | None:
    """수출액 시계열 → YoY 기반 상태/점수/모멘텀."""
    if len(vals) < MIN_MONTHS_YOY:
        return None
    def yoy(i: int) -> float | None:
        if i - 12 < 0:
            return None
        base = vals[i - 12]
        if base == 0:
            return None
        return (vals[i] - base) / abs(base) * 100
    cur = yoy(len(vals) - 1)
    prev = yoy(len(vals) - 4) if len(vals) >= 16 else None  # 3개월 전 YoY
    if cur is None:
        return None
    momentum = (cur - prev) if prev is not None else 0.0
    turning = (prev is not None and prev <= 0 and cur > 0)
    score = _clip(cur / 30.0)        # ±30% → ±1
    return {"score": score, "momentum": momentum, "turning": turning,
            "metric": round(cur, 1)}


def _eval_inv_cycle(vals: list[float]) -> dict | None:
    """재고순환(출하증가율−재고증가율) 시계열 → 레벨/변화 기반."""
    if len(vals) < 4:
        return None
    cur = vals[-1]
    prev = vals[-4] if len(vals) >= 4 else vals[0]
    momentum = cur - prev
    turning = (prev <= 0 and cur > 0)
    score = _clip(cur / 10.0)
    return {"score": score, "momentum": momentum, "turning": turning,
            "metric": round(cur, 1)}


def _eval_price(vals: list[float]) -> dict | None:
    """산업 가격 시계열 → 3개월 모멘텀 기반."""
    if len(vals) < 4:
        return None
    cur, base = vals[-1], vals[-4]
    if base == 0:
        return None
    chg = (cur - base) / abs(base) * 100
    prev_base = vals[-7] if len(vals) >= 7 else None
    prev_chg = ((vals[-4] - prev_base) / abs(prev_base) * 100) if prev_base else None
    momentum = (chg - prev_chg) if prev_chg is not None else chg
    turning = (prev_chg is not None and prev_chg <= 0 and chg > 0)
    return {"score": _clip(chg / 20.0), "momentum": momentum, "turning": turning,
            "metric": round(chg, 1)}


_EVAL = {"export": _eval_export, "inv_cycle": _eval_inv_cycle, "price": _eval_price}


def _classify(agg_score: float, agg_mom: float, any_turn: bool) -> str:
    if any_turn or (agg_score > 0 and agg_mom > 0):
        return "turning_up"
    if agg_score > 0.15:
        return "rising"
    if agg_score < 0 and agg_mom > 0:
        return "bottoming"
    if agg_score <= -0.15:
        return "falling"
    return "bottoming"


def _load_taxonomy() -> list[dict]:
    path = ROOT / "data" / "seed" / "industry_indicators.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("indicators", [])


def run(con=None, calc_date: str | None = None) -> None:
    own = con is None
    if own:
        con = connect()
    if calc_date is None:
        calc_date = date.today().isoformat()

    try:
        taxonomy = _load_taxonomy()
        # level2_id → [indicator def]
        by_ind: dict[str, list[dict]] = {}
        for t in taxonomy:
            by_ind.setdefault(t["level2_id"], []).append(t)

        saved = 0
        for level2_id, defs in by_ind.items():
            parts, detail = [], {}
            for d in defs:
                vals = [v for _, v in _series(con, level2_id, d["id"])]
                ev = _EVAL.get(d["type"], lambda _v: None)(vals)
                if ev is None:
                    continue
                w = TYPE_WEIGHT.get(d["type"], 0.3)
                parts.append((ev, w))
                detail[d["id"]] = {"name": d["name"], "type": d["type"],
                                   "metric": ev["metric"], "turning": ev["turning"]}

            if not parts:
                state, score = "관측부족", None
            else:
                wsum = sum(w for _, w in parts)
                agg_score = sum(e["score"] * w for e, w in parts) / wsum
                agg_mom   = sum(e["momentum"] * w for e, w in parts) / wsum
                any_turn  = any(e["turning"] for e, _ in parts)
                state = _classify(agg_score, agg_mom, any_turn)
                score = round(agg_score, 4)

            con.execute(
                """INSERT OR REPLACE INTO l0_signals
                   (level2_id, calc_date, driver_state, driver_score, detail)
                   VALUES (?, ?, ?, ?, ?)""",
                (level2_id, calc_date, state, score,
                 json.dumps(detail, ensure_ascii=False) if detail else None),
            )
            saved += 1

        con.commit()
        log.info("l0_signals 저장 완료 — %d개 산업 (calc_date=%s)", saved, calc_date)
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
