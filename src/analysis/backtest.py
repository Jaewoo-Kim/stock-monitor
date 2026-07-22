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
from signals.timing import (
    OVERSOLD_DEV20_MAX,
    OVERSOLD_RET5D_MAX,
    OVERSOLD_RSI_MAX,
)

log = logging.getLogger(__name__)

LOOKBACK = 60   # RS·추세 계산 (3개월)
FWD_DAYS = 20   # forward 4주
STEP = 3        # as-of 간격(거래일)
OUTPUT = ROOT / "data" / "output"

OVERSOLD_LOOKBACK = 20            # MA20 계산 최소 거래일
OVERSOLD_FWD_SET = (5, 10, 20)    # 낙폭과대 이후 forward 거래일 다지점 검증


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


def _rsi14(closes: list[float]) -> float | None:
    period = 14
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def _run_oversold(ind_series: dict) -> dict:
    """단기 낙폭과대(oversold_flag) 신호 검증 — as-of 시점 룩어헤드 없이 재구성해
    forward 수익률(5/10/20 거래일)을 baseline(전체 관측치)과 비교.

    일별 관측치는 연속된 날짜끼리 자기상관이 크므로(같은 급락 구간이 여러 날
    반복 플래그됨), 연속 구간을 하나의 '이벤트'로 묶어 최초일 기준으로도 별도 집계
    — 이벤트 단위가 실질적인 유효 표본 수에 더 가깝다.
    """
    max_fwd = max(OVERSOLD_FWD_SET)
    all_obs: list[dict] = []
    flagged_by_ind: dict[str, list[int]] = {}

    for level2_id, series in ind_series.items():
        closes = [c for _, c in series]
        n = len(closes)
        idxs = []
        for i in range(OVERSOLD_LOOKBACK, n - max_fwd):
            window = closes[: i + 1]
            ma20 = sum(window[-20:]) / 20
            if ma20 == 0:
                continue
            dev20 = (closes[i] - ma20) / ma20 * 100
            if closes[i - 5] <= 0:
                continue
            ret5 = (closes[i] - closes[i - 5]) / closes[i - 5] * 100
            rsi14 = _rsi14(window[-15:]) if len(window) >= 15 else None
            if rsi14 is None:
                continue
            fwd = {f: (closes[i + f] - closes[i]) / closes[i] * 100 for f in OVERSOLD_FWD_SET}
            flagged = (ret5 <= OVERSOLD_RET5D_MAX and rsi14 <= OVERSOLD_RSI_MAX
                       and dev20 <= OVERSOLD_DEV20_MAX)
            all_obs.append({"flagged": flagged, **fwd})
            if flagged:
                idxs.append(i)
        if idxs:
            flagged_by_ind[level2_id] = idxs

    def _avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 2) if vals else None

    def _winrate(vals: list[float]) -> float | None:
        return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None

    def _summarize(obs: list[dict]) -> dict:
        return {
            "n_obs": len(obs),
            **{f"fwd{f}_avg_pct": _avg([o[f] for o in obs]) for f in OVERSOLD_FWD_SET},
            **{f"fwd{f}_winrate": _winrate([o[f] for o in obs]) for f in OVERSOLD_FWD_SET},
        }

    baseline = _summarize(all_obs)
    flagged_daily = _summarize([o for o in all_obs if o["flagged"]])

    # 연속 구간(같은 급락 에피소드)을 1건으로 묶어 최초일만 채택 — 일별 관측치는
    # 자기상관이 커서(같은 급락이 여러 날 반복 플래그) 유효 표본을 과대추정하므로 보정
    event_obs: list[dict] = []
    for level2_id, idxs in flagged_by_ind.items():
        closes = [c for _, c in ind_series[level2_id]]
        prev = None
        for i in sorted(idxs):
            if prev is None or i - prev > 1:
                event_obs.append({f: (closes[i + f] - closes[i]) / closes[i] * 100 for f in OVERSOLD_FWD_SET})
            prev = i
    events = {"n_events": len(event_obs), **{k: v for k, v in _summarize(event_obs).items() if k != "n_obs"}}

    # 이벤트 vs baseline 차이의 통계적 유의성(Welch's t 근사) — 표본이 매우 작아
    # (n_events≈수십) 방향성 힌트 이상으로 해석하지 않도록 t값·유의성 여부를 함께 기록.
    # 필요표본은 관측된 효과크기를 그대로 유지한다는(낙관적) 가정 하의 근사치일 뿐이다.
    def _stats_test(ev_vals: list[float], bl_vals: list[float]) -> dict:
        n_e, n_b = len(ev_vals), len(bl_vals)
        if n_e < 2 or n_b < 2:
            return {"diff_pct": None, "t_stat": None, "significant_5pct": None, "n_needed_per_group": None}
        mean_e, mean_b = sum(ev_vals) / n_e, sum(bl_vals) / n_b
        var_e = sum((v - mean_e) ** 2 for v in ev_vals) / (n_e - 1)
        var_b = sum((v - mean_b) ** 2 for v in bl_vals) / (n_b - 1)
        se = (var_e / n_e + var_b / n_b) ** 0.5
        diff = mean_e - mean_b
        t = diff / se if se > 0 else None
        # 80% 검정력 필요 표본(그룹당, 근사): ((z_alpha+z_beta)^2 * 2 * sd_pooled^2) / diff^2
        sd_pooled = (((n_e - 1) * var_e + (n_b - 1) * var_b) / (n_e + n_b - 2)) ** 0.5 if (n_e + n_b) > 2 else None
        n_needed = round(((1.96 + 0.84) ** 2) * 2 * (sd_pooled ** 2) / (diff ** 2)) if (sd_pooled and diff) else None
        return {
            "diff_pct": round(diff, 2),
            "t_stat": round(t, 2) if t is not None else None,
            "significant_5pct": (abs(t) >= 1.96) if t is not None else None,
            "n_needed_per_group": n_needed,
        }

    significance = {
        f"fwd{f}": _stats_test([o[f] for o in event_obs], [o[f] for o in all_obs if not o["flagged"]])
        for f in OVERSOLD_FWD_SET
    }

    return {
        "thresholds": {
            "ret5d_max": OVERSOLD_RET5D_MAX, "rsi_max": OVERSOLD_RSI_MAX, "dev20_max": OVERSOLD_DEV20_MAX,
        },
        "significance": significance,
        "baseline": baseline,
        "flagged_daily": flagged_daily,
        "flagged_events": events,
        "note": "이벤트=연속 플래그 구간을 1건으로 묶은 것(자기상관 보정, 유효표본에 가까움 — 그래도 "
                "n≈수십에 불과). fwd5일 초과수익은 방향은 양(+)이나 |t|<1.96로 통계적으로 유의하지 "
                "않고(가격이력 ~6개월·단일 국면 표본), 80% 검정력을 확보하려면 그룹당 수백 건 이상 "
                "필요 추정(significance.n_needed_per_group) — 현재는 확정된 엣지가 아니라 약한 "
                "가설이므로 timing_score에는 가산하지 않고 정보성 배지로만 노출한다.",
    }


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

    oversold = _run_oversold(ind_series)

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
        "oversold": oversold,
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "backtest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("backtest: n=%d IC=%s RS상위 fwd=%s%% RS하위=%s%%",
             result["n_obs"], result["ic_rs_fwd"],
             result["rs_high_fwd_pct"], result["rs_low_fwd_pct"])
    log.info("oversold backtest: events=%d fwd5=%s%% fwd20=%s%% (baseline fwd5=%s%% fwd20=%s%%)",
             oversold["flagged_events"]["n_events"],
             oversold["flagged_events"]["fwd5_avg_pct"], oversold["flagged_events"]["fwd20_avg_pct"],
             oversold["baseline"]["fwd5_avg_pct"], oversold["baseline"]["fwd20_avg_pct"])
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=2))
