"""정적 JSON 빌더.

DB → data/output/*.json 생성. GitHub Pages / 로컬 서버에서 대시보드가 이 파일을 읽는다.

출력 파일:
  data/output/industries.json  — 산업별 사이클 현황 + 대표종목 + ETF
  data/output/reports.json     — 최신 리포트 100건 (산업별)
  data/output/meta.json        — 빌드 시각, 통계

실행:
  python src/build_static.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from db import connect  # noqa: E402

log = logging.getLogger(__name__)

OUTPUT = ROOT / "data" / "output"

PHASE_ORDER = {"전환": 0, "확장": 1, "둔화": 2, "바닥": 3, "침체": 4, "관측부족": 5}


# ─────────────────────────────────────
# 산업 현황 JSON
# ─────────────────────────────────────

def build_industries(con) -> list[dict]:
    today = date.today().isoformat()

    # 최신 신호 (level2_id 별 가장 최근 calc_date)
    signals = {
        row[0]: {
            "cycle_phase":      row[1],
            "composite_score":  row[2],
            "conf_breadth":     row[3],
            "conf_magnitude":   row[4],
            "conf_upside_gap":  row[5],
            "lead_first_turn":  row[6],
            "lead_accel":       row[7],
            "phase_confidence": row[8],
            "n_reports":        row[9],
            "calc_date":        row[10],
        }
        for row in con.execute(
            """
            SELECT cs.level2_id, cs.cycle_phase, cs.composite_score,
                   cs.conf_breadth, cs.conf_magnitude, cs.conf_upside_gap,
                   cs.lead_first_turn, cs.lead_accel,
                   cs.phase_confidence, cs.n_reports, cs.calc_date
            FROM cycle_signals cs
            INNER JOIN (
                SELECT level2_id, MAX(calc_date) AS max_date
                FROM cycle_signals GROUP BY level2_id
            ) latest ON cs.level2_id = latest.level2_id
                     AND cs.calc_date = latest.max_date
            """
        ).fetchall()
    }

    # 최신 타이밍 신호 (level2_id 별)
    timing = {
        row[0]: {
            "timing_state":   row[1],
            "timing_score":   row[2],
            "idx_trend_up":   row[3],
            "idx_ret_4w":     row[4],
            "idx_ret_12w":    row[5],
            "idx_rsi14":      row[6],
            "idx_high_break": row[7],
            "price_days":     row[8],
            "idx_rs_3m":      row[9],
            "idx_rs_up":      row[10],
            "breadth_pct":    row[11],
            "breadth_up":     row[12],
        }
        for row in con.execute(
            """
            SELECT ts.level2_id, ts.timing_state, ts.timing_score,
                   ts.idx_trend_up, ts.idx_ret_4w, ts.idx_ret_12w,
                   ts.idx_rsi14, ts.idx_high_break, ts.price_days,
                   ts.idx_rs_3m, ts.idx_rs_up, ts.breadth_pct, ts.breadth_up
            FROM timing_signals ts
            INNER JOIN (
                SELECT level2_id, MAX(calc_date) AS max_date
                FROM timing_signals GROUP BY level2_id
            ) latest ON ts.level2_id = latest.level2_id
                     AND ts.calc_date = latest.max_date
            """
        ).fetchall()
    }

    # L0 업황 동인 신호 (level2_id 별)
    l0 = {
        row[0]: {"driver_state": row[1], "driver_score": row[2],
                 "detail": json.loads(row[3]) if row[3] else None}
        for row in con.execute(
            "SELECT level2_id, driver_state, driver_score, detail FROM l0_signals"
        ).fetchall()
    }

    # 산업 기본 정보
    industries = con.execute(
        """
        SELECT i.level2_id, i.level1_id, i.level2_name,
               s.level1_name, i.coverage_density, i.is_signal_eligible
        FROM industries i
        JOIN sectors s ON i.level1_id = s.level1_id
        ORDER BY i.level2_id
        """
    ).fetchall()

    # 대표종목 (level2_id → list)
    rep_companies: dict[str, list] = {}
    for row in con.execute(
        """
        SELECT c.level2_id, c.ticker, c.name,
               ph.close AS last_close
        FROM companies c
        LEFT JOIN (
            SELECT ticker, close FROM price_history
            WHERE date = (SELECT MAX(date) FROM price_history)
        ) ph ON c.ticker = ph.ticker
        WHERE c.is_representative = 1 AND c.level2_id IS NOT NULL
        ORDER BY c.level2_id, c.ticker
        """
    ).fetchall():
        level2_id, ticker, name, last_close = row
        rep_companies.setdefault(level2_id, []).append({
            "ticker": ticker,
            "name": name,
            "last_close": last_close,
        })

    # 대표 ETF (level2_id → list)
    etfs: dict[str, list] = {}
    for row in con.execute(
        "SELECT level2_id, etf_ticker, etf_name FROM industry_etfs ORDER BY level2_id"
    ).fetchall():
        etfs.setdefault(row[0], []).append({"ticker": row[1], "name": row[2]})

    # 최근 리포트 건수 (4주)
    report_counts: dict[str, int] = {
        row[0]: row[1]
        for row in con.execute(
            """
            SELECT c.level2_id, COUNT(*) FROM report_events re
            JOIN companies c ON re.ticker = c.ticker
            WHERE re.published_date >= date(?, '-28 days')
              AND c.level2_id IS NOT NULL
            GROUP BY c.level2_id
            """,
            (today,),
        ).fetchall()
    }

    result = []
    for (level2_id, level1_id, level2_name, level1_name,
         coverage_density, is_signal_eligible) in industries:
        sig = signals.get(level2_id, {})
        tim = timing.get(level2_id, {})
        l0v = l0.get(level2_id, {})
        result.append({
            "level2_id":      level2_id,
            "level1_id":      level1_id,
            "level2_name":    level2_name,
            "level1_name":    level1_name,
            "coverage":       coverage_density,
            "signal_ok":      bool(is_signal_eligible),
            "cycle_phase":    sig.get("cycle_phase", "관측부족"),
            "composite_score": sig.get("composite_score"),
            "conf_breadth":   sig.get("conf_breadth"),
            "conf_magnitude": sig.get("conf_magnitude"),
            "upside_gap":     sig.get("conf_upside_gap"),
            "lead_first_turn": sig.get("lead_first_turn"),
            "lead_accel":     sig.get("lead_accel"),
            "phase_confidence": sig.get("phase_confidence"),
            "n_reports_signal": sig.get("n_reports", 0),
            "n_reports_4w":   report_counts.get(level2_id, 0),
            "calc_date":      sig.get("calc_date"),
            # 매수 타이밍 (방향 + 가격 확인)
            "timing_state":   tim.get("timing_state"),
            "timing_score":   tim.get("timing_score"),
            "idx_trend_up":   tim.get("idx_trend_up"),
            "idx_ret_4w":     tim.get("idx_ret_4w"),
            "idx_ret_12w":    tim.get("idx_ret_12w"),
            "idx_rsi14":      tim.get("idx_rsi14"),
            "idx_high_break": tim.get("idx_high_break"),
            "price_days":     tim.get("price_days"),
            # L1 산업 전체 전환: 상대강도·폭
            "idx_rs_3m":      tim.get("idx_rs_3m"),
            "idx_rs_up":      tim.get("idx_rs_up"),
            "breadth_pct":    tim.get("breadth_pct"),
            "breadth_up":     tim.get("breadth_up"),
            # L0 업황 동인 (수출·재고순환)
            "l0_state":       l0v.get("driver_state"),
            "l0_score":       l0v.get("driver_score"),
            "l0_detail":      l0v.get("detail"),
            "companies":      rep_companies.get(level2_id, []),
            "etfs":           etfs.get(level2_id, []),
        })

    # 사이클 단계 → composite_score 순 정렬
    result.sort(key=lambda r: (
        PHASE_ORDER.get(r["cycle_phase"], 5),
        -(r["composite_score"] or -999),
    ))

    return result


# ─────────────────────────────────────
# 최신 리포트 JSON
# ─────────────────────────────────────

def build_reports(con, limit: int = 100) -> list[dict]:
    rows = con.execute(
        f"""
        SELECT re.report_id, re.published_date, re.broker,
               a.name AS analyst_name,
               re.ticker, c.name AS company_name,
               c.level2_id,
               i.level2_name,
               re.title, re.opinion, re.target_price, re.prev_target,
               re.source_url, re.broker_url, re.pdf_available
        FROM report_events re
        LEFT JOIN analysts  a ON re.analyst_id = a.analyst_id
        LEFT JOIN companies c ON re.ticker     = c.ticker
        LEFT JOIN industries i ON c.level2_id  = i.level2_id
        ORDER BY re.published_date DESC, re.report_id DESC
        LIMIT {limit}
        """
    ).fetchall()

    cols = [
        "report_id", "published_date", "broker", "analyst_name",
        "ticker", "company_name", "level2_id", "level2_name",
        "title", "opinion", "target_price", "prev_target",
        "source_url", "broker_url", "pdf_available",
    ]
    return [dict(zip(cols, row)) for row in rows]


# ─────────────────────────────────────
# 메타 JSON
# ─────────────────────────────────────

def build_industry_detail(con) -> dict[str, dict]:
    """level2_id → 상세 데이터 (리포트 타임라인 + 종목 테이블).

    Returns:
        {level2_id: {recent_reports:[...], companies_detail:[...]}}
    """
    # 최근 4주 리포트 (level2_id 별, 최대 20건)
    rows = con.execute(
        """
        SELECT c.level2_id,
               re.published_date, re.broker, re.ticker,
               comp.name AS company_name,
               re.title, re.opinion,
               re.target_price, re.prev_target,
               re.source_url, re.broker_url,
               a.name AS analyst_name
        FROM report_events re
        JOIN companies comp ON re.ticker = comp.ticker
        JOIN (SELECT level2_id FROM industries) i ON comp.level2_id = i.level2_id
        JOIN companies c ON re.ticker = c.ticker
        LEFT JOIN analysts a ON re.analyst_id = a.analyst_id
        WHERE re.published_date >= date('now', '-28 days')
          AND c.level2_id IS NOT NULL
        ORDER BY c.level2_id, re.published_date DESC
        """
    ).fetchall()

    detail: dict[str, dict] = {}
    seen: dict[str, int] = {}
    for row in rows:
        (level2_id, pub_date, broker, ticker, cname,
         title, opinion, tp, prev_tp, src_url, brk_url, analyst) = row
        d = detail.setdefault(level2_id, {"recent_reports": [], "companies_detail": []})
        if seen.get(level2_id, 0) < 20:
            tp_pct = None
            if tp and prev_tp and prev_tp > 0:
                tp_pct = round((tp - prev_tp) / prev_tp * 100, 1)
            d["recent_reports"].append({
                "date": pub_date, "broker": broker,
                "ticker": ticker, "company_name": cname,
                "title": title, "opinion": opinion,
                "target_price": tp, "prev_target": prev_tp, "tp_pct": tp_pct,
                "source_url": src_url, "broker_url": brk_url,
                "analyst_name": analyst,
            })
            seen[level2_id] = seen.get(level2_id, 0) + 1

    # 대표종목 최근 종가 + 1개월 수익률
    price_rows = con.execute(
        """
        SELECT c.level2_id, c.ticker, c.name,
               ph_now.close AS close_now,
               ph_1m.close  AS close_1m
        FROM companies c
        LEFT JOIN (
            SELECT ticker, close FROM price_history
            WHERE date = (SELECT MAX(date) FROM price_history)
        ) ph_now ON c.ticker = ph_now.ticker
        LEFT JOIN (
            SELECT ticker, close FROM price_history
            WHERE date = (
                SELECT MAX(date) FROM price_history
                WHERE date <= date((SELECT MAX(date) FROM price_history), '-30 days')
            )
        ) ph_1m ON c.ticker = ph_1m.ticker
        WHERE c.is_representative = 1 AND c.level2_id IS NOT NULL
        ORDER BY c.level2_id, c.ticker
        """
    ).fetchall()

    for level2_id, ticker, name, close_now, close_1m in price_rows:
        d = detail.setdefault(level2_id, {"recent_reports": [], "companies_detail": []})
        ret_1m = None
        if close_now and close_1m and close_1m > 0:
            ret_1m = round((close_now - close_1m) / close_1m * 100, 1)
        d["companies_detail"].append({
            "ticker": ticker, "name": name,
            "close": close_now, "ret_1m": ret_1m,
        })

    return detail


def build_stock_scores(con) -> dict[str, list]:
    """level2_id → 종목 점수 목록 (산업 내 순위순)."""
    rows = con.execute(
        """
        SELECT ss.ticker, c.name, c.level2_id,
               ss.eps_rev_z, ss.margin_trend, ss.composite,
               ss.upside_pct, ss.n_targets, ss.share_bonus, ss.buy_score,
               ss.rank_in_level2, ss.calc_date
        FROM stock_scores ss
        JOIN companies c ON ss.ticker = c.ticker
        INNER JOIN (
            SELECT ticker, MAX(calc_date) AS max_date
            FROM stock_scores GROUP BY ticker
        ) latest ON ss.ticker = latest.ticker
                 AND ss.calc_date = latest.max_date
        WHERE c.level2_id IS NOT NULL
        ORDER BY c.level2_id, COALESCE(ss.rank_in_level2, 999)
        """
    ).fetchall()

    result: dict[str, list] = {}
    for (ticker, name, level2_id, eps_rev_z, margin_trend, composite,
         upside_pct, n_targets, share_bonus, buy_score, rank, calc_date) in rows:
        result.setdefault(level2_id, []).append({
            "ticker":       ticker,
            "name":         name,
            "eps_rev_z":    eps_rev_z,
            "margin_trend": margin_trend,
            "composite":    composite,
            "upside_pct":   upside_pct,
            "n_targets":    n_targets,
            "buy_score":    buy_score,
            "rank":         rank,
            "calc_date":    calc_date,
        })
    return result


def build_market_share(con) -> dict[str, list]:
    """ticker → 글로벌 점유율·순위 목록 (순위 우선, 점유율 내림차순)."""
    rows = con.execute(
        """
        SELECT ticker, segment, global_share, global_rank, as_of, source, note
        FROM company_market_share
        ORDER BY ticker,
                 COALESCE(global_rank, 999),
                 COALESCE(global_share, -1) DESC
        """
    ).fetchall()
    result: dict[str, list] = {}
    for ticker, segment, share, rank, as_of, source, note in rows:
        result.setdefault(ticker, []).append({
            "segment": segment,
            "share":   share,
            "rank":    rank,
            "as_of":   as_of,
            "source":  source,
            "note":    note,
        })
    return result


def _opinion_bucket(op: str | None) -> str | None:
    if not op:
        return None
    s = op.strip().upper()
    if "매도" in op or "SELL" in s:
        return "sell"
    if "중립" in op or "보유" in op or "HOLD" in s:
        return "hold"
    if "매수" in op or "BUY" in s:
        return "buy"
    return "hold"


def build_company_briefs(con) -> dict[str, dict]:
    """종목별 기업 브리프: 저장된 사업·리스크 + 실시간 애널리스트·펀더멘털·퀀트 집계."""
    today = date.today().isoformat()

    # 대표종목 기본 + 산업
    companies = con.execute(
        """
        SELECT c.ticker, c.name, c.level2_id, i.level2_name
        FROM companies c
        LEFT JOIN industries i ON c.level2_id = i.level2_id
        WHERE c.is_representative = 1 AND c.is_etf = 0 AND c.level2_id IS NOT NULL
        ORDER BY c.level2_id, c.ticker
        """
    ).fetchall()

    # 저장된 브리프
    briefs = {
        row[0]: {"business": row[1], "risks": row[2], "moat": row[3],
                 "source": row[4], "updated": row[5]}
        for row in con.execute(
            "SELECT ticker, business_summary, risk_summary, moat_summary, source, updated_at FROM company_briefs"
        ).fetchall()
    }

    # 최신가 + 1개월 수익률
    price = {}
    for row in con.execute(
        """
        SELECT c.ticker, n.close AS close_now, m.close AS close_1m
        FROM companies c
        LEFT JOIN (SELECT ticker, close FROM price_history
                   WHERE date=(SELECT MAX(date) FROM price_history)) n ON c.ticker=n.ticker
        LEFT JOIN (SELECT ticker, close FROM price_history
                   WHERE date=(SELECT MAX(date) FROM price_history
                               WHERE date<=date((SELECT MAX(date) FROM price_history),'-30 days'))) m
                  ON c.ticker=m.ticker
        WHERE c.is_representative=1
        """
    ).fetchall():
        tk, now, m1 = row
        ret = round((now - m1) / m1 * 100, 1) if (now and m1 and m1 > 0) else None
        price[tk] = {"close": now, "ret_1m": ret}

    # 애널리스트 집계 (최근 12주)
    since = (date.fromisoformat(today) - timedelta(weeks=12)).isoformat()
    analyst: dict[str, dict] = {}
    for row in con.execute(
        """
        SELECT re.ticker, re.published_date, re.broker, re.title,
               re.opinion, re.target_price, re.source_url, re.broker_url
        FROM report_events re
        JOIN companies c ON re.ticker = c.ticker
        WHERE c.is_representative=1 AND re.published_date >= ?
        ORDER BY re.ticker, re.published_date DESC, re.report_id DESC
        """,
        (since,),
    ).fetchall():
        tk, pdate, broker, title, op, tp, surl, burl = row
        a = analyst.setdefault(tk, {"n_buy": 0, "n_hold": 0, "n_sell": 0,
                                    "targets": [], "latest_target": None, "recent": []})
        b = _opinion_bucket(op)
        if b == "buy":
            a["n_buy"] += 1
        elif b == "sell":
            a["n_sell"] += 1
        elif b == "hold":
            a["n_hold"] += 1
        if tp:
            a["targets"].append(tp)
            if a["latest_target"] is None:
                a["latest_target"] = tp
        if len(a["recent"]) < 5:
            a["recent"].append({"date": pdate, "broker": broker, "title": title,
                                 "opinion": op, "source_url": surl, "broker_url": burl})

    # 펀더멘털 (DART, 최근 4분기)
    fundamentals: dict[str, list] = {}
    for row in con.execute(
        """
        SELECT ticker, period_end, revenue, op_income, op_margin, eps_actual
        FROM company_fundamentals WHERE source='DART'
        ORDER BY ticker, period_end DESC
        """
    ).fetchall():
        tk = row[0]
        lst = fundamentals.setdefault(tk, [])
        if len(lst) < 4:
            lst.append({"period_end": row[1], "revenue": row[2], "op_income": row[3],
                        "op_margin": row[4], "eps_actual": row[5]})

    # 글로벌 점유율
    mshare: dict[str, list] = {}
    for row in con.execute(
        """SELECT ticker, segment, global_share, global_rank, as_of, source, note
           FROM company_market_share ORDER BY ticker, COALESCE(global_rank,999)"""
    ).fetchall():
        mshare.setdefault(row[0], []).append({
            "segment": row[1], "share": row[2], "rank": row[3],
            "as_of": row[4], "source": row[5], "note": row[6]})

    # DART 사업보고서 발췌
    disclosures = {
        row[0]: {"rcept_no": row[1], "report_nm": row[2], "rcept_dt": row[3],
                 "biz_overview": row[4], "risk_text": row[5]}
        for row in con.execute(
            "SELECT ticker, rcept_no, report_nm, rcept_dt, biz_overview, risk_text FROM company_disclosures"
        ).fetchall()
    }

    # 퀀트 (stock_scores 최신)
    quant = {
        row[0]: {"buy_score": row[1], "rank_in_level2": row[2], "upside_pct": row[3],
                 "composite": row[4]}
        for row in con.execute(
            """
            SELECT ss.ticker, ss.buy_score, ss.rank_in_level2, ss.upside_pct, ss.composite
            FROM stock_scores ss
            INNER JOIN (SELECT ticker, MAX(calc_date) md FROM stock_scores GROUP BY ticker) l
              ON ss.ticker=l.ticker AND ss.calc_date=l.md
            """
        ).fetchall()
    }

    # 산업 타이밍 (level2_id 최신)
    timing = {
        row[0]: {"timing_state": row[1], "timing_score": row[2]}
        for row in con.execute(
            """
            SELECT ts.level2_id, ts.timing_state, ts.timing_score
            FROM timing_signals ts
            INNER JOIN (SELECT level2_id, MAX(calc_date) md FROM timing_signals GROUP BY level2_id) l
              ON ts.level2_id=l.level2_id AND ts.calc_date=l.md
            """
        ).fetchall()
    }

    result: dict[str, dict] = {}
    for ticker, name, level2_id, level2_name in companies:
        br = briefs.get(ticker, {})
        a  = analyst.get(ticker, {})
        avg_t = round(sum(a["targets"]) / len(a["targets"])) if a.get("targets") else None
        q  = quant.get(ticker, {})
        p  = price.get(ticker, {})
        risks = None
        if br.get("risks"):
            try:
                risks = json.loads(br["risks"])
            except Exception:
                risks = [br["risks"]]
        result[ticker] = {
            "ticker": ticker, "name": name,
            "level2_id": level2_id, "level2_name": level2_name,
            "business": br.get("business"), "moat": br.get("moat"), "risks": risks,
            "brief_source": br.get("source"), "brief_updated": br.get("updated"),
            "close": p.get("close"), "ret_1m": p.get("ret_1m"),
            "analyst": {
                "n_buy": a.get("n_buy", 0), "n_hold": a.get("n_hold", 0),
                "n_sell": a.get("n_sell", 0),
                "avg_target": avg_t, "latest_target": a.get("latest_target"),
                "recent": a.get("recent", []),
            },
            "fundamentals": fundamentals.get(ticker, []),
            "market_share": mshare.get(ticker, []),
            "dart": disclosures.get(ticker),
            "quant": {
                "buy_score": q.get("buy_score"), "rank_in_level2": q.get("rank_in_level2"),
                "upside_pct": q.get("upside_pct"), "composite": q.get("composite"),
                "timing_state": timing.get(level2_id, {}).get("timing_state"),
                "timing_score": timing.get(level2_id, {}).get("timing_score"),
            },
        }
    return result


def build_events(con, limit: int = 40) -> list[dict]:
    """최근 신호 변화 이벤트 (알림 피드)."""
    rows = con.execute(
        """
        SELECT e.event_date, e.level2_id, i.level2_name, e.event_type,
               e.from_state, e.to_state, e.detail
        FROM signal_events e
        JOIN industries i ON e.level2_id = i.level2_id
        ORDER BY e.event_date DESC, e.event_type
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for ev_date, level2_id, name, etype, frm, to, detail in rows:
        try:
            det = json.loads(detail) if detail else {}
        except Exception:
            det = {}
        out.append({
            "date": ev_date, "level2_id": level2_id, "level2_name": name,
            "event_type": etype, "from_state": frm, "to_state": to, "detail": det,
        })
    return out


