"""L1 신호 백테스트 — 신호가 forward 산업 수익률을 예측했는가.

가격 데이터만으로 과거 as-of 시점의 L1 신호(상대강도 RS·추세)를 재구성하고,
이후 FWD_DAYS 거래일 산업지수 수익률과의 관계를 측정.

측정:
  IC(정보계수)  : RS와 forward 수익률의 순위상관 (Spearman)
  버킷 수익률    : RS 상위군 vs 하위군의 평균 forward 수익률
  추세 비교      : 정배열(MA20>MA60) vs 역배열 평균 forward 수익률

주의: 가용 가격 이력이 ~6개월이라 표본이 작다(디렉셔널 참고용).

실행: python src/analysis/backtest.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from db import connect

log = logging.getLogger(__name__)

LOOKBACK = 60   # RS·추세 계산 (3개월)
FWD_DAYS = 20   # forward 4주
STEP = 3        # as-of 간격(거래일)
OUTPUT = ROOT / "data" / "output"


def _sma(v: list[float], n: int, end: int) -> float | None:
    if end < n:
        return None
    return sum(v[end - n:end]) / n


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 5:
        return None
    def rank(a: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: a[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and a[order[j + 1]] == a[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    return num / (dx * dy) if dx and dy else None


def run() -> dict:
    con = connect()
    try:
        # 시장 프록시 (date→close), 날짜 순
        mkt = {d: c for d, c in con.execute(
            "SELECT date, close FROM market_index_history WHERE code='_UNIV' ORDER BY date")}
        # 산업지수 (level2→[(date,close)])
        rows = con.execute(
            "SELECT level2_id, date, close FROM industry_index_history ORDER BY level2_id, date").fetchall()
    finally:
        con.close()

    from collections import defaultdict
    ind_series: dict[str, list] = defaultdict(list)
    for level2_id, d, c in rows:
        ind_series[level2_id].append((d, c))

    obs_rs: list[float] = []
    obs_fwd: list[float] = []
    obs_trend: list[int] = []
    n_asof = 0

    for level2_id, series in ind_series.items():
        dates = [d for d, _ in series]
        closes = [c for _, c in series]
        n = len(closes)
        for i in range(LOOKBACK, n - FWD_DAYS, STEP):
            d0, d_lb = dates[i], dates[i - LOOKBACK]
            if d0 not in mkt or d_lb not in mkt or closes[i - LOOKBACK] <= 0 or mkt[d_lb] <= 0:
                continue
            ind_ret = closes[i] / closes[i - LOOKBACK] - 1
            mkt_ret = mkt[d0] / mkt[d_lb] - 1
            rs = (ind_ret - mkt_ret) * 100
            ma20 = _sma(closes, 20, i)
            ma60 = _sma(closes, 60, i)
            trend = 1 if (ma20 and ma60 and ma20 > ma60) else 0
            fwd = (closes[i + FWD_DAYS] / closes[i] - 1) * 100
            obs_rs.append(rs)
            obs_fwd.append(fwd)
            obs_trend.append(trend)
            n_asof += 1

    ic = _spearman(obs_rs, obs_fwd)

    # RS 3분위 버킷
    def bucket_mean(pred: list[float], fwd: list[float]) -> dict:
        idx = sorted(range(len(pred)), key=lambda k: pred[k])
        t = len(idx) // 3
        low = [fwd[k] for k in idx[:t]]
        high = [fwd[k] for k in idx[-t:]] if t else []
        return {
            "high_rs_fwd": round(sum(high) / len(high), 2) if high else None,
            "low_rs_fwd":  round(sum(low) / len(low), 2) if low else None,
        }

    buckets = bucket_mean(obs_rs, obs_fwd)
    up = [obs_fwd[k] for k in range(len(obs_fwd)) if obs_trend[k] == 1]
    dn = [obs_fwd[k] for k in range(len(obs_fwd)) if obs_trend[k] == 0]

    result = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "n_obs": len(obs_fwd),
        "lookback_days": LOOKBACK,
        "fwd_days": FWD_DAYS,
        "ic_rs_fwd": round(ic, 3) if ic is not None else None,
        "rs_high_fwd_pct": buckets["high_rs_fwd"],
        "rs_low_fwd_pct": buckets["low_rs_fwd"],
        "trend_up_fwd_pct": round(sum(up) / len(up), 2) if up else None,
        "trend_dn_fwd_pct": round(sum(dn) / len(dn), 2) if dn else None,
        "avg_fwd_pct": round(sum(obs_fwd) / len(obs_fwd), 2) if obs_fwd else None,
        "note": "가격이력 ~6개월 · 표본 소량 · 디렉셔널 참고용",
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "backtest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("backtest: n=%d IC=%s RS상위 fwd=%s%% RS하위=%s%%",
             result["n_obs"], result["ic_rs_fwd"],
             result["rs_high_fwd_pct"], result["rs_low_fwd_pct"])
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=2))
