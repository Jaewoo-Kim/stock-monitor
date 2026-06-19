"""KRX 전체 상장종목 -> WICS -> level2_id 매핑 -> companies 테이블 적재.

흐름:
  1. pykrx로 KOSPI/KOSDAQ 전체 종목 목록 + 종목명 수집
  2. WICS 세분류(6자리) 인덱스를 순회해 ticker -> wics_code 역매핑 구성
     (6자리 미지원 코드는 스킵, 4자리 fallback 처리)
  3. wics_code -> level2_id 변환 (WICS_TO_LEVEL2)
  4. mapping_overrides 테이블 오버라이드 적용 (2차전지 등)
  5. companies 테이블 INSERT OR IGNORE (is_representative=1 시드 보존)

실행:
  python src/collectors/company_mapper.py            # 오늘 날짜 기준
  python src/collectors/company_mapper.py 20260601   # 특정일 기준
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from db import connect  # noqa: E402

try:
    from pykrx import stock as krx
except ImportError:
    print("[ERROR] pykrx 미설치: pip install pykrx")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# WICS 6자리 세분류 -> level2_id 매핑
# KRX WICS 세분류 코드 기준 (get_index_portfolio_deposit_file 지원)
# ──────────────────────────────────────────────────────────────
WICS_TO_LEVEL2: dict[str, str] = {
    # ── 에너지 (S9) ──
    "G101010": "S9_energy",
    "G101020": "S9_energy",
    # ── 소재 (S8) ──
    "G151010": "S8_chemicals",
    "G151020": "S7_construction",  # 건설자재 -> 건설·건자재
    "G151030": "S8_chemicals",
    "G151040": "S8_steel",
    "G151050": "S8_chemicals",
    # ── 산업재 (S7) ──
    "G201010": "S7_defense",       # 항공우주와국방
    "G201020": "S7_construction",  # 건물제품
    "G201030": "S7_construction",  # 건설및엔지니어링
    "G201040": "S7_power",         # 전기장비 -> 전력기기·전선
    "G201050": "S7_holding",       # 복합기업
    "G201060": "S7_machinery",     # 기계
    "G201070": "S7_holding",       # 무역회사와유통업자
    "G201080": "S7_ship",          # 조선 (핵심)
    "G202010": "S7_holding",       # 상업서비스와공급품
    "G202020": "S7_holding",
    "G203010": "S7_transport",     # 항공화물과물류
    "G203020": "S7_transport",     # 해운
    "G203030": "S7_transport",     # 도로와철도운송
    "G203040": "S7_transport",     # 운송인프라
    "G203050": "S7_transport",     # 항공사
    # ── 경기소비재 (S3) ──
    "G251010": "S3_auto",
    "G251020": "S3_auto",
    "G252010": "S3_clothing",
    "G252020": "S3_clothing",
    "G252030": "S3_clothing",
    "G253010": "S3_leisure",
    "G253020": "S3_leisure",
    "G255010": "S3_retail",
    "G255020": "S3_retail",
    "G256010": "S2_entertainment", # 미디어
    "G256020": "S2_entertainment", # 엔터테인먼트 (게임 오버라이드로 분리)
    "G256030": "S2_internet",      # 인터랙티브미디어와서비스
    # ── 필수소비재 (S4) ──
    "G301010": "S4_food",
    "G302010": "S4_food",
    "G302020": "S4_food",
    "G303010": "S4_food",
    "G303020": "S3_cosmetics",     # 개인용품 -> 화장품 (대다수가 화장품)
    # ── 건강관리 (S5) ──
    "G351010": "S5_medtech",
    "G351020": "S5_medtech",
    "G351030": "S5_medtech",
    "G352010": "S5_pharma",
    "G352020": "S5_pharma",
    # ── 금융 (S6) ──
    "G401010": "S6_bank",
    "G402010": "S6_bank",
    "G402020": "S6_securities",
    "G402030": "S6_securities",
    "G403010": "S6_insurance",
    "G404010": "S6_reits",
    "G404020": "S6_reits",
    # ── IT·테크 (S1) ──
    "G451010": "S1_software",
    "G451020": "S1_software",
    "G451030": "S1_software",      # 인터넷소프트웨어 (NAVER/카카오 오버라이드)
    "G452010": "S1_hardware",
    "G452020": "S1_hardware",
    "G453010": "S1_semilarge",     # 반도체 (소부장 오버라이드 후 분리)
    # ── 커뮤니케이션서비스 (S2) ──
    "G501010": "S2_telecom",
    "G501020": "S2_telecom",
    # ── 유틸리티 (S10) ──
    "G551010": "S10_utility",
    "G551020": "S10_utility",
    "G551030": "S10_utility",
    "G551040": "S10_utility",
    "G551050": "S10_utility",
}

# 4자리 fallback (6자리 미지원 시)
WICS_4D_TO_LEVEL2: dict[str, str] = {
    "G1010": "S9_energy",
    "G1510": "S8_chemicals",
    "G2010": "S7_holding",       # 자본재 전체 -> 지주 (오버라이드로 세분)
    "G2020": "S7_holding",
    "G2030": "S7_transport",
    "G2510": "S3_auto",
    "G2520": "S3_clothing",
    "G2530": "S3_leisure",
    "G2550": "S3_retail",
    "G2560": "S2_entertainment",
    "G3010": "S4_food",
    "G3020": "S4_food",
    "G3030": "S3_cosmetics",
    "G3510": "S5_medtech",
    "G3520": "S5_pharma",
    "G4010": "S6_bank",
    "G4020": "S6_securities",
    "G4030": "S6_insurance",
    "G4040": "S6_reits",
    "G4510": "S1_software",
    "G4520": "S1_hardware",
    "G4530": "S1_semilarge",
    "G5010": "S2_telecom",
    "G5510": "S10_utility",
}


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _fetch_wics_mapping(date: str) -> dict[str, tuple[str, str]]:
    """ticker -> (wics_code, level2_id) 역매핑 구성.

    6자리 코드를 먼저 시도, 실패하면 4자리 fallback.
    동일 ticker가 복수 WICS에 속할 경우 첫 번째 hit(더 세분된 코드) 우선.
    """
    mapping: dict[str, tuple[str, str]] = {}

    # 6자리 코드 우선
    for code, level2_id in WICS_TO_LEVEL2.items():
        try:
            df = krx.get_index_portfolio_deposit_file(date, code)
            if df is None or df.empty:
                continue
            for ticker in df.index:
                if ticker not in mapping:
                    mapping[ticker] = (code, level2_id)
        except Exception as exc:
            log.debug("WICS %s 스킵: %s", code, exc)

    # 4자리 fallback (6자리에서 매핑 안 된 ticker)
    missed = 0
    for code, level2_id in WICS_4D_TO_LEVEL2.items():
        try:
            df = krx.get_index_portfolio_deposit_file(date, code)
            if df is None or df.empty:
                continue
            for ticker in df.index:
                if ticker not in mapping:
                    mapping[ticker] = (code, level2_id)
                    missed += 1
        except Exception as exc:
            log.debug("WICS4 %s 스킵: %s", code, exc)

    log.info("WICS 역매핑 완료: %d 종목 (fallback %d)", len(mapping), missed)
    return mapping


def _fetch_all_tickers(date: str) -> dict[str, tuple[str, str]]:
    """ticker -> (name, market)"""
    result: dict[str, tuple[str, str]] = {}
    for market in ("KOSPI", "KOSDAQ"):
        try:
            tickers = krx.get_market_ticker_list(date, market=market)
            for t in tickers:
                name = krx.get_market_ticker_name(t)
                result[t] = (name, market)
            log.info("%s: %d 종목", market, len(tickers))
        except Exception as exc:
            log.error("%s 종목 목록 오류: %s", market, exc)
    return result


def _confidence(wics_code: str) -> float:
    """WICS 코드 길이에 따른 신뢰도: 6자리=0.85, 4자리=0.60"""
    return 0.85 if len(wics_code) == 7 else 0.60  # 'G' + 6digits = 7chars


def run(date: str | None = None) -> None:
    ref_date = date or _today()
    log.info("기준일: %s", ref_date)

    all_tickers = _fetch_all_tickers(ref_date)
    wics_map = _fetch_wics_mapping(ref_date)

    con = connect()
    try:
        cur = con.cursor()

        # ── 1. 오버라이드 목록 로드 ──
        overrides: dict[str, str] = {
            row[0]: row[1]
            for row in cur.execute("SELECT ticker, level2_id FROM mapping_overrides")
        }
        log.info("오버라이드 %d건 로드", len(overrides))

        # ── 2. companies 적재 (INSERT OR IGNORE: 시드 데이터 보존) ──
        inserted = 0
        for ticker, (name, market) in all_tickers.items():
            if ticker in overrides:
                level2_id = overrides[ticker]
                confidence = 0.95
            elif ticker in wics_map:
                wics_code, level2_id = wics_map[ticker]
                confidence = _confidence(wics_code)
            else:
                level2_id = None
                confidence = 0.0

            cur.execute(
                """INSERT OR IGNORE INTO companies
                   (ticker, name, market, level2_id, mapping_confidence, is_representative)
                   VALUES(?, ?, ?, ?, ?, 0)""",
                (ticker, name, market, level2_id, confidence),
            )
            inserted += cur.rowcount

        # ── 3. 오버라이드를 기존 행에도 반영 (UPDATE — 시드 is_representative 무관하게) ──
        for ticker, level2_id in overrides.items():
            cur.execute(
                "UPDATE companies SET level2_id=?, mapping_confidence=0.95 WHERE ticker=?",
                (level2_id, ticker),
            )

        con.commit()

        total = cur.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        unmapped = cur.execute(
            "SELECT COUNT(*) FROM companies WHERE level2_id IS NULL"
        ).fetchone()[0]
        log.info(
            "INSERT %d, companies 합계 %d (미매핑 %d, %.1f%%)",
            inserted, total, unmapped, unmapped / total * 100 if total else 0,
        )
    finally:
        con.close()


if __name__ == "__main__":
    arg_date = sys.argv[1] if len(sys.argv) > 1 else None
    run(arg_date)
