"""SQLite 연결 + 스키마 초기화 헬퍼.

수집기/시그널 엔진은 `from src.db import connect` 로 DB 핸들을 얻는다.
직접 실행하면(`python src/db.py`) 스키마로 빈 DB를 생성한다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "monitor.db"
SCHEMA_PATH = ROOT / "data" / "schema.sql"


def connect() -> sqlite3.Connection:
    """외래키가 켜진 monitor.db 연결을 반환."""
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    return con


# 기존 테이블에 나중에 추가된 컬럼 (CREATE IF NOT EXISTS로는 반영 안 됨 → ALTER)
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "stock_scores": {
        "upside_pct":  "REAL",
        "n_targets":   "INTEGER",
        "share_bonus": "REAL",
        "buy_score":   "REAL",
    },
}


def _migrate_columns(con) -> None:
    """누락 컬럼을 ALTER TABLE ADD COLUMN으로 멱등 추가."""
    for table, cols in _COLUMN_MIGRATIONS.items():
        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            continue
        have = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        for col, col_type in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def init_db() -> None:
    """schema.sql을 실행해 테이블을 생성(IF NOT EXISTS, 멱등) + 컬럼 마이그레이션."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    con = connect()
    try:
        con.executescript(sql)
        _migrate_columns(con)
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    init_db()
    con = connect()
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
    finally:
        con.close()
    print(f"{DB_PATH} 생성 완료 - 테이블 {len(tables)}개")
    for t in tables:
        print(f"  - {t}")
