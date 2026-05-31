#!/usr/bin/env bash
# lbnote_dev 컨테이너 안에 개발 도구 설치: Node.js(22) + Claude Code + Codex CLI.
#
# ⚠️ 컨테이너 writable layer라 `docker rm` 하면 사라짐 → 컨테이너 재생성(dev.sh) 후 이 스크립트 다시 실행.
# 🔑 인증은 본인이: 설치 후 `docker exec -it lbnote_dev bash` → `claude` / `codex` 로그인.
#
# 사용 (서버에서):
#   ./docker/dev-setup.sh              # 기본 컨테이너 lbnote_dev
#   ./docker/dev-setup.sh <컨테이너명>
set -euo pipefail
NAME="${1:-lbnote_dev}"

docker exec -i "$NAME" bash -s <<'INNER'
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates git >/dev/null
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
  apt-get install -y -qq nodejs >/dev/null
fi
npm install -g @anthropic-ai/claude-code @openai/codex 2>&1 | tail -3
echo "=== 설치 결과 ==="
echo "node: $(node -v)  npm: $(npm -v)"
echo "claude: $(claude --version 2>&1 | head -1)"
echo "codex:  $(codex --version 2>&1 | head -1)"
INNER

echo ">> done on $NAME"
echo ">> 인증: docker exec -it $NAME bash  →  claude / codex 로그인"
