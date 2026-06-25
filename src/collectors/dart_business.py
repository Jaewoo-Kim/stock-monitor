"""DART 사업보고서 원문 발췌 수집기.

대표 종목의 최신 정기보고서(사업/반기/분기)를 받아
'사업의 개요'·'위험요소' 섹션 텍스트를 발췌해 company_disclosures에 적재.

흐름:
  1. list.json — 회사의 최근 정기공시에서 가장 최신 보고서 rcept_no 확보
  2. document.xml — 보고서 원문 ZIP 다운로드 → XML 텍스트화
  3. 휴리스틱 섹션 추출(사업의 개요 / 위험요소) → 발췌 저장

특성:
  - 무료 자동소스(DART). DART_API_KEY 없으면 skip.
  - 호출량 절약: 미수집 or 90일 경과 종목만, 1회 실행당 최대 MAX_PER_RUN 종목.
  - 원문 포맷이 다양해 추출 실패 가능 → graceful(None 저장 안 함, 다음 실행에 재시도).

실행:
  python src/collectors/dart_business.py
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from db import connect
from collectors.dart_collector import _load_corp_codes  # corp_code 캐시 재사용

log = logging.getLogger(__name__)

DART_API = "https://opendart.fss.or.kr/api"
MAX_PER_RUN = 20          # 1회 실행당 처리 종목 수 (호출량·시간 절약)
REFRESH_DAYS = 90         # 이 일수 지난 발췌만 갱신
SECTION_MAXLEN = 1600     # 섹션 발췌 최대 길이
DELAY_SEC = 0.4

BIZ_HEADINGS = ["사업의 개요", "사업의 내용", "회사의 개요", "주요 제품 및 서비스"]
RISK_HEADINGS = ["위험요소", "주요 위험", "투자위험", "사업위험"]
# 다음 섹션 시작으로 보고 컷할 헤딩(발췌 길이 보정용)
STOP_HEADINGS = ["주요 제품", "원재료", "생산 및 설비", "매출 및 수주", "재무에 관한 사항",
                 "이사회", "주주에 관한 사항", "임원 및 직원"]


# ─────────────────────────────────────
# DART API
# ─────────────────────────────────────

def _latest_report(corp_code: str, api_key: str) -> dict | None:
    """최근 정기보고서(사업>반기>분기 우선) 1건의 rcept_no 등 반환."""
    bgn = (date.today() - timedelta(days=460)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    try:
        resp = requests.get(
            f"{DART_API}/list.json",
            params={
                "crtfc_key": api_key, "corp_code": corp_code,
                "bgn_de": bgn, "end_de": end,
                "pblntf_ty": "A",        # 정기공시
                "page_count": "100",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("list.json 오류 corp=%s: %s", corp_code, exc)
        return None

    if data.get("status") not in ("000", 0, "0"):
        return None

    items = data.get("list", [])
    if not items:
        return None

    def _rank(nm: str) -> int:
        if "사업보고서" in nm:
            return 0
        if "반기보고서" in nm:
            return 1
        if "분기보고서" in nm:
            return 2
        return 3

    # 보고서 우선순위 → 접수일 최신
    items.sort(key=lambda r: (_rank(r.get("report_nm", "")), -int(r.get("rcept_dt", "0") or 0)))
    best = items[0]
    return {
        "rcept_no": best.get("rcept_no"),
        "report_nm": best.get("report_nm", "").strip(),
        "rcept_dt": best.get("rcept_dt"),
    }


def _fetch_document_text(rcept_no: str, api_key: str) -> str | None:
    """document.xml ZIP → 원문 텍스트(태그 제거)."""
    try:
        resp = requests.get(
            f"{DART_API}/document.xml",
            params={"crtfc_key": api_key, "rcept_no": rcept_no},
            timeout=40,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("document.xml 오류 rcept=%s: %s", rcept_no, exc)
        return None

    raw_parts: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith((".xml", ".html", ".htm")):
                    continue
                blob = zf.read(name)
                text = None
                for enc in ("utf-8", "cp949", "euc-kr"):
                    try:
                        text = blob.decode(enc)
                        break
                    except Exception:
                        continue
                if text:
                    raw_parts.append(text)
    except zipfile.BadZipFile:
        # ZIP이 아니면 본문 자체가 XML일 수 있음
        for enc in ("utf-8", "cp949"):
            try:
                raw_parts.append(resp.content.decode(enc))
                break
            except Exception:
                continue

    if not raw_parts:
        return None
    return "\n".join(raw_parts)


# ─────────────────────────────────────
# 섹션 추출
# ─────────────────────────────────────

def _plain_text(xml: str) -> str:
    t = re.sub(r"<[^>]+>", " ", xml)        # 태그 제거
    t = re.sub(r"&[a-zA-Z#0-9]+;", " ", t)  # 엔티티 제거
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _extract_section(plain: str, headings: list[str], max_len: int = SECTION_MAXLEN) -> str | None:
    for kw in headings:
        idx = plain.find(kw)
        if idx < 0:
            continue
        chunk = plain[idx: idx + max_len + 400]
        # 다음 주요 섹션 헤딩에서 컷
        cut = len(chunk)
        for stop in STOP_HEADINGS:
            sidx = chunk.find(stop, len(kw) + 20)
            if 0 < sidx < cut:
                cut = sidx
        result = chunk[:cut].strip()
        result = re.sub(r"\s+", " ", result)
        if len(result) > max_len:
            result = result[:max_len].rsplit(" ", 1)[0] + " …"
        # 너무 짧으면 의미 없음
        return result if len(result) >= 40 else None
    return None


# ─────────────────────────────────────
# DB
# ─────────────────────────────────────

def _targets(con) -> list[tuple[str, str]]:
    """수집 대상(미수집 or 오래된) 대표종목 [(ticker, name)] — 최대 MAX_PER_RUN."""
    cutoff = (date.today() - timedelta(days=REFRESH_DAYS)).isoformat()
    rows = con.execute(
        """
        SELECT c.ticker, c.name
        FROM companies c
        LEFT JOIN company_disclosures d ON c.ticker = d.ticker
        WHERE c.is_representative = 1 AND c.is_etf = 0
          AND (d.ticker IS NULL OR COALESCE(d.fetched_at, '0') < ?)
        ORDER BY (d.fetched_at IS NOT NULL), c.ticker
        LIMIT ?
        """,
        (cutoff, MAX_PER_RUN),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _save(con, ticker: str, rep: dict, biz: str | None, risk: str | None) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO company_disclosures
            (ticker, rcept_no, report_nm, rcept_dt, biz_overview, risk_text, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker, rep.get("rcept_no"), rep.get("report_nm"), rep.get("rcept_dt"),
         biz, risk, date.today().isoformat()),
    )
    con.commit()


# ─────────────────────────────────────
# 메인
# ─────────────────────────────────────

def run(con=None) -> None:
    api_key = os.environ.get("DART_API_KEY", "").strip()
    if not api_key:
        log.warning("DART_API_KEY 미설정 — dart_business 스킵")
        return

    own = con is None
    if own:
        con = connect()

    try:
        targets = _targets(con)
        if not targets:
            log.info("dart_business: 갱신 대상 없음 (모두 최신)")
            return

        code_map = _load_corp_codes(api_key)
        log.info("dart_business: %d개 종목 사업보고서 발췌 시작", len(targets))

        ok = 0
        for ticker, name in targets:
            corp = code_map.get(ticker)
            if not corp:
                log.debug("corp_code 없음: %s %s", ticker, name)
                continue
            rep = _latest_report(corp, api_key)
            time.sleep(DELAY_SEC)
            if not rep or not rep.get("rcept_no"):
                continue
            xml = _fetch_document_text(rep["rcept_no"], api_key)
            time.sleep(DELAY_SEC)
            if not xml:
                continue
            plain = _plain_text(xml)
            biz = _extract_section(plain, BIZ_HEADINGS)
            risk = _extract_section(plain, RISK_HEADINGS)
            if biz or risk:
                _save(con, ticker, rep, biz, risk)
                ok += 1
                log.info("발췌 저장 [%s] %s — 개요 %s / 위험 %s",
                         ticker, name,
                         f"{len(biz)}자" if biz else "없음",
                         f"{len(risk)}자" if risk else "없음")

        log.info("dart_business 완료 — %d/%d 저장", ok, len(targets))
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
