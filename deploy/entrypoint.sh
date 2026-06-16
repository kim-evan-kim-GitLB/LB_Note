#!/usr/bin/env bash
# 컨테이너 진입점: 마운트 상태를 가볍게 점검한 뒤 웹 서버를 PID 1 로 실행한다.
set -euo pipefail

MODEL_DIR="${COHERE_MODEL_PATH:-/app/models/cohere-transcribe-03-2026}"

if [ ! -d "$MODEL_DIR" ]; then
  echo "[entrypoint] WARN: STT 모델 디렉토리가 없습니다: $MODEL_DIR" >&2
  echo "[entrypoint]       docker-compose 의 MODELS_DIR 볼륨 마운트를 확인하세요(3.9G 가중치)." >&2
fi

# claude CLI 존재 확인(요약/추출은 사용자별 자격증명으로 동작 — 전역 키는 주입하지 않음).
if ! command -v claude >/dev/null 2>&1; then
  echo "[entrypoint] WARN: claude CLI 를 찾지 못했습니다. 요약/추출(agent_cli) 백엔드가 동작하지 않습니다." >&2
fi

echo "[entrypoint] 웹 서버 기동: 0.0.0.0:${WEB_PORT:-8088} (TZ=${TZ:-UTC})"
# src/web/__main__.py 가 uvicorn(host=0.0.0.0, port=WEB_PORT)을 띄운다.
exec python -m src.web
