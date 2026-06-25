"""DART OpenAPI 재무 수집기.

대표 종목의 분기/반기/연간 손익계산서 핵심 항목을 수집해 company_fundamentals에 적재.

환경변수:
    DART_API_KEY  — dart.fss.or.kr 에서 발급받은 API 인증키 (없으면 skip)

실행:
    python src/collectors/dart_collector.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys_path_prepend = str(ROOT / "src")

import sys
if sys_path_prepend not in sys.path:
    sys.path.insert(0, sys_path_prepend)

from db import connect

log = logging.getLogger(__name__)

DART_API   = "https://opendart.fss.or.kr/api"
CACHE_PATH = ROOT / "data" / "dart_corp_codes.json"

# DART 계정명 → company_fundamentals 컬럼 매핑
ACCOUNT_MAP: dict[str, str] = {
    "매출액":               "revenue",
    "순매출액":             "revenue",
    "영업수익":             "revenue",
    "영업이익":             "op_income",
    "영업이익(손실)":       "op_income",
    "당기순이익":           "net_income",
    "당기순이익(손실)":     "net_income",
    "지배기업 소유주 귀속 당기순이익": "net_income",
    "기본주당순이익(손실)": "eps_actual",
    "기본주당이익(손실)":   "eps_actual",
    "주당순이익":           "eps_actual",
}

# (reprt_code, 분기말 월-일) → period_end 계산용
REPRT_PERIOD: dict[str, str] = {
    "11011": "12-31",  # 사업보고서 (연간)
    "11012": "06-30",  # 반기보고서
    "11013": "09-30",  # 3분기보고서
    "11014": "03-31",  # 1분기보고서
}


# ─────────────────────────────────────
# corp_code 캐시 (ticker → corp_code)
# ─────────────────────────────────────

def _download_corp_codes(api_key: str) -> dict[str, str]:
    """DART 종목코드 ZIP을 받아 ticker → corp_code dict 반환."""
    url  = f"{DART_API}/corpCode.xml"
    resp = requests.get(url, params={"crtfc_key": api_key}, timeout=30)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")

    root = ET.fromstring(xml_bytes)
    mapping: dict[str, str] = {}
    for item in root.findall("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code  = (item.findtext("corp_code")  or "").strip()
        if stock_code:
            mapping[stock_code] = corp_code

    log.info("DART corp_code 매핑 %d건 다운로드", len(mapping))
    return mapping


def _load_corp_codes(api_key: str) -> dict[str, str]:
    """캐시에서 읽거나 없으면 다운로드. 30일 이상 된 캐시는 갱신."""
    if CACHE_PATH.exists():
        mtime = CACHE_PATH.stat().st_mtime
        age_days = (time.time() - mtime) / 86400
        if age_days < 30:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    mapping = _download_corp_codes(api_key)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping


# ─────────────────────────────────────
# DART 재무 API 호출
# ─────────────────────────────────────

def _fetch_financials(
    corp_codes: list[str],
    year: int,
    reprt_code: str,
    api_key: str,
    fs_div: str = "CFS",
) -> list[dict]:
    """fnlttMultiAcnt: 최대 100개 corp_code 한 번에 조회."""
    results: list[dict] = []
    CHUNK = 100
    for i in range(0, len(corp_codes), CHUNK):
        chunk = corp_codes[i : i + CHUNK]
        params = {
            "crtfc_key":  api_key,
            "corp_code":  ",".join(chunk),
            "bsns_year":  str(year),
            "reprt_code": reprt_code,
            "fs_div":     fs_div,
        }
        try:
            resp = requests.get(
                f"{DART_API}/fnlttMultiAcnt.json",
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("DART API 오류 (year=%s, reprt=%s): %s", year, reprt_code, exc)
            continue

        if data.get("status") not in ("000", 0, "0"):
            log.debug("DART 응답 status=%s (year=%s, reprt=%s)", data.get("status"), year, reprt_code)
            continue

        results.extend(data.get("list", []))
        time.sleep(0.3)

    return results


# ─────────────────────────────────────
# DB 저장
# ─────────────────────────────────────

def _upsert_rows(con, ticker_from_corp: dict[str, str], rows: list[dict]) -> int:
    """DART 응답 rows → company_fundamentals INSERT OR REPLACE."""
    # corp_code → {account_col: amount} 집계
    agg: dict[str, dict] = {}
    for row in rows:
        corp  = row.get("corp_code", "")
        acct  = row.get("account_nm", "").strip()
        col   = ACCOUNT_MAP.get(acct)
        if col is None:
            continue

        ticker = ticker_from_corp.get(corp)
        if ticker is None:
            continue

        key = (ticker, row.get("bsns_year", ""), row.get("reprt_code", ""))
        entry = agg.setdefault(key, {})
        # 당기 금액 우선 (숫자로 변환, 쉼표 제거)
        raw = row.get("thstrm_amount", "").replace(",", "").strip()
        try:
            entry[col] = float(raw) if raw and raw not in ("-", "") else None
        except ValueError:
            entry[col] = None

    inserted = 0
    for (ticker, bsns_year, reprt_code), vals in agg.items():
        suffix = REPRT_PERIOD.get(reprt_code)
        if suffix is None or not bsns_year:
            continue

        period_end  = f"{bsns_year}-{suffix}"
        revenue     = vals.get("revenue")
        op_income   = vals.get("op_income")
        op_margin   = round(op_income / revenue * 100, 2) if (revenue and op_income and revenue != 0) else None
        net_income  = vals.get("net_income")
        eps_actual  = vals.get("eps_actual")

        con.execute(
            """
            INSERT OR REPLACE INTO company_fundamentals
                (ticker, period_end, revenue, op_income, op_margin,
                 net_income, eps_actual, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'DART')
            """,
            (ticker, period_end, revenue, op_income, op_margin, net_income, eps_actual),
        )
        inserted += 1

    con.commit()
    return inserted


# ─────────────────────────────────────
# 메인
# ─────────────────────────────────────

def run(con=None) -> None:
    api_key = os.environ.get("DART_API_KEY", "").strip()
    if not api_key:
        log.warning("DART_API_KEY 미설정 — dart_collector 스킵")
        return

    own_con = con is None
    if own_con:
        con = connect()

    try:
        # 대표 종목 ticker 목록
        tickers: list[str] = [
            r[0] for r in con.execute(
                "SELECT ticker FROM companies WHERE is_representative=1 AND is_etf=0"
            ).fetchall()
        ]
        if not tickers:
            log.warning("대표 종목 없음 — dart_collector 스킵")
            return

        log.info("대표 종목 %d개 DART 재무 수집 시작", len(tickers))

        # ticker → corp_code 매핑
        code_map   = _load_corp_codes(api_key)
        ticker_from_corp = {v: k for k, v in code_map.items() if k in tickers}
        corp_codes = [code_map[t] for t in tickers if t in code_map]
        missing    = [t for t in tickers if t not in code_map]
        if missing:
            log.debug("corp_code 없는 종목 %d개: %s", len(missing), missing[:5])

        if not corp_codes:
            log.warning("유효한 corp_code 없음 — dart_collector 스킵")
            return

        today = date.today()
        years = [today.year - 1, today.year]
        reprt_codes = ["11011", "11012", "11013", "11014"]

        total = 0
        for year in years:
            for reprt_code in reprt_codes:
                rows = _fetch_financials(corp_codes, year, reprt_code, api_key)
                if rows:
                    n = _upsert_rows(con, ticker_from_corp, rows)
                    total += n
                    log.info("year=%s reprt=%s → %d건 적재", year, reprt_code, n)

        log.info("DART 재무 적재 완료 — 총 %d건", total)

    finally:
        if own_con:
            con.close()


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
