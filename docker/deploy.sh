#!/usr/bin/env bash
# 빌드한 이미지를 원격 서버로 전송 (레지스트리 없이 save → ssh → load).
# 모델이 이미지에 bake 되어 있으므로 이 이미지 하나만 옮기면 서버는 docker run 만 하면 됨.
#
# 사용법:
#   ./docker/deploy.sh <ssh-host> <tag>
#   ./docker/deploy.sh evan@10.0.0.160 cu121      # 160 (RTX 4090)
#   ./docker/deploy.sh evan@<171-host>  cu128      # 171 (RTX PRO 6000)
#
# 전제: 로컬에서 ssh <host> 무암호 접속(키) 가능, 원격에 docker 설치/권한 있음.
set -euo pipefail

HOST="${1:?ssh host 필요 (예: evan@10.0.0.160)}"
TAG="${2:-cu121}"
IMAGE="lb-note:$TAG"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "ERROR: 로컬에 $IMAGE 없음. 먼저 ./docker/build.sh $TAG" >&2; exit 1;
}

SIZE=$(docker image inspect "$IMAGE" --format '{{.Size}}' | awk '{printf "%.1fGB", $1/1e9}')
echo ">> $IMAGE ($SIZE) → $HOST  (gzip 스트림 전송, 시간 걸릴 수 있음)"
docker save "$IMAGE" | gzip | ssh "$HOST" 'gunzip | docker load'

echo ">> 원격 확인:"
ssh "$HOST" "docker images $IMAGE"
echo ">> done: $IMAGE → $HOST"
