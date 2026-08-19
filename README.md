# 국내 주식 산업 모니터링 서비스

1인 사용 전제, 제로-서버, 최소비용 아키텍처의 국내 상장주식 산업(섹터) 모니터링 도구.
거시→산업→기업 탑다운 투자 로직을 시그널 엔진으로 시스템화한다.

> 이 프로젝트는 다른 프로젝트(DearName 등)와 완전히 독립되어 있으며, 코드/데이터/설정을
> 공유하지 않는다.

## 기획 문서

- [docs/00_통합기획서_v2.md](docs/00_통합기획서_v2.md) — 전체 개요, 아키텍처, 로드맵
- [docs/01_산업분류체계_v1.1.md](docs/01_산업분류체계_v1.1.md) — 산업 분류·종목 매핑
- [docs/02_애널리스트_트래킹_설계_v1.md](docs/02_애널리스트_트래킹_설계_v1.md) — 애널리스트 Top3, 리포트 수집
- [docs/03_전체화면기획서_v2.md](docs/03_전체화면기획서_v2.md) — 전체 화면 기획서

## 셋업

### 1. 자동 셋업

```bash
chmod +x setup.sh   # 압축 해제 시 실행권한이 풀릴 수 있음
./setup.sh
```

가상환경 생성, 의존성 설치, `.env` 생성, git 초기화를 한 번에 처리한다.
`.env`가 생성되면 안내된 링크에서 API 키를 발급해 채운다 (DART·ECOS·Gemini는 무료).

**텔레그램 알림 봇 발급** (무료):
1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 안내에 따라 봇 생성 → 발급된 토큰을 `TELEGRAM_BOT_TOKEN`에 입력
2. 생성된 봇과 대화 시작 (아무 메시지나 전송)
3. `https://api.telegram.org/bot<토큰>/getUpdates` 접속 → 응답의 `chat.id` 값을 `TELEGRAM_CHAT_ID`에 입력
4. 로컬 테스트: `python src/notify/telegram.py` (토큰 미설정 시 자동 skip, 알림 대상 없으면 무전송)

### 2. private repo 연결 (아직 안 했다면)

```bash
gh repo create stock-monitor --private --source=. --remote=origin
git push -u origin main
```

### 3. Claude Code로 개발 시작

```bash
claude
```

첫 메시지:
```
CLAUDE.md와 docs/00_통합기획서_v2.md를 읽고, Phase 1을 시작하자.
순서는 PHASE1_GUIDE.md를 따라줘.
```

### 4. (배포 시) GitHub Secrets 등록

repo Settings > Secrets and variables > Actions 에 `.env`와 동일한 키를 등록.
GitHub Actions가 일 배치로 수집·분석·대시보드 생성을 수행한다.

## 아키텍처 요약

```
GitHub Actions (cron, 무료)
  → 수집: 네이버 리서치 / pykrx / DART / ECOS
  → 적재: SQLite (data/monitor.db)
  → 계산: 시그널 엔진 (목표주가 모멘텀 3지표)
  → 요약: Claude API (워치 애널리스트 공개 PDF만, Batch)
  → 출력: 정적 HTML/JSON → GitHub Pages or Cloudflare Pages
  → 알림: 텔레그램 봇
```

상세 비용 구조는 통합기획서 v2.0 5장 참조 (월 수천 원 이내 목표).

## 현재 단계

Phase 1 — 데이터 파이프라인 구축 (산업 분류 매핑, 리포트 크롤러, SQLite 스키마)
상세 작업 순서는 [PHASE1_GUIDE.md](PHASE1_GUIDE.md) 참조.
