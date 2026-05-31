# syntax=docker/dockerfile:1.7
#
# lb-note — Cohere STT 파이프라인 (모델 bake 포함, 자기완결 이미지)
#
# 빌드 변종:
#   cu121 (RTX 4090 / Ada)      : BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04, TORCH_EXTRA=""
#   cu128 (RTX PRO 6000 / Blackwell): BASE_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04, TORCH_EXTRA="cu128"
#
# 빌드는 docker/build.sh 로 하는 것을 권장.
#
# ⚠️ 빌드 컨텍스트 전제: models/cohere-transcribe-03-2026/ 가 *실제 디렉토리*여야 함
#    (노트북 worktree 처럼 심볼릭링크면 Docker 가 못 따라감 → 160 서버에서 hf download 후 빌드).

ARG BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04
FROM ${BASE_IMAGE}

# 어느 torch extra 로 동기화할지. ""=현재 pyproject(cu121) 그대로, "cu128"=Blackwell용 extra(별도 셋업 필요)
ARG TORCH_EXTRA=""

ENV DEBIAN_FRONTEND=noninteractive \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=automatic \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    # 모델/체크포인트를 전부 bake 했으므로 런타임 네트워크 불필요 (171 오프라인 대비).
    # 필요 시 docker run -e HF_HUB_OFFLINE=0 로 덮어쓸 수 있음.
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1

# 시스템 의존성: ffmpeg(m4a/mp3/aac 디코딩). python 은 uv 가 standalone 으로 받음.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv 바이너리만 복사 (별도 설치 불필요)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# ── 레이어 1: 의존성만 (코드/모델 제외) → 가장 안정적, 캐시 잘 먹음 ──
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "$TORCH_EXTRA" ]; then EXTRA="--extra $TORCH_EXTRA"; else EXTRA=""; fi; \
    uv sync --frozen --no-install-project --no-dev $EXTRA

# ── 레이어 2: 모델 가중치 (3.9G) — 코드보다 먼저 = 거의 안 바뀌는 하위 레이어로 고정 ──
#    (이 레이어 덕에 코드만 고치면 3.9G 는 캐시 재사용, 재빌드 몇 초)
COPY models/cohere-transcribe-03-2026/ ./models/cohere-transcribe-03-2026/
COPY models/gtcrn/ ./models/gtcrn/

# ── 레이어 3: 앱 코드 — 가장 자주 바뀜 = 맨 위 ──
COPY src/ ./src/
COPY tools/ ./tools/
COPY run.py ./

# ── 프로젝트 설치 + 출력 디렉토리(볼륨 마운트 전 fallback) ──
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "$TORCH_EXTRA" ]; then EXTRA="--extra $TORCH_EXTRA"; else EXTRA=""; fi; \
    uv sync --frozen --no-dev $EXTRA \
    && mkdir -p output

# 빌드 검증: 모델/GTCRN 가 이미지 안에 제대로 들어갔는지 확인 (실패 시 빌드 중단)
RUN uv run --no-sync python -c "from src import config; s=config.env_status(); print(s); assert s['cohere_model_exists'] and s['gtcrn_model_exists'], 'baked model missing'"

# `uv run --no-sync` = 런타임에 재동기화(네트워크) 안 함. 오프라인 안전.
ENTRYPOINT ["uv", "run", "--no-sync", "python"]
CMD ["run.py", "--help"]
