"""DART 공시 기반 IR·배당 캘린더 수집기.

data/seed/watchlist.json의 관심종목에 대해 DART 공시검색(list.json)에서
"기업설명회(IR) 개최" / "현금·현물배당결정" 공시를 찾아 ir_events에 적재한다.

주의 — rcept_dt는 "공시 발표일"이다:
  DART list.json은 공시 제목·발표일 등 메타데이터만 제공하고, 실제 IR
  개최일·배당기준일/지급일 같은 구조화된 일정 필드는 제공하지 않는다.
  정확한 일정은 공시 원문(dart_url)에서 확인해야 한다 — 리포트 카드의
  원문 링크 원칙(CLAUDE.md 7)과 동일하게, 화면에는 항상 원문 링크를 같이 보여준다.

환경변수:
    DART_API_KEY  — dart.fss.or.kr 인증키 (없으면 skip)

실행:
    python src/collectors/dart_calendar.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from db import connect
from collectors.dart_collector import _load_corp_codes  # ticker → corp_code (캐시 공유)

log = logging.getLogger(__name__)

DART_API = "https://opendart.fss.or.kr/api"
LOOKBACK_DAYS = 730  # 최근 2년 공시 이력
DELAY_SEC = 0.3

# report_nm 부분일치 → event_type
KEYWORD_MAP: dict[str, str] = {
    "기업설명회": "ir",
    "IR개최": "ir",
    "배당": "dividend",
}


def _load_watchlist() -> list[str]:
    path = ROOT / "data" / "seed" / "watchlist.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [w["ticker"] for w in data.get("watchlist", []) if w.get("ticker")]


def _classify(report_nm: str) -> str | None:
    for kw, etype in KEYWORD_MAP.items():
        if kw in report_nm:
            return etype
    return None


def _fetch_disclosures(api_key: str, corp_code: str, bgn_de: str, end_de: str) -> list[dict]:
    """DART 공시검색(list.json) — 단일 corp_code, 최대 100건/페이지."""
    out: list[dict] = []
    page = 1
    while True:
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": page,
            "page_count": 100,
        }
        try:
            resp = requests.get(f"{DART_API}/list.json", params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("DART 공시검색 오류 corp_code=%s: %s", corp_code, exc)
            break

        status = data.get("status")
        if status == "013":  # 조회된 데이터 없음 (정상)
            break
        if status not in ("000", 0, "0"):
            log.debug("DART list.json status=%s corp_code=%s", status, corp_code)
            break

        rows = data.get("list", [])
        out.extend(rows)
        if len(rows) < 100:
            break
        page += 1
        time.sleep(DELAY_SEC)

    return out


def run(con=None) -> None:
    api_key = os.environ.get("DART_API_KEY", "").strip()
    if not api_key:
        log.warning("DART_API_KEY 미설정 — dart_calendar 스킵")
        return

    tickers = _load_watchlist()
    if not tickers:
        log.warning("watchlist.json에 종목 없음 — dart_calendar 스킵")
        return

    own = con is None
    if own:
        con = connect()

    try:
        code_map = _load_corp_codes(api_key)
        missing = [t for t in tickers if t not in code_map]
        if missing:
            log.warning("corp_code 없는 관심종목 %d개: %s", len(missing), missing)

        end_de = date.today().strftime("%Y%m%d")
        bgn_de = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")

        total = 0
        for ticker in tickers:
            corp_code = code_map.get(ticker)
            if not corp_code:
                continue
            rows = _fetch_disclosures(api_key, corp_code, bgn_de, end_de)
            time.sleep(DELAY_SEC)

            saved = 0
            for r in rows:
                report_nm = r.get("report_nm", "")
                event_type = _classify(report_nm)
                if event_type is None:
                    continue
                rcept_no = r.get("rcept_no", "")
                rcept_dt = r.get("rcept_dt", "")
                if not rcept_no or not rcept_dt:
                    continue
                event_date = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}"
                dart_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
                cur = con.execute(
                    """INSERT OR IGNORE INTO ir_events
                       (ticker, rcept_no, event_type, rcept_dt, report_nm, dart_url)
                       VALUES (?,?,?,?,?,?)""",
                    (ticker, rcept_no, event_type, event_date, report_nm, dart_url),
                )
                saved += cur.rowcount
            if rows:
                log.info("적재 [%s] 공시 %d건 중 IR/배당 %d건", ticker, len(rows), saved)
            total += saved

        con.commit()
        log.info("dart_calendar 완료 — %d개 종목 처리, 신규 %d건 적재", len(tickers), total)
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
