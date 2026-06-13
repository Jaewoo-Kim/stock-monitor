#!/bin/bash
# 최초 1회 실행: 가상환경 생성, 의존성 설치, .env 준비, git 초기화 여부 확인
set -e

echo "== 1. Python 가상환경 생성 =="
python3 -m venv .venv
source .venv/bin/activate

echo "== 2. 의존성 설치 =="
pip install --upgrade pip
pip install -r requirements.txt

echo "== 3. .env 준비 =="
if [ ! -f .env ]; then
  cp .env.example .env
  echo "-> .env 생성됨. API 키를 입력하세요 (DART, ECOS는 무료 발급):"
  echo "   - DART: https://opendart.fss.or.kr"
  echo "   - ECOS: https://ecos.bok.or.kr"
  echo "   - Gemini: https://aistudio.google.com (무료 티어)"
  echo "   - 텔레그램 봇: https://core.telegram.org/bots#how-do-i-create-a-bot"
else
  echo "-> .env 이미 존재, 건너뜀"
fi

echo "== 4. git 초기화 확인 =="
if [ ! -d .git ]; then
  git init
  git add .
  git commit -m "initial: 기획서 및 프로젝트 골격"
  echo "-> git 초기화 완료. private repo 생성 후 origin 연결 필요:"
  echo "   gh repo create <repo-name> --private --source=. --remote=origin"
  echo "   git push -u origin main"
else
  echo "-> git 이미 초기화됨, 건너뜀"
fi

echo ""
echo "== 셋업 완료 =="
echo "다음: claude 실행 후 PHASE1_GUIDE.md 순서대로 진행"
echo "  claude"
