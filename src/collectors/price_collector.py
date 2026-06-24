"""일봉 시세 & 업종 지수 수집기.

수집 대상:
  price_history          : companies(is_representative=1) + industry_etfs 티커
  industry_index_history : 대표종목 equal-weight 평균 종가 (level2_id 별)

실행:
  python src/collectors/price_collector.py              # 어제 기준
  python src/collectors/price_collector.py 20260620     # 특정일
  python src/collectors/price_collector.py 20260601 20260620  # 기간
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from db import connect  # noqa: E402

try:
    from pykrx import stock as krx
except ImportError:
    print("[ERROR] pykrx 미설치: pip install pykrx")
    sys.exit(1)

log = logging.getLogger(__name__)

DELAY_SEC = 0.3  # pykrx 요청 간격 (과부하 방지)


# ─────────────────────────────────────
# 날짜 유틸
# ─────────────────────────────────────

def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y%m%d")


def _to_db_date(d: str) -> str:
    """'YYYYMMDD' -> 'YYYY-MM-DD'"""
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


# ─────────────────────────────────────
# ETF 종목 사전 등록
# ─────────────────────────────────────

def _ensure_etf_companies(con) -> list[str]:
    """industry_etfs 티커를 companies에 자동 등록 (미존재 시).

    price_history 의 FK(companies.ticker) 제약을 충족하기 위해
    ETF를 companies에 먼저 INSERT OR IGNORE.
    """
    rows = con.execute(
        "SELECT etf_ticker, etf_name, level2_id FROM industry_etfs"
    ).fetchall()
    cur = con.cursor()
    etf_tickers: list[str] = []
    for ticker, name, level2_id in rows:
        cur.execute(
            """INSERT OR IGNORE INTO companies
               (ticker, name, market, level2_id,
                mapping_confidence, is_representative, is_etf)
               VALUES(?, ?, 'ETF', ?, 1.0, 0, 1)""",
            (ticker, name, level2_id),
        )
        etf_tickers.append(ticker)
    con.commit()
    return etf_tickers


# ─────────────────────────────────────
# pykrx OHLCV 수집
# ─────────────────────────────────────

def _ohlcv_rows(ticker: str, fromdate: str, todate: str) -> list[tuple[str, float, int]]:
    """pykrx OHLCV → [(date_str, close, volume), ...]. 빈 결과면 []."""
    df = krx.get_market_ohlcv_by_date(fromdate, todate, ticker)
    if df is None or df.empty:
        return []
    result = []
    for idx, row in df.iterrows():
        d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
        # pykrx 컬럼명: 종가(한국) 또는 Close(영문) 버전 모두 대응
        close = float(row.get("종가", row.get("Close", 0)))
        volume = int(row.get("거래량", row.get("Volume", 0)))
        if close > 0:
            result.append((d, close, volume))
    return result


def _fetch_all_ohlcv(
    tickers: list[str], fromdate: str, todate: str
) -> dict[str, list[tuple[str, float, int]]]:
    """티커 목록 전체 OHLCV 수집. ticker -> [(date, close, volume)] 반환."""
    out: dict[str, list] = {}
    total = len(tickers)
    for i, ticker in enumerate(tickers):
        try:
            rows = _ohlcv_rows(ticker, fromdate, todate)
            if rows:
                out[ticker] = rows
            else:
                log.debug("OHLCV 없음: %s", ticker)
        except Exception as exc:
            log.warning("OHLCV 오류 %s: %s", ticker, exc)
        if (i + 1) % 20 == 0:
            log.info("  %d / %d 종목 수집 완료", i + 1, total)
        time.sleep(DELAY_SEC)
    return out


# ─────────────────────────────────────
# price_history 적재
# ─────────────────────────────────────

def collect_prices(con, fromdate: str, todate: str) -> int:
    """대표종목 + ETF 일봉 → price_history. 반환: INSERT 건수."""
    etf_tickers = _ensure_etf_companies(con)

    rep_tickers = [
        r[0] for r in
        con.execute(
            "SELECT ticker FROM companies WHERE is_representative=1"
        ).fetchall()
    ]
    # 중복 제거, 순서 보존
    all_tickers = list(dict.fromkeys(rep_tickers + etf_tickers))
    log.info(
        "시세 수집: 대표종목 %d + ETF %d = %d종목 (%s~%s)",
        len(rep_tickers), len(etf_tickers), len(all_tickers), fromdate, todate,
    )

    price_data = _fetch_all_ohlcv(all_tickers, fromdate, todate)

    cur = con.cursor()
    inserted = 0
    for ticker, rows in price_data.items():
        for (d, close, volume) in rows:
            cur.execute(
                """INSERT OR IGNORE INTO price_history
                   (ticker, date, close, volume) VALUES(?,?,?,?)""",
                (ticker, d, close, volume),
            )
            inserted += cur.rowcount
    con.commit()
    log.info("price_history INSERT: %d건", inserted)
    return inserted


# ─────────────────────────────────────
# industry_index_history 적재
# ─────────────────────────────────────

def collect_industry_index(con, fromdate: str, todate: str) -> int:
    """대표종목 equal-weight 평균 종가 → industry_index_history.

    price_history 기반으로 산출하므로 collect_prices 이후에 호출해야 함.
    """
    from_db = _to_db_date(fromdate)
    to_db = _to_db_date(todate)

    rows = con.execute(
        """
        SELECT c.level2_id, ph.date, AVG(ph.close) AS avg_close, COUNT(*) AS n
        FROM price_history ph
        JOIN companies c ON ph.ticker = c.ticker
        WHERE c.is_representative = 1
          AND c.level2_id IS NOT NULL
          AND ph.date BETWEEN ? AND ?
        GROUP BY c.level2_id, ph.date
        HAVING n >= 1
        """,
        (from_db, to_db),
    ).fetchall()

    cur = con.cursor()
    inserted = 0
    for level2_id, dt, avg_close, _ in rows:
        cur.execute(
            """INSERT OR IGNORE INTO industry_index_history
               (level2_id, date, close) VALUES(?,?,?)""",
            (level2_id, dt, round(avg_close, 2)),
        )
        inserted += cur.rowcount
    con.commit()
    log.info("industry_index_history INSERT: %d건 (level2 %d종)", inserted, len(rows))
    return inserted


# ─────────────────────────────────────
# 진입점
# ─────────────────────────────────────

def run(fromdate: str | None = None, todate: str | None = None) -> None:
    ref_from = fromdate or _yesterday()
    ref_to = todate or ref_from
    log.info("수집 기간: %s ~ %s", ref_from, ref_to)

    con = connect()
    try:
        collect_prices(con, ref_from, ref_to)
        collect_industry_index(con, ref_from, ref_to)
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    import argparse
    ap = argparse.ArgumentParser(description="일봉 시세 & 업종지수 수집기")
    ap.add_argument("fromdate", nargs="?", default=None, help="시작일 YYYYMMDD (기본: 어제)")
    ap.add_argument("todate",   nargs="?", default=None, help="종료일 YYYYMMDD (기본: fromdate)")
    args = ap.parse_args()

    run(args.fromdate, args.todate)
