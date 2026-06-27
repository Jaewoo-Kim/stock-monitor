"""한국은행 ECOS 업황 동인 수집기 (L0).

data/seed/industry_indicators.json 의 taxonomy를 읽어, ECOS 코드가 채워진
지표만 StatisticSearch API로 월간 시계열을 받아 industry_indicators에 적재.

환경변수:
    ECOS_API_KEY  — ecos.bok.or.kr/api 무료 인증키 (없으면 skip)

특성:
  - 코드(stat_code/item_code) 미입력 지표는 skip → 키 발급 후 코드 검증·확정.
  - graceful: 호출 실패해도 다음 실행 재시도.

실행:
    python src/collectors/ecos_collector.py
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

ECOS_BASE = "https://ecos.bok.or.kr/api/StatisticSearch"
DELAY_SEC = 0.4


def _load_taxonomy() -> list[dict]:
    path = ROOT / "data" / "seed" / "industry_indicators.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("indicators", [])


def _fetch_series(api_key: str, stat_code: str, item_code: str,
                  cycle: str, start: str, end: str) -> list[tuple[str, float]]:
    """ECOS StatisticSearch → [(period 'YYYY-MM', value), ...]."""
    item = item_code or "?"
    url = f"{ECOS_BASE}/{api_key}/json/kr/1/1000/{stat_code}/{cycle}/{start}/{end}/{item}"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("ECOS 호출 오류 stat=%s item=%s: %s", stat_code, item_code, exc)
        return []

    rows = data.get("StatisticSearch", {}).get("row", [])
    out: list[tuple[str, float]] = []
    for r in rows:
        t = r.get("TIME", "")            # 'YYYYMM'
        v = r.get("DATA_VALUE")
        if not t or v in (None, ""):
            continue
        period = f"{t[:4]}-{t[4:6]}" if len(t) >= 6 else t
        try:
            out.append((period, float(v)))
        except ValueError:
            continue
    return out


def run(con=None) -> None:
    api_key = os.environ.get("ECOS_API_KEY", "").strip()
    if not api_key:
        log.warning("ECOS_API_KEY 미설정 — ecos_collector 스킵")
        return

    own = con is None
    if own:
        con = connect()

    try:
        taxonomy = _load_taxonomy()
        # 최근 3년 월간
        end = date.today().strftime("%Y%m")
        start = f"{date.today().year - 3}01"

        configured = [t for t in taxonomy
                      if t.get("source") == "ecos" and t.get("ecos", {}).get("stat_code")]
        if not configured:
            log.warning("ECOS 코드가 채워진 지표 없음 — taxonomy의 ecos.stat_code 확정 필요")
            return

        total = 0
        for t in configured:
            ec = t["ecos"]
            series = _fetch_series(api_key, ec["stat_code"], ec.get("item_code", ""),
                                   ec.get("cycle", "M"), start, end)
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
        log.info("ecos_collector 완료 — %d행 적재", total)
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
