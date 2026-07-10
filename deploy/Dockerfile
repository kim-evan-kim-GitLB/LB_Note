# syntax=docker/dockerfile:1.7
# meetscript-ai 온프레미스 배포 이미지 (171 서버 / RTX PRO 6000 Blackwell, cu128)
#
# 단일 컨테이너로 FastAPI(API) + 프론트 dist(SPA 정적 서빙)를 함께 제공한다.
#   - 1단계(frontend): node 로 React/Vite 빌드 -> /fe/dist
#   - 2단계(runtime) : CUDA 런타임 + uv 로 STT venv 구성 + claude CLI + dist 합침
#
# 모델 가중치(3.9G)는 이미지에 굽지 않는다 -> docker-compose 에서 볼륨(ro)로 마운트.

# ---------- 1단계: 프론트엔드 빌드 ----------
# 빌드 컨텍스트 'frontendsrc' = 프론트 repo(/home/evan/meetscript-ai). compose 의
# additional_contexts 로 주입한다. (별도 git repo 라 백엔드 컨텍스트와 분리)
FROM node:20-slim AS frontend
WORKDIR /fe
COPY --from=frontendsrc . /fe
# 호스트 node_modules 가 함께 복사될 수 있으므로 제거 후 lock 기준 클린 설치.
RUN rm -rf node_modules dist && npm ci && npm run build
# 산출물: /fe/dist (vite 기본 outDir)

# ---------- 2단계: 런타임 ----------
# CUDA 12.8 런타임(Blackwell 호환). torch cu128 휠은 자체 CUDA 라이브러리를 포함하지만
# 드라이버 호환 베이스를 둬 안정성을 확보한다. (호스트 드라이버에 맞춰 태그 조정 가능)
FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 시스템 의존: python3.12(+venv), ffmpeg(webm/m4a/mp3 디코딩), node20(claude CLI), git/curl
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv \
        ffmpeg curl ca-certificates git gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

# uv (재현 가능한 의존성 동기화)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# [순서 중요] 의존성(torch cu128 등)을 먼저 설치해 레이어 캐시에 고정한다. 이렇게 하면
# 아래 코드/프롬프트만 바뀐 재빌드는 무거운 uv sync 레이어를 캐시로 건너뛰어 수 초에 끝난다.
# 앱은 `python -m src.web`(src 네임스페이스 실행)이라 프로젝트 자체 설치는 불필요 → --no-install-project.
COPY pyproject.toml uv.lock ./
# --extra slack: Slack 컨트롤 봇(slack-bolt)을 동일 이미지에 포함 → compose 의 slackbot
# 서비스가 같은 이미지로 `python -m src.slack_bot` 을 실행한다(별도 이미지 불필요).
RUN uv sync --frozen --no-install-project --no-dev --extra cu128 --extra slack

# 런타임에 필요한 코드 + 데이터. 추출/요약/정제 프롬프트(prompts/)와 glossary(config/)가
# 빠지면 추출 단계에서 FileNotFoundError 로 죽으므로 반드시 함께 복사한다.
#   - prompts/extract.ko.md : 액션아이템 추출 규칙(고도화 시 주로 만지는 파일)
#   - prompts/summarize.ko.md, prompts/clean.ko.md
#   - config/glossary.ko.json : 결정적 용어 교정 사전
COPY src ./src
COPY prompts ./prompts
COPY config ./config

# 프론트 빌드 산출물 합치기
COPY --from=frontend /fe/dist ./web-dist

# 엔트리포인트
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# venv 의 python/uvicorn 과 claude CLI(/usr/bin)를 PATH 앞단에 둔다.
ENV PATH="/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    COHERE_MODEL_PATH=/app/models/cohere-transcribe-03-2026 \
    WEB_FRONTEND_DIST=/app/web-dist \
    WEB_PORT=8088 \
    HOME=/root \
    TZ=KST-9

# 모델/산출물은 볼륨 마운트 지점. 미리 만들어 둔다.
RUN mkdir -p /app/models /app/output

EXPOSE 8088
ENTRYPOINT ["/entrypoint.sh"]
