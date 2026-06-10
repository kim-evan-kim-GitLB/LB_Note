"""웹 백엔드 진입점 — `python -m src.web` 로 실행.

(.venv가 root 소유 + /app 최상위 root 소유 → top-level run_server.py 대신 모듈 진입점 사용.)
실행:
  sudo /app/.venv/bin/python -m src.web
환경변수: WEB_PORT(기본 8000), WEB_CLEAN_BACKEND(기본 passthrough), WEB_FRONTEND_DIST(컨테이너 정적).

dev: 프론트는 Vite(:3000)가 /api 를 이 서버(:8000)로 프록시. 컨테이너: WEB_FRONTEND_DIST 로 정적 동봉.
"""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    # WEB_PORT 등 .env 값을 포트 읽기 전에 로드(미적용 버그 수정). app import 전이라 직접 로드.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    port = int(os.environ.get("WEB_PORT", "8000"))
    uvicorn.run("src.web.app:app", host="0.0.0.0", port=port, log_level="info")
