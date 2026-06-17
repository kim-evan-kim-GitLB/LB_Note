#!/usr/bin/env bash
# 171 호스트에서 1회 실행하는 부트스트랩 — 모델/DB 준비 + 빌드 + 기동까지 한 번에.
#
# 사용법 (호스트 셸):
#   1) cp .env.deploy.example .env.deploy   # 그리고 JWT_SECRET 등 값 채우기
#   2) sudo bash bootstrap.sh <개발컨테이너_이름또는ID>
#        - <개발컨테이너>: 모델(3.9G)·기존 DB 를 꺼내올 현재 개발 컨테이너. `docker ps` 로 확인.
#        - 모델/DB 가 이미 호스트에 있으면 SKIP_COPY=1 로 복사 단계를 건너뛴다.
#
# 무엇을 하나:
#   - MODELS_DIR 에 모델 가중치 복사(없을 때) / DATA_DIR/web 에 기존 meetings.db 복사(없을 때)
#   - docker compose 로 빌드 + 기동(HTTP, HOST_PORT)
#   - 헬스체크 출력
set -euo pipefail
cd "$(dirname "$0")"

DEV="${1:-}"
ENV_FILE=".env.deploy"

if [ ! -f "$ENV_FILE" ]; then
  echo "[bootstrap] $ENV_FILE 없음. 먼저: cp .env.deploy.example .env.deploy 후 값(JWT_SECRET 등) 채우세요." >&2
  exit 1
fi
# 경로 변수 로드(MODELS_DIR/DATA_DIR/HOST_PORT 등)
set -a; . "./$ENV_FILE"; set +a
: "${MODELS_DIR:?.env.deploy 에 MODELS_DIR 필요}"
: "${DATA_DIR:?.env.deploy 에 DATA_DIR 필요}"

if [ "${SKIP_COPY:-0}" != "1" ]; then
  [ -n "$DEV" ] || { echo "[bootstrap] 사용법: sudo bash bootstrap.sh <개발컨테이너_이름또는ID> (또는 SKIP_COPY=1)" >&2; exit 1; }

  # 1) 모델 3.9G (없을 때만 복사)
  if [ ! -e "$MODELS_DIR/cohere-transcribe-03-2026/model.safetensors" ]; then
    echo "[bootstrap] 모델 복사: $DEV:/app/models -> $MODELS_DIR"
    mkdir -p "$MODELS_DIR"
    docker cp "$DEV:/app/models/." "$MODELS_DIR/"
  else
    echo "[bootstrap] 모델 이미 존재 → 스킵"
  fi

  # 2) 기존 DB(계정 등) 승계 (없을 때만 복사)
  if [ ! -e "$DATA_DIR/web/meetings.db" ]; then
    echo "[bootstrap] DB 복사: $DEV:/app/output/web/meetings.db -> $DATA_DIR/web/"
    mkdir -p "$DATA_DIR/web"
    docker cp "$DEV:/app/output/web/meetings.db" "$DATA_DIR/web/" || \
      echo "[bootstrap] (개발 컨테이너에 DB 없음 — 빈 DB 로 시작)"
  else
    echo "[bootstrap] DB 이미 존재 → 스킵(기존 데이터 보존)"
  fi
fi
mkdir -p "$DATA_DIR/web" "$MODELS_DIR"

# 3) TLS 인증서 생성(SITE_HOST 를 SAN 에 포함). 이미 있으면 건너뛴다(SITE_HOST 바뀌면 직접 재실행).
if [ ! -f certs/cert.pem ]; then
  echo "[bootstrap] TLS 인증서 생성(gen-cert.sh)"
  bash gen-cert.sh "$ENV_FILE"
else
  echo "[bootstrap] certs/cert.pem 이미 존재 → 스킵(SITE_HOST 변경 시 gen-cert.sh 재실행)"
fi

# 4) 빌드 + 기동
echo "[bootstrap] docker compose up -d --build"
docker compose --env-file "$ENV_FILE" up -d --build

# 5) 헬스체크 (caddy TLS 종단 → HTTPS, 자체서명이라 -k)
PORT="${HOST_PORT:-49152}"
echo "[bootstrap] 기동 대기..."
for i in $(seq 1 30); do
  code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 3 "https://localhost:$PORT/api/health" || true)
  [ "$code" = "200" ] && { echo "[bootstrap] OK"; curl -sk "https://localhost:$PORT/api/health"; echo; break; }
  sleep 2
done
echo
echo "[bootstrap] 완료. 접속: https://<171-LAN-IP>:$PORT  (자체서명 인증서 — 첫 접속 시 브라우저 경고 수용)"
