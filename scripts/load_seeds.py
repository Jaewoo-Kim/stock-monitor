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


def main() -> None:
    reset = "--reset" in sys.argv
    if reset:
        init_db()
        print("DB 재초기화 완료")

    con = connect()
    try:
        cur = con.cursor()
        steps = [
            ("sectors",             load_sectors),
            ("industries",          load_industries),
            ("industry_etfs",       load_industry_etfs),
            ("representative_companies", load_companies),
            ("analysts",            load_analysts),
            ("award_seeds",         load_award_seeds),
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
