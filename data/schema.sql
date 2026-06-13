-- stock-monitor DB 스키마 v1.0
-- 실행: sqlite3 data/monitor.db < data/schema.sql

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────
-- 1. 산업 분류 (docs/01)
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sectors (
    level1_id   TEXT PRIMARY KEY,   -- 예: S1
    level1_name TEXT NOT NULL        -- 예: IT·테크
);

CREATE TABLE IF NOT EXISTS industries (
    level2_id            TEXT PRIMARY KEY,   -- 예: S1_반도체대형
    level1_id            TEXT NOT NULL REFERENCES sectors(level1_id),
    level2_name          TEXT NOT NULL,       -- 예: 반도체 (대형)
    coverage_density     INTEGER NOT NULL CHECK(coverage_density IN (1,2,3)),
                         -- 1=★ 월수건, 2=★★ 주1~수건, 3=★★★ 주간다수
    signal_window_weeks  INTEGER NOT NULL,   -- 4(★★★) or 8(★★) or NULL→보류(★)
    is_signal_eligible   INTEGER NOT NULL DEFAULT 1  -- 0=데이터부족 상시보류(★ 섹터 등)
);

-- 종목 마스터
CREATE TABLE IF NOT EXISTS companies (
    ticker               TEXT PRIMARY KEY,   -- 6자리 종목코드 (보통주)
    name                 TEXT NOT NULL,
    market               TEXT NOT NULL CHECK(market IN ('KOSPI','KOSDAQ','KONEX','ETF','기타')),
    level2_id            TEXT REFERENCES industries(level2_id),
    mapping_confidence   REAL NOT NULL DEFAULT 1.0,  -- 0~1, 낮을수록 수동 보정 필요
    is_representative    INTEGER NOT NULL DEFAULT 0,
    is_etf               INTEGER NOT NULL DEFAULT 0,
    is_preferred         INTEGER NOT NULL DEFAULT 0, -- 우선주 플래그
    last_updated         TEXT NOT NULL DEFAULT (date('now'))
);

-- 2차전지 등 WICS 자동 매핑 오버라이드 목록
CREATE TABLE IF NOT EXISTS mapping_overrides (
    ticker     TEXT PRIMARY KEY REFERENCES companies(ticker),
    level2_id  TEXT NOT NULL REFERENCES industries(level2_id),
    reason     TEXT NOT NULL  -- 예: '2차전지 통합 정의 — LG에너지솔루션 셀'
);

-- 리포트 업종 표기 정규화 alias (종목 미특정 산업 리포트용)
CREATE TABLE IF NOT EXISTS report_sector_alias (
    raw_label  TEXT PRIMARY KEY,   -- 크롤링 원문 그대로
    level2_id  TEXT NOT NULL REFERENCES industries(level2_id)
);

-- 산업별 대표 ETF
CREATE TABLE IF NOT EXISTS industry_etfs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level2_id  TEXT NOT NULL REFERENCES industries(level2_id),
    etf_ticker TEXT NOT NULL,
    etf_name   TEXT NOT NULL,
    UNIQUE(level2_id, etf_ticker)
);

-- 테마 태그 (다대다 — AI, 로봇, 원전 등)
CREATE TABLE IF NOT EXISTS theme_tags (
    tag_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS company_themes (
    ticker  TEXT NOT NULL REFERENCES companies(ticker),
    tag_id  INTEGER NOT NULL REFERENCES theme_tags(tag_id),
    PRIMARY KEY(ticker, tag_id)
);

-- ─────────────────────────────────────────
-- 2. 애널리스트 트래킹 (docs/02)
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS analysts (
    analyst_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    broker         TEXT NOT NULL,   -- 증권사명
    active         INTEGER NOT NULL DEFAULT 1,
    predecessor_id INTEGER REFERENCES analysts(analyst_id),  -- 이직 전 레코드
    UNIQUE(name, broker)
);

-- 산업별 Top 3 현황
CREATE TABLE IF NOT EXISTS analyst_industry_rank (
    level2_id       TEXT NOT NULL REFERENCES industries(level2_id),
    analyst_id      INTEGER NOT NULL REFERENCES analysts(analyst_id),
    rank            INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 3),
    score           REAL,                    -- NULL = seed 단계
    method          TEXT NOT NULL CHECK(method IN ('seed_award','auto_accuracy')),
    effective_from  TEXT NOT NULL DEFAULT (date('now')),
    PRIMARY KEY(level2_id, analyst_id)
);

