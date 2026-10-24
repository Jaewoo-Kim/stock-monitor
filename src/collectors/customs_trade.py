"""관세청 무역통계 오픈API 수집기 (L0 수출 지표).

data/seed/industry_indicators.json 의 taxonomy에서 source="customs"이고
customs.verified=true인 지표만 품목별 수출입실적(HS코드 기준) API로 월간
수출액 시계열을 받아 industry_indicators에 적재한다.

환경변수:
    CUSTOMS_API_KEY  — data.go.kr 공공데이터포털 일반 인증키
                       (회원가입 → "관세청_품목별 수출입실적(GW)" 활용신청 → 승인 후 발급)
                       https://www.data.go.kr/data/15101609/openapi.do
                       없으면 skip.

중요 — hs_code는 1차 추정치다 (docs/06 참조):
  이 프로젝트는 이 API를 실제로 호출해 응답 스키마(엔드포인트 경로·파라미터명·
  응답 필드명)를 검증할 네트워크 접근 권한이 없는 환경에서 작성됐다. taxonomy의
  customs.verified가 true로 바뀌기 전까지는 해당 지표를 항상 skip한다 — 틀린
  HS코드로 잘못된 수치가 조용히 신호 엔진에 들어가는 것을 막기 위함
  (CLAUDE.md 원칙 3: 출처·정확도 우선).

  검증 절차:
    1. 위 URL에서 활용신청 → serviceKey 발급 → CUSTOMS_API_KEY로 등록
    2. 활용가이드 문서에서 실제 엔드포인트 경로·요청 파라미터명(강력 추정: strtYymm/
       endYymm/hsSgn)·응답 필드명(강력 추정: expDlrAmt)을 확인
    3. 아래 CUSTOMS_BASE/PARAM 상수와 _parse_items()를 실제 응답에 맞게 수정
    4. industry_indicators.json에서 해당 지표의 customs.verified를 true로 변경

실행:
    python src/collectors/customs_trade.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from db import connect

log = logging.getLogger(__name__)

# 미검증 — docs/06 절차대로 실제 API 문서 확인 후 조정
CUSTOMS_BASE = "https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList"
DELAY_SEC = 0.4


def _load_taxonomy() -> list[dict]:
    path = ROOT / "data" / "seed" / "industry_indicators.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("indicators", [])


def _parse_items(payload: dict) -> list[dict]:
    """data.go.kr 표준 응답 포장(response.body.items.item[])에서 item 리스트 추출."""
    body = payload.get("response", {}).get("body", {})
    items = body.get("items")
    if items is None:
        return []
    if isinstance(items, dict):
        item = items.get("item", [])
        return item if isinstance(item, list) else [item]
    return items if isinstance(items, list) else []


def _fetch_series(api_key: str, hs_code: str, start_yymm: str, end_yymm: str) -> list[tuple[str, float]]:
    """품목별 수출입실적 API → [(period 'YYYY-MM', 수출액), ...]."""
    params = {
        "serviceKey": api_key,
        "strtYymm": start_yymm,
        "endYymm": end_yymm,
        "hsSgn": hs_code,
        "type": "json",
        "numOfRows": 100,
    }
    try:
        resp = requests.get(CUSTOMS_BASE, params=params, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log.warning("관세청 API 호출 오류 hs_code=%s: %s", hs_code, exc)
        return []

    items = _parse_items(payload)
    out: list[tuple[str, float]] = []
    for it in items:
        period_raw = it.get("year") or it.get("yymm") or it.get("statYymm") or it.get("period")
        value_raw = it.get("expDlrAmt") or it.get("expDlr") or it.get("expAmt")
        if not period_raw or value_raw in (None, ""):
            log.warning("관세청 응답 필드명 불일치 — 항목 확인 필요: %s", list(it.keys()))
            continue
        p = str(period_raw)
        period = f"{p[:4]}-{p[4:6]}" if len(p) >= 6 else p
        try:
            out.append((period, float(value_raw)))
        except ValueError:
            continue
    return out


def run(con=None) -> None:
    api_key = os.environ.get("CUSTOMS_API_KEY", "").strip()
    if not api_key:
        log.warning("CUSTOMS_API_KEY 미설정 — customs_trade 스킵")
        return

    own = con is None
    if own:
        con = connect()

    try:
        taxonomy = _load_taxonomy()
        configured = [
            t for t in taxonomy
            if t.get("source") == "customs"
            and t.get("customs", {}).get("hs_code")
            and t.get("customs", {}).get("verified") is True
        ]
        if not configured:
            log.warning(
                "customs.verified=true 지표 없음 — hs_code는 1차 추정치이므로 "
                "docs/06 절차대로 검증 후 verified를 true로 바꿔야 수집됨"
            )
            return

        end = date.today().strftime("%Y%m")
        start = f"{date.today().year - 3}01"

        total = 0
        for t in configured:
            cst = t["customs"]
            series = _fetch_series(api_key, cst["hs_code"], start, end)
            time.sleep(DELAY_SEC)
            for period, value in series:
                con.execute(
                    """INSERT OR REPLACE INTO industry_indicators
                       (indicator_id, level2_id, period, value) VALUES(?,?,?,?)""",
                    (t["id"], t["level2_id"], period, value),
                )
                total += 1
            if series:
                log.info("적재 [%s/%s] %d개월", t["level2_id"], t["id"], len(series))

        con.commit()
        log.info("customs_trade 완료 — %d행 적재", total)
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
