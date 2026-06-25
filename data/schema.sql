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
    -- 선행 보강 신호용 (docs/04 관절②): EPS 추정치 리비전은 목표가 리비전을 선행
    eps_estimate    REAL,            -- 애널리스트 EPS 추정치 (당해/차년)
    prev_eps_est    REAL,            -- 직전 EPS 추정치 (리비전 산출용)
    eps_fwd_year    INTEGER,         -- 추정 대상 회계연도 (예: 2026)
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
-- 3.5 기업 재무 (docs/04 관절③: 섹터 내 펀더멘털 모멘텀 종목 선별)
--     실적(actual)은 DART에서, 컨센서스(consensus)는 추정치 집계에서 적재.
--     Phase 1은 스키마만 확보, 본격 적재는 Phase 2~3.
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS company_fundamentals (
    ticker        TEXT NOT NULL REFERENCES companies(ticker),
    period_end    TEXT NOT NULL,   -- 분기말 YYYY-MM-DD (예: 2025-09-30)
    revenue       REAL,
    op_income     REAL,
    op_margin     REAL,            -- op_income / revenue (영업이익률 추세용)
    net_income    REAL,
    eps_actual    REAL,            -- 실적 EPS (DART)
    eps_consensus REAL,            -- 컨센서스 EPS (리비전 추적용)
    source        TEXT NOT NULL CHECK(source IN ('DART','consensus')),
    PRIMARY KEY(ticker, period_end, source)
);

CREATE INDEX IF NOT EXISTS idx_fund_ticker ON company_fundamentals(ticker);

-- ─────────────────────────────────────────
-- 3.6 글로벌 시장 점유율·순위 (수동 시드 — 무료 자동소스 없음)
--     리서치/리포트에서 확인한 값만 출처·기준시점과 함께 적재.
--     LLM 추정 금지 (CLAUDE.md 원칙: 판단하지 않고 설명만 한다).
--     한 기업이 복수 세그먼트(예: 삼성전자 DRAM/NAND/파운드리)를 가질 수 있음.
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS company_market_share (
    ticker        TEXT NOT NULL REFERENCES companies(ticker),
    segment       TEXT NOT NULL,    -- 제품/세그먼트 (예: 'DRAM', 'EV 배터리', '소형 건설장비')
    global_share  REAL,             -- 글로벌 점유율 % (NULL 허용)
    global_rank   INTEGER,          -- 글로벌 순위 (NULL 허용)
    as_of         TEXT NOT NULL,    -- 기준 시점 (예: '2025Q1', '2024')
    source        TEXT NOT NULL,    -- 출처 (예: 'TrendForce', 'SNE Research', 'Clarksons')
    note          TEXT,             -- 비고
    PRIMARY KEY(ticker, segment, as_of)
);

CREATE INDEX IF NOT EXISTS idx_mshare_ticker ON company_market_share(ticker);

-- ─────────────────────────────────────────
-- 3.7 기업 브리프 (사업 개요 + 투자 리스크)
--     사업개요·리스크 텍스트는 1차자료(DART 사업보고서)·애널리스트 리포트를
--     근거로 생성·캐싱. 애널리스트 의견/펀더멘털/점유율/타이밍은 빌드 시 실시간 집계.
--     원칙: LLM은 판단(매수/매도)하지 않고 설명만. 추천은 컨센서스+퀀트로 제시.
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS company_briefs (
    ticker           TEXT PRIMARY KEY REFERENCES companies(ticker),
    business_summary TEXT,    -- 사업 개요 (무엇을 하는 회사인가)
    risk_summary     TEXT,    -- 투자 리스크 (JSON 배열 문자열: ["...","..."])
    moat_summary     TEXT,    -- 핵심 경쟁력/해자 (선택)
    source           TEXT,    -- 근거 출처 표기 (예: 'DART 사업보고서·애널리스트 리포트')
    updated_at       TEXT     -- 생성/갱신 시점 (YYYY-MM)
);

-- DART 사업보고서 원문 발췌 (1차자료 — 사업의 개요 / 위험)
CREATE TABLE IF NOT EXISTS company_disclosures (
    ticker       TEXT PRIMARY KEY REFERENCES companies(ticker),
    rcept_no     TEXT,    -- DART 접수번호 (원문 링크 구성용)
    report_nm    TEXT,    -- 보고서명 (예: '사업보고서 (2025.12)')
    rcept_dt     TEXT,    -- 접수일 YYYYMMDD
    biz_overview TEXT,    -- '사업의 개요' 발췌
    risk_text    TEXT,    -- '위험요소' 발췌 (있을 때)
    fetched_at   TEXT     -- 수집 시점 (YYYY-MM-DD)
);

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

