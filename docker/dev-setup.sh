#!/usr/bin/env bash
# lbnote_dev 컨테이너 안에 개발 환경 구성:
#   - non-root 사용자 evan 생성 (sudo NOPASSWD)
#   - Node.js(22) 시스템 설치 + Claude Code / Codex CLI 를 evan 계정(~/.npm-global)에 설치
#
# ⚠️ 컨테이너 writable layer라 `docker rm` 하면 사라짐 → 컨테이너 재생성(dev.sh) 후 이 스크립트 다시 실행.
#    즉 셋업 루프 = `./docker/dev.sh && ./docker/dev-setup.sh` 두 줄.
# 🔑 인증은 본인이: 설치 후 `docker exec -it -u evan lbnote_dev bash` → `claude` / `codex` 로그인.
#
# 사용 (서버에서):
#   ./docker/dev.sh && ./docker/dev-setup.sh     # 160 (cu121 기본)
#   IMAGE=lb-note:cu128 ./docker/dev.sh && ./docker/dev-setup.sh   # 171 (Blackwell)
#   ./docker/dev-setup.sh <컨테이너명>            # 컨테이너명 지정
#   DEV_USER=other ./docker/dev-setup.sh          # 사용자명 변경(기본 evan)
set -euo pipefail
NAME="${1:-lbnote_dev}"
DEV_USER="${DEV_USER:-evan}"

docker exec -i "$NAME" bash -s "$DEV_USER" <<'INNER'
set -e
DEV_USER="$1"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates git sudo >/dev/null
# Node.js 22 (시스템 설치, root 필요)
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
  apt-get install -y -qq nodejs >/dev/null
fi
# non-root 개발 사용자 + sudo NOPASSWD
id "$DEV_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$DEV_USER"
usermod -aG sudo "$DEV_USER" 2>/dev/null || true
echo "$DEV_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$DEV_USER"
chmod 0440 "/etc/sudoers.d/$DEV_USER"
# Claude Code / Codex 를 evan 계정 홈(~/.npm-global)에 설치 (root 아님)
sudo -u "$DEV_USER" -H bash -lc '
  set -e
  mkdir -p ~/.npm-global
  npm config set prefix ~/.npm-global >/dev/null
  grep -q ".npm-global/bin" ~/.bashrc 2>/dev/null || echo "export PATH=\$HOME/.npm-global/bin:\$PATH" >> ~/.bashrc
  export PATH=$HOME/.npm-global/bin:$PATH
  npm install -g @anthropic-ai/claude-code @openai/codex 2>&1 | tail -3
  echo "  claude -> $(command -v claude || echo MISSING) : $(claude --version 2>&1 | head -1)"
  echo "  codex  -> $(command -v codex  || echo MISSING) : $(codex  --version 2>&1 | head -1)"
'
echo "=== 설치 결과 ==="
echo "node $(node -v) / npm $(npm -v)"
echo "user: $(id "$DEV_USER")"
INNER

echo ">> done on $NAME"
echo ">> 접속: docker exec -it -u $DEV_USER $NAME bash"
echo ">> 인증: 위 셸에서  claude  /  codex  로그인"
