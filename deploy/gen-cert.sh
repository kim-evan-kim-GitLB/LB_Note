#!/usr/bin/env bash
# SITE_HOST(.env.deploy)를 SAN 에 넣은 self-signed 인증서 생성 → deploy/certs/{cert,key}.pem.
#
# 왜: raw IP 접속은 SNI 가 없어 caddy 의 tls internal 이 인증서를 못 골라 핸드셰이크가 실패한다.
#     IP 를 SAN 에 박은 명시적 인증서를 만들어 caddy 에 직접 물려야 IP 접속이 동작한다.
#
# 사용법(호스트):
#   cd ~/LB_Note-deploy/deploy
#   bash gen-cert.sh            # .env.deploy 의 SITE_HOST 사용
#   (SITE_HOST 변경 시 다시 실행 후 caddy 재기동: docker compose ... up -d)
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${1:-.env.deploy}"
[ -f "$ENV_FILE" ] || { echo "[gen-cert] $ENV_FILE 없음. 먼저 .env.deploy 를 작성하세요." >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
: "${SITE_HOST:?[gen-cert] SITE_HOST(접속 IP/호스트명)를 .env.deploy 에 지정하세요}"

mkdir -p certs
# SAN 구성: localhost/127.0.0.1 기본 + SITE_HOST(쉼표 다중) 각각 IP/DNS 자동 판별.
SAN="DNS:localhost,IP:127.0.0.1"
IFS=',' read -ra HOSTS <<< "$SITE_HOST"
for h in "${HOSTS[@]}"; do
	h="$(echo "$h" | xargs)"   # 공백 제거
	[ -z "$h" ] && continue
	if [[ "$h" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
		SAN="$SAN,IP:$h"
	else
		SAN="$SAN,DNS:$h"
	fi
done

openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
	-keyout certs/key.pem -out certs/cert.pem \
	-subj "/CN=meetscript" -addext "subjectAltName=$SAN" >/dev/null 2>&1
chmod 600 certs/key.pem
echo "[gen-cert] 생성 완료: $(pwd)/certs/cert.pem"
echo "[gen-cert] SAN = $SAN"
echo "[gen-cert] 만료: $(openssl x509 -in certs/cert.pem -noout -enddate | cut -d= -f2)"
