#!/usr/bin/env bash
# 개발용 실행 — git clone 한 *최신 코드*를 이미지 위에 마운트해서 재빌드 없이 반영.
#   이미지의 .venv(torch 등)·/app/models(모델)는 그대로 쓰고, src/tools/run.py 만 git 최신으로 교체.
#   코드 업데이트 루프:  git pull  →  ./docker/run-dev.sh ...   (이미지 재빌드/재전송 0)
#
# 전제: 이 스크립트는 *git clone 한 레포 디렉토리* 안에서 실행 (예: ~/LB_Note).
#       입력/출력은 레포 밖(~/lbnote)에 둠 — 코드와 데이터 분리.
#
# 사용법:
#   git pull
#   IMAGE=lb-note:cu121 ./docker/run-dev.sh \
#       tools/run_long_slice10m.py "samples/회의.m4a" --dereverb --denoise --vad --out output/run1
#
# 환경변수:
#   IMAGE        실행 이미지 (기본 lb-note:cu121 / 171이면 lb-note:cu128)
#   SAMPLES_DIR  입력 음성 (기본 ~/lbnote/samples)
#   OUTPUT_DIR   출력 (기본 ~/lbnote/output)
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_DIR="$PWD"

IMAGE="${IMAGE:-lb-note:cu121}"
SAMPLES_DIR="${SAMPLES_DIR:-$HOME/lbnote/samples}"
OUTPUT_DIR="${OUTPUT_DIR:-$HOME/lbnote/output}"
mkdir -p "$OUTPUT_DIR"

echo ">> IMAGE=$IMAGE  코드=$REPO_DIR(git)  입력=$SAMPLES_DIR  출력=$OUTPUT_DIR"
docker run --gpus all --rm -it \
  -v "$REPO_DIR/src":/app/src \
  -v "$REPO_DIR/tools":/app/tools \
  -v "$REPO_DIR/run.py":/app/run.py \
  -v "$SAMPLES_DIR":/app/samples:ro \
  -v "$OUTPUT_DIR":/app/output \
  ${ENV_FILE:+--env-file "$ENV_FILE"} \
  "$IMAGE" "$@"
