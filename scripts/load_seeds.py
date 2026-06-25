"""시드 CSV → SQLite 적재.

실행:
    python scripts/load_seeds.py            # 전체 적재
    python scripts/load_seeds.py --reset    # DB 재초기화 후 적재
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from db import connect, init_db  # noqa: E402

SEED = ROOT / "data" / "seed"


def load_sectors(cur) -> int:
    path = SEED / "sectors.csv"
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    cur.executemany(
        "INSERT OR IGNORE INTO sectors(level1_id, level1_name) VALUES(:level1_id, :level1_name)",
        rows,
    )
    return len(rows)


def load_industries(cur) -> int:
    path = SEED / "industries.csv"
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    for r in rows:
        r["coverage_density"] = int(r["coverage_density"])
        r["signal_window_weeks"] = int(r["signal_window_weeks"])
        r["is_signal_eligible"] = int(r["is_signal_eligible"])
    cur.executemany(
        """INSERT OR IGNORE INTO industries
           (level2_id, level1_id, level2_name, coverage_density,
            signal_window_weeks, is_signal_eligible)
           VALUES(:level2_id, :level1_id, :level2_name, :coverage_density,
                  :signal_window_weeks, :is_signal_eligible)""",
        rows,
    )
    return len(rows)


def load_industry_etfs(cur) -> int:
    path = SEED / "industry_etfs.csv"
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    cur.executemany(
        """INSERT OR IGNORE INTO industry_etfs(level2_id, etf_ticker, etf_name)
           VALUES(:level2_id, :etf_ticker, :etf_name)""",
        rows,
    )
    return len(rows)


def load_companies(cur) -> int:
    path = SEED / "representative_companies.csv"
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    for r in rows:
        r["mapping_confidence"] = float(r["mapping_confidence"])
        r["is_representative"] = int(r["is_representative"])
        r.setdefault("is_etf", 0)
        r.setdefault("is_preferred", 0)
    cur.executemany(
        """INSERT OR IGNORE INTO companies
           (ticker, name, market, level2_id, mapping_confidence, is_representative)
           VALUES(:ticker, :name, :market, :level2_id, :mapping_confidence, :is_representative)""",
        rows,
    )
    return len(rows)


def load_analysts(cur) -> int:
    path = SEED / "analysts.csv"
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    for r in rows:
        r["active"] = int(r["active"])
    cur.executemany(
        "INSERT OR IGNORE INTO analysts(name, broker, active) VALUES(:name, :broker, :active)",
        rows,
    )
    return len(rows)


def load_award_seeds(cur) -> int:
    path = SEED / "award_seeds.csv"
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    inserted = 0
    for r in rows:
        cur.execute(
            "SELECT analyst_id FROM analysts WHERE name=? AND broker=?",
            (r["analyst_name"], r["analyst_broker"]),
        )
        row = cur.fetchone()
        if row is None:
            print(f"  [WARN] 애널리스트 미등록: {r['analyst_name']} ({r['analyst_broker']}) — 건너뜀")
            continue
        analyst_id = row[0]
        cur.execute(
            """INSERT OR IGNORE INTO award_seeds
               (analyst_id, award_name, award_sector_raw, level2_id, year_half)
               VALUES(?, ?, ?, ?, ?)""",
            (analyst_id, r["award_name"], r["award_sector_raw"], r["level2_id"], r["year_half"]),
        )
        inserted += 1
    return inserted


def load_market_share(cur) -> int:
    """글로벌 시장 점유율·순위 시드 적재. FK 미충족(ticker 없음) 시 스킵."""
    import sqlite3 as _sqlite3

    path = SEED / "market_share.csv"
    if not path.exists():
        return 0
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    inserted = skipped = 0
    for r in rows:
        share = r.get("global_share", "").strip()
        rank  = r.get("global_rank", "").strip()
        try:
            cur.execute(
                """INSERT OR REPLACE INTO company_market_share
                   (ticker, segment, global_share, global_rank, as_of, source, note)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["ticker"], r["segment"],
                    float(share) if share else None,
                    int(rank) if rank else None,
                    r["as_of"], r["source"], r.get("note", "") or None,
                ),
            )
            inserted += 1
        except _sqlite3.IntegrityError:
            skipped += 1
    if skipped:
        print(f"  [INFO] market_share: {skipped}건 FK 미충족 스킵")
    return inserted


def load_company_briefs(cur) -> int:
    """기업 브리프 시드(JSON) → company_briefs. FK 미충족 시 스킵."""
    import json as _json
    import sqlite3 as _sqlite3

    path = SEED / "company_briefs.json"
    if not path.exists():
        return 0
    data = _json.loads(path.read_text(encoding="utf-8"))
    inserted = skipped = 0
    for ticker, b in data.items():
        risks = b.get("risks")
        risk_json = _json.dumps(risks, ensure_ascii=False) if risks else None
        try:
            cur.execute(
                """INSERT OR REPLACE INTO company_briefs
                   (ticker, business_summary, risk_summary, moat_summary, source, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    ticker, b.get("business"), risk_json, b.get("moat"),
                    b.get("source"), b.get("updated"),
                ),
            )
            inserted += 1
        except _sqlite3.IntegrityError:
            skipped += 1
    if skipped:
        print(f"  [INFO] company_briefs: {skipped}건 FK 미충족 스킵")
    return inserted


def load_mapping_overrides(cur) -> int:
    import sqlite3 as _sqlite3

    path = SEED / "mapping_overrides.csv"
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    inserted = skipped = 0
    for r in rows:
        try:
            cur.execute(
                """INSERT OR REPLACE INTO mapping_overrides(ticker, level2_id, reason)
                   VALUES(:ticker, :level2_id, :reason)""",
                r,
            )
            inserted += 1
        except _sqlite3.IntegrityError:
            # ticker가 companies에 아직 없음 (company_mapper 실행 후 자동 반영)
            skipped += 1
    if skipped:
        print(f"  [INFO] mapping_overrides: {skipped}건 FK 미충족 스킵 (company_mapper 후 반영)")
    return inserted


_SEED_TABLES = [
    "award_seeds", "analyst_industry_rank", "analyst_scores",
    "mapping_overrides", "company_themes", "report_events",
    "industry_etfs", "company_fundamentals", "company_market_share",
    "company_briefs", "price_history", "industry_index_history", "companies",
    "analysts", "industries", "sectors", "theme_tags",
]


def main() -> None:
    reset = "--reset" in sys.argv
    if reset:
        init_db()
        con_r = connect()
        try:
            for tbl in _SEED_TABLES:
                con_r.execute(f"DELETE FROM {tbl}")
            con_r.commit()
        finally:
            con_r.close()
        print("DB 재초기화 완료")

    con = connect()
    try:
        cur = con.cursor()
        steps = [
            ("sectors",              load_sectors),
            ("industries",           load_industries),
            ("industry_etfs",        load_industry_etfs),
            ("representative_companies", load_companies),
            ("analysts",             load_analysts),
            ("award_seeds",          load_award_seeds),
            ("mapping_overrides",    load_mapping_overrides),
            ("market_share",         load_market_share),
            ("company_briefs",       load_company_briefs),
        ]
        for name, fn in steps:
            n = fn(cur)
            print(f"  {name}: {n}건 적재")
        con.commit()
        print("완료 - data/monitor.db 업데이트")
    finally:
        con.close()


if __name__ == "__main__":
    main()
