"""네이버 금융 리서치센터 크롤러.

수집 대상: finance.naver.com/research/company_list.naver (종목 리포트)
흐름:
  1. 목록 페이지 폴링 → 신규 nid(보고서 ID) 선별
  2. 각 nid 상세 페이지 방문 → 애널리스트·의견 추출
  3. report_events INSERT OR IGNORE (source_url 중복 방지)

실행:
  python src/collectors/naver_research.py           # 최신 1페이지
  python src/collectors/naver_research.py --pages 3 # 3페이지
  python src/collectors/naver_research.py --date 20260620  # 특정일 이후만
"""
from __future__ import annotations

import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from db import connect  # noqa: E402

log = logging.getLogger(__name__)

BASE = "https://finance.naver.com"
LIST_URL = f"{BASE}/research/company_list.naver"
READ_URL = f"{BASE}/research/company_read.naver"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE}/research/",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
DELAY_SEC = 1.2  # 요청 간 정중 대기


# ─────────────────────────────────────
# 파싱 헬퍼
# ─────────────────────────────────────

def _get(url: str, params: dict | None = None) -> BeautifulSoup:
    resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = "euc-kr"
    return BeautifulSoup(resp.text, "html.parser")


def _parse_date(raw: str) -> str:
    """'26.06.20' 또는 '2026.06.20' -> 'YYYY-MM-DD'"""
    raw = raw.strip()
    m = re.search(r"(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw)
    if m:
        y, mo, d_ = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        if len(y) == 2:
            y = "20" + y
        return f"{y}-{mo}-{d_}"
    return date.today().isoformat()


def _parse_price(raw: str) -> float | None:
    digits = re.sub(r"[^\d]", "", raw)
    return float(digits) if digits else None


def _extract_nid(href: str) -> str | None:
    m = re.search(r"nid=(\d+)", href)
    return m.group(1) if m else None


# ─────────────────────────────────────
# 목록 페이지
# ─────────────────────────────────────

def parse_list_page(page: int = 1) -> list[dict]:
    """종목 리포트 목록 한 페이지 파싱.

    실제 네이버 금융 리서치 컬럼 순서 (2026-06 확인):
      [0] 종목명  [1] 제목+nid  [2] 증권사  [3] 첨부PDF  [4] 날짜  [5] 조회수

    반환: [{stock_name, title, nid, source_url, broker, pdf_available, published_date}, ...]
    """
    soup = _get(LIST_URL, params={"page": page})
    rows: list[dict] = []

    table = soup.select_one("table.type_1")
    if table is None:
        log.warning("목록 테이블을 찾을 수 없음 (page=%d) — HTML 구조 변경 가능성", page)
        return rows

    for tr in table.select("tr"):
        tds = tr.select("td")
        if len(tds) < 5:
            continue

        # 0: 종목명
        stock_link = tds[0].select_one("a")
        if not stock_link:
            continue
        stock_name = stock_link.get_text(strip=True)

        # 1: 리포트 제목 + nid
        title_link = tds[1].select_one("a")
        if not title_link:
            continue
        title = title_link.get_text(strip=True)
        href = title_link.get("href", "")
        nid = _extract_nid(href)
        if not nid:
            continue
        source_url = f"{BASE}/research/company_read.naver?nid={nid}"

        # 2: 증권사
        broker = tds[2].get_text(strip=True)

        # 3: 첨부(PDF) 여부 — 링크 존재 여부로 판단 (목표가는 상세 페이지에서만)
        pdf_available = 1 if tds[3].select_one("a") else 0

        # 4: 날짜
        published_date = _parse_date(tds[4].get_text(strip=True))

        rows.append({
            "stock_name": stock_name,
            "title": title,
            "nid": nid,
            "source_url": source_url,
            "broker": broker,
            "pdf_available": pdf_available,
            "published_date": published_date,
        })

    return rows


# ─────────────────────────────────────
# 상세 페이지
# ─────────────────────────────────────

def parse_detail(nid: str) -> dict:
    """상세 페이지에서 목표가·투자의견·이전목표가·PDF URL 추출.

    네이버 금융 리서치 구조 (2026-06 확인):
      - 전문 텍스트에 "목표가 240,000 | 투자의견 매수" 패턴 존재
      - PDF는 stock.pstatic.net 도메인으로 제공 (공개 PDF → broker_url로 활용)
      - 애널리스트명은 HTML에 미노출 (PDF 내부에만 있음) → authors_raw=None

    파싱 실패 시 빈 값(None)으로 graceful 반환.
    """
    result: dict = {
        "analyst_raw": None,
        "target_price": None,
        "opinion": None,
        "prev_target": None,
        "broker_url": None,
        "pdf_available": 0,
    }
    try:
        soup = _get(READ_URL, params={"nid": nid})
        body = soup.get_text(" ", strip=True)

        # 목표가
        m_tp = re.search(r"목표가\s*([\d,]+)", body)
        if m_tp:
            result["target_price"] = _parse_price(m_tp.group(1))

        # 투자의견
        m_op = re.search(
            r"투자의견\s*(매수|적극\s*매수|중립|보유|매도|BUY|HOLD|SELL|STRONG\s*BUY)",
            body, re.I,
        )
        if m_op:
            result["opinion"] = m_op.group(1).strip()

        # 이전 목표가 패턴: "35,000 → 45,000" 또는 "35,000 -> 45,000"
        m_prev = re.search(
            r"([\d,]{4,})\s*(?:원)?\s*(?:->|→|▶|⟶)\s*([\d,]{4,})",
            body,
        )
        if m_prev:
            result["prev_target"] = _parse_price(m_prev.group(1))

        # PDF URL (pstatic.net) → broker_url로 활용 (공개 PDF 직접 링크)
        pstatic = soup.select_one("a[href*='pstatic.net']")
        if pstatic:
            result["broker_url"] = pstatic["href"]
            result["pdf_available"] = 1

    except Exception as exc:
        log.warning("상세 파싱 실패 nid=%s: %s", nid, exc)

    return result


# ─────────────────────────────────────
# DB 헬퍼
# ─────────────────────────────────────

def _known_nids(con) -> set[str]:
    """이미 수집된 source_url에서 nid 추출."""
    rows = con.execute("SELECT source_url FROM report_events").fetchall()
    result = set()
    for (url,) in rows:
        nid = _extract_nid(url or "")
        if nid:
            result.add(nid)
    return result


def _resolve_ticker(con, stock_name: str) -> str | None:
    """종목명 → ticker (companies 테이블 조회, 부분 일치 허용)"""
    row = con.execute(
        "SELECT ticker FROM companies WHERE name=? LIMIT 1", (stock_name,)
    ).fetchone()
    if row:
        return row[0]
    # 부분 일치 (예: '한화오션' vs '한화오션(주)')
    row = con.execute(
        "SELECT ticker FROM companies WHERE name LIKE ? LIMIT 1",
        (f"{stock_name[:4]}%",),
    ).fetchone()
    return row[0] if row else None


def _resolve_analyst(con, name: str, broker: str) -> int | None:
    """애널리스트명+증권사 → analyst_id. 없으면 INSERT 후 반환."""
    if not name:
        return None
    row = con.execute(
        "SELECT analyst_id FROM analysts WHERE name=? AND broker=?", (name, broker)
    ).fetchone()
    if row:
        return row[0]
    con.execute(
        "INSERT OR IGNORE INTO analysts(name, broker, active) VALUES(?,?,1)",
        (name, broker),
    )
    row = con.execute(
        "SELECT analyst_id FROM analysts WHERE name=? AND broker=?", (name, broker)
    ).fetchone()
    return row[0] if row else None


def _insert_report(con, rec: dict) -> bool:
    try:
        con.execute(
            """INSERT OR IGNORE INTO report_events
               (published_date, broker, analyst_id, authors_raw,
                ticker, title, opinion,
                target_price, prev_target,
                source_url, broker_url,
                pdf_available, has_summary)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                rec["published_date"], rec["broker"], rec.get("analyst_id"),
                rec.get("analyst_raw"),
                rec.get("ticker"), rec["title"], rec.get("opinion"),
                rec.get("target_price"), rec.get("prev_target"),
                rec["source_url"], rec.get("broker_url"),
                rec["pdf_available"],
            ),
        )
        return con.execute("SELECT changes()").fetchone()[0] == 1
    except Exception as exc:
        log.error("INSERT 실패 nid=%s: %s", rec.get("nid"), exc)
        return False


# ─────────────────────────────────────
# prev_target 자동 산출 (리비전 신호 활성화)
# ─────────────────────────────────────

def backfill_prev_targets(con) -> int:
    """prev_target을 '같은 증권사(broker)'의 직전 목표가로 채운다 (증권사별 리비전).

    같은 종목이라도 증권사가 다르면 목표가 수준이 달라(교차오염) → 반드시 동일
    증권사의 직전 목표가와 비교해야 진짜 상향/하향 리비전이 된다.
    권위적·멱등: 매 실행 전체 재계산(동일 증권사 직전값이 없으면 NULL).
    """
    cur = con.execute(
        """
        UPDATE report_events AS r
        SET prev_target = (
            SELECT p.target_price FROM report_events p
            WHERE p.ticker = r.ticker
              AND p.broker = r.broker
              AND p.target_price IS NOT NULL
              AND p.target_price > 0
              AND ( p.published_date < r.published_date
                    OR (p.published_date = r.published_date AND p.report_id < r.report_id) )
            ORDER BY p.published_date DESC, p.report_id DESC
            LIMIT 1
        )
        WHERE r.ticker IS NOT NULL
          AND r.target_price IS NOT NULL
        """
    )
    con.commit()
    n = con.execute(
        "SELECT COUNT(*) FROM report_events WHERE prev_target IS NOT NULL"
    ).fetchone()[0]
    log.info("prev_target(증권사별) 산출: %d건", n)
    return n


# ─────────────────────────────────────
# 진입점
# ─────────────────────────────────────

def run(max_pages: int = 1, since_date: str | None = None) -> None:
    """
    max_pages: 수집할 목록 페이지 수
    since_date: 'YYYY-MM-DD' 이후 리포트만 수집 (None = 제한 없음)
    """
    con = connect()
    try:
        known = _known_nids(con)
        log.info("기존 수집 nid: %d건", len(known))

        inserted = skipped_dup = skipped_old = 0

        for page in range(1, max_pages + 1):
            log.info("목록 수집 page=%d", page)
            items = parse_list_page(page)
            if not items:
                break
            time.sleep(DELAY_SEC)

            stop_page = False
            for item in items:
                # 날짜 필터
                if since_date and item["published_date"] < since_date:
                    skipped_old += 1
                    stop_page = True
                    continue

                # 중복 필터
                if item["nid"] in known:
                    skipped_dup += 1
                    continue

                # 상세 페이지 방문
                detail = parse_detail(item["nid"])
                time.sleep(DELAY_SEC)

                # ticker 해석
                ticker = _resolve_ticker(con, item["stock_name"])
                if ticker is None:
                    log.debug("ticker 미매핑: %s", item["stock_name"])

                # analyst_id 해석 (없으면 자동 등록)
                analyst_id = _resolve_analyst(
                    con, detail.get("analyst_raw"), item["broker"]
                )

                # detail의 target_price/pdf_available이 list보다 우선 (더 정확)
                merged = {**item, **{k: v for k, v in detail.items() if v is not None}}
                rec = {**merged, "ticker": ticker, "analyst_id": analyst_id}
                if _insert_report(con, rec):
                    inserted += 1
                    known.add(item["nid"])
                    log.info(
                        "NEW [%s] %s / %s / 목표가 %s",
                        item["published_date"], item["broker"],
                        item["title"][:30], rec.get("target_price"),
                    )

            if stop_page:
                log.info("since_date(%s) 도달 — 수집 중단", since_date)
                break

        con.commit()
        # 직전 목표가 자동 산출 → 리비전 신호 활성화
        backfill_prev_targets(con)
        log.info(
            "완료: 신규 %d건 / 중복 스킵 %d건 / 날짜 스킵 %d건",
            inserted, skipped_dup, skipped_old,
        )
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    import argparse
    ap = argparse.ArgumentParser(description="네이버 리서치 크롤러")
    ap.add_argument("--pages", type=int, default=1, help="수집 페이지 수")
    ap.add_argument("--date", default=None, help="수집 기준일 YYYYMMDD (이후만)")
    args = ap.parse_args()

    since = None
    if args.date:
        d = args.date
        since = f"{d[:4]}-{d[4:6]}-{d[6:]}"

    run(max_pages=args.pages, since_date=since)
