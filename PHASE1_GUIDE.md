# Phase 1 착수 가이드

Claude Code 첫 세션에서 이 순서로 진행하면 된다. 각 단계는 이전 단계 산출물에 의존하므로
순서를 건너뛰지 않는다.

## 0. 컨텍스트 확인 (첫 메시지)

```
CLAUDE.md와 docs/00_통합기획서_v2.md를 읽고, Phase 1을 시작하자.
순서는 PHASE1_GUIDE.md를 따라줘.
```

## 1. DB 스키마 (data/schema.sql)

docs/01(산업분류체계), docs/02(애널리스트 트래킹)에 흩어진 스키마 조각을 통합해
data/schema.sql로 작성. 최소 테이블:

- `sectors`, `industries` (Level1/Level2, 31개 산업 시드 데이터 포함)
- `companies` (KRX 상장종목, level2_id, is_representative)
- `mapping_overrides` (2차전지 등 종목 단위 오버라이드)
- `industry_etfs`
- `analysts`, `analyst_industry_rank`, `award_seeds`
- `report_events` (source_url, broker_url, target_price, opinion, has_summary 등)
- `price_history`, `industry_index_history` (pykrx 수집분)
- `theme_tags`, `company_themes`

스키마 작성 후 `sqlite3 data/monitor.db < data/schema.sql`로 빈 DB 생성까지 확인.

## 2. 산업 분류 시드 데이터 (data/seed/)

docs/01의 31개 Level2 산업과 대표기업 표를 CSV/SQL INSERT로 변환:

- `data/seed/industries.csv` — Level1/Level2, coverage_density, signal_window_weeks
- `data/seed/representative_companies.csv` — 대표기업 시드 (분기 갱신 전까지 사용)
- `data/seed/award_seeds.csv` — 애널리스트 어워드 시드 (docs/02의 검증 예시부터)

KRX 전체 종목 → Level2 자동 매핑은 3단계 이후, 여기서는 시드만.

## 3. 종목-산업 매핑 (src/collectors/company_mapper.py)

- pykrx로 KRX 상장종목 전체 조회
- WICS 분류 → Level2 변환 (docs/01 2.2절 오버라이드 규칙 적용)
- 매핑 신뢰도 낮은 종목은 `mapping_confidence` 낮게 표시 (보정 큐용)
- 출력: `companies` 테이블 적재

## 4. 네이버 리서치 크롤러 (src/collectors/naver_research.py)

- 일별 신규 리포트 목록 수집 (날짜, 증권사, 애널리스트, 종목/산업, 제목, 목표가, 투자의견)
- source_url(상세 페이지 링크) 필수 확보
- broker_url(증권사 도메인 링크) 확보 시도 — **확보율을 로그로 남겨 docs/02 미결사항 검증**
- PDF 공개 여부 판별, 공개분은 다운로드 경로만 기록(`pdf_path`), 다운로드는 이번 단계 선택
- 출력: `report_events` 테이블 적재

## 5. 시세·업종지수 수집 (src/collectors/price_collector.py)

- pykrx로 대표기업·ETF 일별 종가, 업종지수 수집
- 출력: `price_history`, `industry_index_history`

## 6. 일일 배치 스크립트 (src/run_daily.py)

3~5를 순서대로 실행하는 단일 엔트리포인트. 이 단계까지 완료되면:

```bash
python src/run_daily.py
```

실행 시 무인으로 리포트·시세 데이터가 DB에 쌓여야 한다. GitHub Actions 워크플로
(.github/workflows/daily.yml)는 이 스크립트를 cron으로 호출하는 것뿐이므로 마지막에 작성.

## Phase 1 완료 기준 (통합기획서 v2.0 8장)

- 매일 자동 수집이 무인으로 돌아감
- 3대 지표(Breadth/Magnitude/Upside Gap)를 수동 SQL 쿼리로 계산 가능한 데이터가 쌓임
- (선택) 워치리스트 텔레그램 알림 — 데이터가 어느 정도 쌓인 뒤 진행해도 무방

## 진행 중 주의

- 매 단계마다 CLAUDE.md의 7원칙(특히 4. 산업 귀속=종목코드 기준, 7. 원문 링크 2종)을
  코드가 위반하지 않는지 점검
- 네이버/한경 크롤러는 구조 변경 가능성이 높으므로 파싱 로직을 별도 함수로 분리해 교체 용이하게
- 이 단계에서는 LLM 호출 없음 (Phase 3부터)
