#!/usr/bin/env bash
# lb-note 이미지 빌드 (모델 bake 포함, 자기완결)
#
# 사용법:
#   ./docker/build.sh cu121     # RTX 4090 (160 서버)
#   ./docker/build.sh cu128     # RTX PRO 6000 / Blackwell (171 서버)
#
# ⚠️ 빌드 머신에 models/cohere-transcribe-03-2026/ 가 *실제 디렉토리*로 있어야 함:
#     uv run hf download CohereLabs/cohere-transcribe-03-2026 \
#       --local-dir models/cohere-transcribe-03-2026
#    그리고 models/gtcrn/model_trained_on_dns3.tar 도 필요(README/handoff 참조).
set -euo pipefail
cd "$(dirname "$0")/.."

VARIANT="${1:-cu121}"
case "$VARIANT" in
  cu121)
    BASE="nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04"
    EXTRA="cu121"
    ;;
  cu128)
    BASE="nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04"
    EXTRA="cu128"
    echo ">> cu128: torch 2.7+/cu128 로 빌드됩니다(Blackwell). lock 에 cu128 변종 포함됨."
    echo ">> 실제 런타임 동작은 Blackwell GPU(171)에서 검증 필요 — 노트북엔 해당 GPU 없음."
    ;;
  *)
    echo "usage: $0 [cu121|cu128]" >&2; exit 1
    ;;
esac

# 모델이 심볼릭링크면 Docker 가 못 따라가므로 미리 차단
if [ -L "models/cohere-transcribe-03-2026" ]; then
  echo "ERROR: models/cohere-transcribe-03-2026 가 심볼릭링크입니다." >&2
  echo "       Docker 는 컨텍스트 밖 심볼릭링크를 bake 할 수 없습니다." >&2
  echo "       실제 모델이 있는 머신(예: 160)에서 hf download 후 빌드하세요." >&2
  exit 1
fi

echo ">> building lb-note:$VARIANT  (base=$BASE)"
DOCKER_BUILDKIT=1 docker build \
  --build-arg BASE_IMAGE="$BASE" \
  --build-arg TORCH_EXTRA="$EXTRA" \
  -t "lb-note:$VARIANT" \
  .
echo ">> done: lb-note:$VARIANT"
