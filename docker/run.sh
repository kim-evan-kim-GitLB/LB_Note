#!/usr/bin/env bash
# lb-note 컨테이너 실행 (모델은 이미지에 bake 됨 → 모델 볼륨 불필요)
#
# 마운트하는 것은 입력(samples)·출력(output)뿐. 모델은 이미지 안에 있음.
#
# 사용법:
#   IMAGE=lb-note:cu121 ./docker/run.sh \
#       tools/run_long_slice10m.py "samples/회의.m4a" --dereverb --denoise --vad --out output/run1
#
#   IMAGE=lb-note:cu128 ./docker/run.sh \
#       run.py "samples/회의.m4a" --pipeline --reference "answer/ax_tf_클로바.txt" --out output/run1
#
# 환경변수:
#   IMAGE        실행할 이미지 태그 (기본 lb-note:cu121)
#   SAMPLES_DIR  입력 음성 디렉토리 (기본 ./samples)
#   OUTPUT_DIR   출력 디렉토리 (기본 ./output)
#   ENV_FILE     (선택) .env 파일 경로 → --env-file 로 주입
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-lb-note:cu121}"
SAMPLES_DIR="${SAMPLES_DIR:-$PWD/samples}"
OUTPUT_DIR="${OUTPUT_DIR:-$PWD/output}"
mkdir -p "$OUTPUT_DIR"

docker run --gpus all --rm -it \
  -v "$SAMPLES_DIR":/app/samples:ro \
  -v "$OUTPUT_DIR":/app/output \
  ${ENV_FILE:+--env-file "$ENV_FILE"} \
  "$IMAGE" "$@"
