#!/usr/bin/env bash
# 상주 dev 컨테이너 — 포트 없이 `docker exec`로 접속해 core 로직 개발.
#   이미지의 .venv(torch)·/app/models(모델)는 그대로 두고, git 코드(src/tools/run.py)만 마운트.
#   → 호스트에서 git pull 하면 컨테이너 안에도 즉시 반영(재빌드 X).
#
# 사용 (서버의 ~/LB_Note 에서):
#   ./docker/dev.sh                       # 160 (기본 cu121)
#   IMAGE=lb-note:cu128 ./docker/dev.sh   # 171 (Blackwell)
#   docker exec -it lbnote_dev bash       # ← 접속
#
# 컨테이너 안에서 실행 예:
#   uv run --no-sync python tools/run_long_slice10m.py samples/x.m4a --vad --out output/r1
#   (또는 /app/.venv/bin/python ...)
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_DIR="$PWD"

IMAGE="${IMAGE:-lb-note:cu121}"
NAME="${NAME:-lbnote_dev}"

mkdir -p "$HOME/lbnote/samples" "$HOME/lbnote/output"
docker rm -f "$NAME" >/dev/null 2>&1 || true

docker run -dit --gpus all --name "$NAME" --restart unless-stopped \
  --shm-size=16g \
  --entrypoint sleep \
  -v "$REPO_DIR/src":/app/src \
  -v "$REPO_DIR/tools":/app/tools \
  -v "$REPO_DIR/run.py":/app/run.py \
  -v "$HOME/lbnote/samples":/app/samples \
  -v "$HOME/lbnote/output":/app/output \
  "$IMAGE" infinity

echo ">> '$NAME' 상주 시작 (image=$IMAGE)"
echo ">> 접속:  docker exec -it $NAME bash"
echo ">> 코드 갱신:  (호스트) git pull  →  컨테이너 안에 즉시 반영"
