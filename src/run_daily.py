"""일일 배치 오케스트레이터.

실행 순서:
  1. company_mapper  — KRX 전종목 → WICS → companies 적재 (월요일만)
  2. naver_research  — 네이버 리서치 최신 3페이지 수집
  3. price_collector — 전일 종가 수집

GitHub Actions 에서 평일 KST 07:00 에 자동 실행.
로컬 수동 실행:
  python src/run_daily.py
  python src/run_daily.py --all-steps   # 요일 무관 company_mapper 포함
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("run_daily")


def _yesterday_yyyymmdd() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y%m%d")


def _since_date_str() -> str:
    """리서치 수집 기준일: 3일 전 (주말 포함 버퍼)."""
    return (date.today() - timedelta(days=3)).strftime("%Y-%m-%d")


def step_company_mapper() -> bool:
    log.info("=== [1/3] company_mapper 시작 ===")
    try:
        from collectors.company_mapper import run
        run()
        log.info("=== [1/3] company_mapper 완료 ===")
        return True
    except Exception as exc:
        log.error("company_mapper 실패: %s", exc, exc_info=True)
        return False


def step_naver_research() -> bool:
    log.info("=== [2/3] naver_research 시작 ===")
    try:
        from collectors.naver_research import run
        run(max_pages=3, since_date=_since_date_str())
        log.info("=== [2/3] naver_research 완료 ===")
        return True
    except Exception as exc:
        log.error("naver_research 실패: %s", exc, exc_info=True)
        return False


def step_consensus() -> bool:
    log.info("=== [2.5/6] naver_consensus(EPS 추정) 시작 ===")
    try:
        from collectors.naver_consensus import run
        run()
        log.info("=== [2.5/6] naver_consensus 완료 ===")
        return True
    except Exception as exc:
        log.error("naver_consensus 실패: %s", exc, exc_info=True)
        return False


def step_price_collector() -> bool:
    log.info("=== [3/5] price_collector 시작 ===")
    try:
        from collectors.price_collector import run
        run(fromdate=_yesterday_yyyymmdd())
        log.info("=== [3/5] price_collector 완료 ===")
        return True
    except Exception as exc:
        log.error("price_collector 실패: %s", exc, exc_info=True)
        return False


def step_dart() -> bool:
    log.info("=== [4/6] dart_collector 시작 ===")
    try:
        from collectors.dart_collector import run
        run()
        log.info("=== [4/6] dart_collector 완료 ===")
        return True
    except Exception as exc:
        log.error("dart_collector 실패: %s", exc, exc_info=True)
        return False


def step_ecos() -> bool:
    log.info("=== [4.6/6] ecos(L0 업황 동인) 시작 ===")
    try:
        from collectors.ecos_collector import run
        run()
        log.info("=== [4.6/6] ecos 완료 ===")
        return True
    except Exception as exc:
        log.error("ecos 실패: %s", exc, exc_info=True)
        return False


def step_customs_trade() -> bool:
    log.info("=== [4.7/6] customs_trade(L0 수출 지표) 시작 ===")
    try:
        from collectors.customs_trade import run
        run()
        log.info("=== [4.7/6] customs_trade 완료 ===")
        return True
    except Exception as exc:
        log.error("customs_trade 실패: %s", exc, exc_info=True)
        return False


def step_dart_calendar() -> bool:
    log.info("=== [4.8/6] dart_calendar(IR·배당 캘린더) 시작 ===")
    try:
        from collectors.dart_calendar import run
        run()
        log.info("=== [4.8/6] dart_calendar 완료 ===")
        return True
    except Exception as exc:
        log.error("dart_calendar 실패: %s", exc, exc_info=True)
        return False


def step_dart_business() -> bool:
    log.info("=== [4.5/6] dart_business(사업보고서 발췌) 시작 ===")
    try:
        from collectors.dart_business import run
        run()
        log.info("=== [4.5/6] dart_business 완료 ===")
        return True
    except Exception as exc:
        log.error("dart_business 실패: %s", exc, exc_info=True)
        return False


def step_signals() -> bool:
    log.info("=== [5/6] signal_engine 시작 ===")
    try:
        from signals.run_signals import run
        run()
        log.info("=== [5/6] signal_engine 완료 ===")
        return True
    except Exception as exc:
        log.error("signal_engine 실패: %s", exc, exc_info=True)
        return False


def step_stock_scorer() -> bool:
    log.info("=== [5.5/6] stock_scorer 시작 ===")
    try:
        from signals.stock_scorer import run
        run()
        log.info("=== [5.5/6] stock_scorer 완료 ===")
        return True
    except Exception as exc:
        log.error("stock_scorer 실패: %s", exc, exc_info=True)
        return False


def step_build_static() -> bool:
    log.info("=== [6/6] build_static 시작 ===")
    try:
        from build_static import run
        run()
        log.info("=== [6/6] build_static 완료 ===")
        return True
    except Exception as exc:
        log.error("build_static 실패: %s", exc, exc_info=True)
        return False


def step_backtest() -> bool:
    log.info("=== [6.5/6] backtest(신호 검증) 시작 ===")
    try:
        from analysis.backtest import run
        run()
        log.info("=== [6.5/6] backtest 완료 ===")
        return True
    except Exception as exc:
        log.error("backtest 실패: %s", exc, exc_info=True)
        return False


def step_notify() -> bool:
    log.info("=== [7/7] telegram 알림 시작 ===")
    try:
        from notify.telegram import run
        run()
        log.info("=== [7/7] telegram 알림 완료 ===")
        return True
    except Exception as exc:
        log.error("telegram 알림 실패: %s", exc, exc_info=True)
        return False


def main(run_mapper: bool = False) -> int:
    today = date.today()
    log.info("daily batch 시작 — %s", today.isoformat())

    results: dict[str, bool] = {}

    # company_mapper: 월요일(weekday=0)마다 or --all-steps 플래그
    if run_mapper or today.weekday() == 0:
        results["company_mapper"] = step_company_mapper()
    else:
        log.info("=== [1/6] company_mapper 스킵 (월요일 전용) ===")
        results["company_mapper"] = True

    results["naver_research"]  = step_naver_research()
    results["consensus"]       = step_consensus()     # 컨센서스 EPS 스냅샷 (L2 선행)
    results["price_collector"] = step_price_collector()
    results["dart"]            = step_dart()         # DART_API_KEY 없으면 내부 skip
    results["dart_business"]   = step_dart_business() # DART_API_KEY 없으면 내부 skip
    results["dart_calendar"]   = step_dart_calendar() # DART_API_KEY 없거나 watchlist 없으면 내부 skip
    results["ecos"]            = step_ecos()          # ECOS_API_KEY 없으면 내부 skip
    results["customs_trade"]   = step_customs_trade() # CUSTOMS_API_KEY 없거나 verified=false면 내부 skip
    results["signals"]         = step_signals()       # L0 + cycle + timing 포함
    results["stock_scorer"]    = step_stock_scorer()
    results["build_static"]    = step_build_static()
    results["backtest"]        = step_backtest()
    results["notify"]          = step_notify()    # TELEGRAM_BOT_TOKEN 없으면 내부 skip

    # 결과 요약
    log.info("─────────────────────────────────")
    failed = [k for k, v in results.items() if not v]
    if failed:
        log.error("실패 스텝: %s", ", ".join(failed))
        return 1
    log.info("모든 스텝 성공")
    return 0


if __name__ == "__main__":
    force_mapper = "--all-steps" in sys.argv
    sys.exit(main(run_mapper=force_mapper))