-- ─────────────────────────────────────────
-- 5. 신호 엔진 산출물 (Phase 2)
-- ─────────────────────────────────────────

-- 산업별 주간 신호 스냅샷
CREATE TABLE IF NOT EXISTS cycle_signals (
    level2_id        TEXT NOT NULL REFERENCES industries(level2_id),
    calc_date        TEXT NOT NULL,   -- 계산 기준일 YYYY-MM-DD (보통 일요일)
    window_weeks     INTEGER NOT NULL, -- 신호 윈도우 (4 or 8주)

    -- 확인 레이어 (목표가 리비전 기반)
    conf_breadth     REAL,  -- 상향 비율 (-1~1): (상향-하향) / 전체
    conf_magnitude   REAL,  -- 상향 평균 % (상향 리포트만)
    conf_upside_gap  INTEGER, -- 업종지수 N주 최고가 돌파 (0/1)

    -- 선행 레이어 (EPS 리비전 기반 — 데이터 부족 시 NULL)
    lead_breadth     REAL,  -- EPS 상향 비율
    lead_magnitude   REAL,  -- EPS 상향 평균 %
    lead_accel       REAL,  -- 리비전 가속도 (Δ breadth)
    lead_first_turn  INTEGER, -- 최초 양전환 플래그 (0/1)

    -- 종합 점수 & 사이클 상태
    composite_score  REAL,   -- -1~1, 높을수록 전환·확장
    cycle_phase      TEXT CHECK(cycle_phase IN
                       ('확장','전환','둔화','침체','바닥','관측부족')),
    phase_confidence REAL,   -- 0~1 (데이터 충분도)
    n_reports        INTEGER NOT NULL DEFAULT 0,  -- 윈도우 내 리포트 수

    PRIMARY KEY(level2_id, calc_date)
);

CREATE INDEX IF NOT EXISTS idx_signal_date  ON cycle_signals(calc_date);
CREATE INDEX IF NOT EXISTS idx_signal_phase ON cycle_signals(cycle_phase);

-- 섹터 내 종목별 펀더멘털 모멘텀 + 매수후보 점수
CREATE TABLE IF NOT EXISTS stock_scores (
    ticker        TEXT NOT NULL REFERENCES companies(ticker),
    calc_date     TEXT NOT NULL,
    -- 관절③ 펀더멘털 모멘텀
    eps_rev_z     REAL,   -- EPS 리비전 z-score (NULL=데이터없음)
    margin_trend  REAL,   -- 영업이익률 2분기 추세 (-1/0/+1)
    composite     REAL,   -- 0.5*eps_rev_z + 0.5*margin_trend (모멘텀 축)
    -- 매수후보 종합 (상승여력 + 모멘텀 + 글로벌 점유율)
    upside_pct    REAL,   -- 최신 평균 목표가 대비 현재가 상승여력 % (NULL=목표가없음)
    n_targets     INTEGER,-- 상승여력 산출에 쓰인 목표가 리포트 수
    share_bonus   REAL,   -- 글로벌 점유율 가산 (0~1)
    buy_score     REAL,   -- 0~100 종합 매수후보 점수
    rank_in_level2 INTEGER, -- 산업 내 buy_score 순위
    PRIMARY KEY(ticker, calc_date)
);

CREATE INDEX IF NOT EXISTS idx_score_date ON stock_scores(calc_date);

-- 산업별 매수 타이밍 신호 (애널리스트 방향 + 가격 확인)
CREATE TABLE IF NOT EXISTS timing_signals (
    level2_id      TEXT NOT NULL REFERENCES industries(level2_id),
    calc_date      TEXT NOT NULL,
    -- 가격(업종지수) 기반 확인 지표
    idx_ma20       REAL,    -- 20일 이동평균
    idx_ma60       REAL,    -- 60일 이동평균
    idx_trend_up   INTEGER, -- 정배열(MA20>MA60) 1/0
    idx_ret_4w     REAL,    -- 4주(20영업일) 수익률 %
    idx_ret_12w    REAL,    -- 12주(60영업일) 수익률 %
    idx_rsi14      REAL,    -- RSI(14)
    idx_high_break INTEGER, -- 8주 신고가 돌파 1/0
    price_days     INTEGER, -- 계산에 쓰인 거래일 수
    -- 종합 타이밍 판정
    timing_state   TEXT CHECK(timing_state IN
                     ('buy','watch','hold','caution','관측부족')),
    timing_score   REAL,    -- 0~100 (방향*가격확인 종합)
    PRIMARY KEY(level2_id, calc_date)
);

CREATE INDEX IF NOT EXISTS idx_timing_date ON timing_signals(calc_date);