def build_meta(con) -> dict:
    n_reports = con.execute("SELECT COUNT(*) FROM report_events").fetchone()[0]
    n_companies = con.execute(
        "SELECT COUNT(*) FROM companies WHERE is_representative=1"
    ).fetchone()[0]
    last_report = con.execute(
        "SELECT MAX(published_date) FROM report_events"
    ).fetchone()[0]
    last_price = con.execute(
        "SELECT MAX(date) FROM price_history"
    ).fetchone()[0]

    return {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "n_reports_total": n_reports,
        "n_representative_companies": n_companies,
        "last_report_date": last_report,
        "last_price_date": last_price,
    }


# ─────────────────────────────────────
# 메인
# ─────────────────────────────────────

def run() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)

    con = connect()
    try:
        industries      = build_industries(con)
        reports         = build_reports(con)
        industry_detail = build_industry_detail(con)
        stock_scores    = build_stock_scores(con)
        market_share    = build_market_share(con)
        company_briefs  = build_company_briefs(con)
        events          = build_events(con)
        meta            = build_meta(con)
    finally:
        con.close()

    def _write(name: str, data) -> None:
        path = OUTPUT / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("wrote %s (%d items)", path.name, len(data) if isinstance(data, list) else 1)

    _write("industries.json",      industries)
    _write("reports.json",         reports)
    _write("industry_detail.json", industry_detail)
    _write("stock_scores.json",    stock_scores)
    _write("market_share.json",    market_share)
    _write("company_briefs.json",  company_briefs)
    _write("events.json",          events)
    _write("meta.json",            meta)

    log.info("빌드 완료 → %s", OUTPUT)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    run()