-- 어워드 시드 정보
CREATE TABLE IF NOT EXISTS award_seeds (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    analyst_id        INTEGER NOT NULL REFERENCES analysts(analyst_id),
    award_name        TEXT NOT NULL,   -- 예: 매경베스트애널리스트
    award_sector_raw  TEXT NOT NULL,   -- 어워드 원문 부문명
    level2_id         TEXT NOT NULL REFERENCES industries(level2_id),
    year_half         TEXT NOT NULL    -- 예: 2025H2
);

-- 자체 적중률 스코어 누적
CREATE TABLE IF NOT EXISTS analyst_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    analyst_id      INTEGER NOT NULL REFERENCES analysts(analyst_id),
    level2_id       TEXT NOT NULL REFERENCES industries(level2_id),
    period_end      TEXT NOT NULL,   -- 계산 기준일 (YYYY-MM-DD)
    hit_rate_6m     REAL,            -- 6개월 적중률 (0~1)
    hit_rate_12m    REAL,            -- 12개월 적중률
    direction_rate  REAL,            -- 방향 적중률
    n_reports       INTEGER NOT NULL DEFAULT 0,
    weighted_score  REAL,
    UNIQUE(analyst_id, level2_id, period_end)
);

-- ─────────────────────────────────────────
-- 3. 리포트 이벤트 (docs/02 + CLAUDE.md 원칙 7)
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS report_events (
    report_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    published_date  TEXT NOT NULL,   -- YYYY-MM-DD
    broker          TEXT NOT NULL,   -- 증권사명
    -- 저자 (제1저자 analyst_id + raw 문자열 병기)
    analyst_id      INTEGER REFERENCES analysts(analyst_id),
    authors_raw     TEXT,            -- JSON 배열 '["홍길동","김철수"]'
    -- 대상 (종목 or 산업)
    ticker          TEXT REFERENCES companies(ticker),
    level2_id       TEXT REFERENCES industries(level2_id),  -- 종목 없는 산업 리포트
    title           TEXT NOT NULL,
    opinion         TEXT,            -- 매수/중립/매도 등
    target_price    REAL,            -- 목표주가
    prev_target     REAL,            -- 직전 목표주가 (변경 산출용)
    close_on_date   REAL,            -- 발행일 종가 (적중률 계산용)
    -- 링크 (CLAUDE.md 원칙 7: 2종 필수)
    source_url      TEXT NOT NULL,   -- 한경/네이버 상세 링크 (항상 존재)
    broker_url      TEXT,            -- 증권사 리서치센터 링크 (확보 시에만)
    -- PDF / 요약
    pdf_available   INTEGER NOT NULL DEFAULT 0,
    pdf_path        TEXT,            -- 로컬 경로 (개인용 보관)
    has_summary     INTEGER NOT NULL DEFAULT 0,
    summary_text    TEXT,            -- LLM 요약 (Phase 3 이후)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_report_date       ON report_events(published_date);
CREATE INDEX IF NOT EXISTS idx_report_level2     ON report_events(level2_id);
CREATE INDEX IF NOT EXISTS idx_report_ticker     ON report_events(ticker);
CREATE INDEX IF NOT EXISTS idx_report_analyst    ON report_events(analyst_id);

-- ─────────────────────────────────────────
-- 4. 시세 (docs/00 5.1)
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS price_history (
    ticker  TEXT NOT NULL REFERENCES companies(ticker),
    date    TEXT NOT NULL,   -- YYYY-MM-DD
    close   REAL NOT NULL,
    volume  INTEGER,
    PRIMARY KEY(ticker, date)
);

CREATE TABLE IF NOT EXISTS industry_index_history (
    level2_id  TEXT NOT NULL REFERENCES industries(level2_id),
    date       TEXT NOT NULL,
    close      REAL NOT NULL,
    PRIMARY KEY(level2_id, date)
);
