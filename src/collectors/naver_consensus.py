"""네이버 컨센서스 EPS 수집기 (L2 선행 레이어).

finance.naver.com 기업실적분석 표에서 '연간 (E)' 컬럼 = forward 컨센서스 EPS.
매일 스냅샷으로 저장 → 같은 fwd_year 스냅샷 간 변화 = EPS 추정치 리비전.

실행:
  python src/collectors/naver_consensus.py
"""
from __future__ import annotations

import logging
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from db import connect

log = logging.getLogger(__name__)

MAIN_URL = "https://finance.naver.com/item/main.naver"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}
DELAY_SEC = 0.5


def _num(s: str) -> float | None:
    s = (s or "").replace(",", "").strip()
    if not s or s in ("-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_consensus(html: str) -> dict | None:
    """기업실적분석 표 → {fwd_year, eps, op_income}. 실패 시 None."""
    soup = BeautifulSoup(html, "html.parser")
    div = soup.select_one(".section.cop_analysis")
    if not div:
        return None
    t = div.select_one("table.tb_type1.tb_num")
    if not t:
        return None

    head_rows = t.select("thead tr")
    if len(head_rows) < 2:
        return None
    periods = [c.get_text(" ", strip=True) for c in head_rows[1].select("th,td")]

    # 연간 forward = 좌→우 첫 '(E)' 컬럼 (연간이 분기보다 앞)
    fwd_idx = next((i for i, p in enumerate(periods) if "(E)" in p), None)
    if fwd_idx is None:
        return None
    m = re.match(r"(\d{4})", periods[fwd_idx])
    fwd_year = int(m.group(1)) if m else None
    if fwd_year is None:
        return None

    def row_vals(keyword: str) -> list[str] | None:
        for tr in t.select("tbody tr"):
            th = tr.select_one("th")
            name = th.get_text(" ", strip=True) if th else ""
            if keyword in name:
                return [td.get_text(strip=True) for td in tr.select("td")]
        return None

    eps_vals = row_vals("EPS")
    op_vals  = row_vals("영업이익")
    eps = _num(eps_vals[fwd_idx]) if eps_vals and fwd_idx < len(eps_vals) else None
    op  = _num(op_vals[fwd_idx])  if op_vals  and fwd_idx < len(op_vals)  else None
    if eps is None:
        return None
    return {"fwd_year": fwd_year, "eps": eps, "op_income": op}


def _fetch(ticker: str) -> dict | None:
    try:
        r = requests.get(MAIN_URL, params={"code": ticker}, headers=HEADERS, timeout=20)
        r.raise_for_status()
        r.encoding = "euc-kr"
        return parse_consensus(r.text)
    except Exception as exc:
        log.debug("컨센서스 파싱 실패 %s: %s", ticker, exc)
        return None


def run(con=None) -> None:
    own = con is None
    if own:
        con = connect()
    snapshot = date.today().isoformat()

    try:
        tickers = [r[0] for r in con.execute(
            "SELECT ticker FROM companies WHERE is_representative=1 AND is_etf=0"
        ).fetchall()]
        log.info("컨센서스 EPS 수집 — %d종목 (snapshot=%s)", len(tickers), snapshot)

        saved = 0
        for i, ticker in enumerate(tickers):
            data = _fetch(ticker)
            time.sleep(DELAY_SEC)
            if not data:
                continue
            con.execute(
                """INSERT OR IGNORE INTO eps_consensus
                   (ticker, fwd_year, snapshot, eps, op_income) VALUES(?,?,?,?,?)""",
                (ticker, data["fwd_year"], snapshot, data["eps"], data["op_income"]),
            )
            saved += 1
            if (i + 1) % 30 == 0:
                con.commit()
                log.info("  %d / %d", i + 1, len(tickers))
        con.commit()
        log.info("eps_consensus 저장 완료 — %d종목", saved)
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
