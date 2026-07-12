# LB Note Slack 봇 — 라이브 배포 · 사용자 UX · 공지 DB/콘솔 (2026-07-10)

2026-07-09 설계([docs/2026-07-09-slack-control-bot.md](2026-07-09-slack-control-bot.md))된 Slack 컨트롤 봇을
**실제 배포·검증**하고, 사용자 관점 UX 개선과 **공지 DB화 + 웹 관리자 콘솔**까지 완성한 작업 기록.

## 요약

| 영역 | 결과 | PR |
|------|------|----|
| 봇 온프레미스 배포 통합 + 라이브 연결 | ✅ 머지·배포 | LB_Note **#30** |
| 사용자 관점 UX(상태 판정) + 공지 관리자 게이트 + 공지 DB화 | ✅ 머지 | LB_Note **#31** |
| 웹 관리자 콘솔 "공지 관리"(CRUD·Slack 미리보기) | ✅ 머지 | LB_Note-web **#24** |

Jira: `WDLABD2411-543`. 봇 계정 `@lbnotebot`(Litbig 워크스페이스).

---

## 1. 아키텍처 (배포 형상)

봇은 웹서버와 **별개 프로세스 = 별개 컨테이너**다.

```
Slack(Socket Mode, 아웃바운드 WS)
        │
[meetscript-slackbot-171]  ← python -m src.slack_bot
        │  http://meetscript:8088  (compose 내부 DNS, 관리자 호출은 단명 admin JWT 직접 서명)
        ▼
[meetscript-171]  ← FastAPI (:8088, 호스트 미노출 = expose만)
        ▲
[meetscript-caddy-171]  ← TLS, 호스트 :49152
```

- 봇→웹 API는 **compose 내부 네트워크**(`LBNOTE_API_BASE=http://meetscript:8088`). 웹은 8088을 호스트에 열지 않으므로
  봇은 반드시 같은 compose 네트워크의 서비스여야 한다(호스트 loopback 불가).
- 관리자 권한이 필요한 호출은 **매 요청 단명(60초) admin JWT**를 `JWT_SECRET`으로 직접 서명(scope 미포함 →
  `user_from_token(scope=None)` 통과, DB `admin` 계정 조회). 장기 토큰 미저장.
- `deploy/docker-compose.yml`의 `slackbot` 서비스가 웹과 **동일 이미지**(`meetscript-ai:171`)를 재사용하되
  엔트리포인트만 봇 모듈로 교체. `Dockerfile`은 `uv sync ... --extra slack`으로 slack-bolt를 이미지에 포함.

## 2. 봇 명령 (사용자 관점)

봇 목적: **관리자 부재 시 일반 사용자가 스스로 상태를 알거나 조치**하기 위함.

| 명령 | 동작 | 권한 |
|------|------|------|
| `상태` / `status` | **사용자용 한 줄 판정** — `✅ LB Note 정상 이용 가능` / `⚠️ 문제+조치안내`. 내부 지표(백엔드·GPU·디스크·claude인증) 숨김 | 전체 |
| `비번초기화` / `reset` | 본인 셀프서비스(Slack 이메일==username 매칭) → 임시비번 **DM 전용** | 본인 |
| `공지` / `notice` | **DB 최신 활성 공지**를 읽어 지정 채널 배포 | **관리자(role=admin)만** |
| `요구사항` / `req` | DB `requirements` 테이블 적재 → 접수번호 회신 | 전체 |
| `help` | 사용법 | 전체 |

- **상태 판정 로직**: health 응답 실패 → "연결 안 됨" 안내 / 디스크 ≥90% → "저장공간 임박" 안내 / 그 외 → 정상.
  metrics(관리자 조회)가 실패해도 서버가 응답하면 사용자에겐 정상으로 본다.
- **공지 권한 게이트**: 요청자 Slack 이메일 → LB Note 계정 role 확인(`GET /api/admin/users` 재사용). admin 아니면 거부.

## 3. 공지 = DB 저장 + 웹 콘솔 (설계 변경 이력)

공지 소스는 **인라인 → md 파일 → DB**로 수렴했다(최종: DB). 이유: 이모지 완료체크·웹 배너 확장·앱 데이터 일관성.

### 백엔드 (`src/web`)
- `notices` 테이블(`store.py`) + `NoticeStore`(add/get/latest/list/update/delete).
  - `id, title(선택), body, active(1=활성), created_by, created_at, updated_at`
  - 스키마는 `CREATE TABLE IF NOT EXISTS` → 배포 시 앱 기동에 **자동 생성**(기존 DB 무영향, 수동 마이그레이션 불필요).
- 엔드포인트(`app.py`, `require_admin`):
  - `POST /api/notices` 작성 · `GET /api/notices` 목록 · `PATCH /api/notices/{id}` 수정/활성토글 · `DELETE /api/notices/{id}`
  - `GET /api/notices/latest` — **최신 활성 공지**(봇이 읽는 진입점). 없으면 `{"notice": null}`.
  - `body` 빈값 생성/수정 거부(422).

### 봇 (`src/slack_bot`)
- `공지` → `lbnote_client.get_latest_notice()` → 최신 활성 공지(제목+본문)를 `📢 *공지*` 형식으로 배포.
  등록 공지 없으면 "관리자 콘솔에서 먼저 등록" 안내.

### 프론트 (`LB_Note-web`)
- `src/components/NoticeManagement.tsx` — 관리자 콘솔 "공지 관리" 카드.
  - **"다음 배포 대상"**(최신 활성) Slack 미리보기 하이라이트, 작성/편집 모달의 **라이브 미리보기**,
    활성 토글(Eye/EyeOff), 삭제 확인, 로딩/에러/빈 상태.
- `src/services/noticeService.ts` — `/api/notices` CRUD(axios, Bearer 자동주입).
- `src/components/AdminSettingsView.tsx` — 공지 카드 최상단 + 섹션 앵커 내비(공지/사용자/Google).

**전체 흐름**: 관리자가 웹 콘솔에서 공지 작성(DB) → Slack에서 `@봇 공지` → 봇이 최신 공지 배포.

## 4. 배포 절차 (라이브 호스트)

⚠️ 공지 콘솔은 프론트라, 배포 이미지가 **프론트 repo 클론에서 빌드**한다 → **두 repo 다 pull 필요**.

```bash
# 1) 백엔드 repo (#30·#31)
cd ~/LB_Note-deploy && git pull
# 2) 프론트 repo (#24) — .env.deploy 의 FRONTEND_DIR
cd <FRONTEND_DIR> && git pull
# 3) 재빌드 (반드시 --build: slack-bolt·프론트 dist 갱신)
cd ~/LB_Note-deploy/deploy && docker compose --env-file .env.deploy up -d --build
docker compose --env-file .env.deploy logs -f slackbot
```

배포 후 확인: 웹 관리자 콘솔에서 공지 작성 → `@lbnotebot 공지`(배포) → `@lbnotebot 상태`(한 줄 판정).

## 5. 검증 (완료)

- 봇 모듈 임포트 · ruff 클린 · `build_app` 임포트.
- 봇 admin JWT → 실 엔드포인트 종단(`/api/notices` CRUD·`/admin/metrics`·`reset-password`·`/health`).
- 공지 종단: 생성·최신조회·비활성 폴백·빈값거부(422)·삭제/404 + 봇이 최신 공지 배포·비관리자 거부.
- 상태 4갈래(정상/디스크임박/연결실패/metrics실패-정상) 시뮬.
- 기존 웹 회귀(test_admin_users + test_web_contract) 24 passed.
- 프론트 `tsc --noEmit` · `vite build` 통과.
- 라이브: `@lbnotebot help`·`상태` 응답 확인(Litbig 워크스페이스 연결).

## 6. 운영 함정 / 주의

- ⚠️ **admin 계정 `must_change_password=1`이면 봇의 모든 관리자 호출이 403**(비번초기화·상태·요구사항·공지).
  가동 전 admin 비번이 변경된 상태인지 확인.
- ⚠️ 재배포는 **반드시 `up -d --build`** — 안 하면 구이미지에 slack-bolt/프론트 미반영.
- Slack 앱 매니페스트 스코프 변경 시 앱 재설치 → `xoxb` 토큰 재발급 가능(`.env.deploy` 갱신).
- 개발 환경은 컨테이너(웹 백엔드 :8000). vite dev 프리뷰는 VS Code 포트포워딩으로만 접근.
- 봇 토큰(`SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`)은 `deploy/.env.deploy`(gitignore)에만. 저장소엔 `.example` 템플릿만.

## 7. 후속(미포함)

- 공지 웹앱 배너 노출(DB 준비됨 → 확장 용이), 공지 이력/만료.
- 요구사항 Jira 연동·관리자 알림, 관리자 승인형 비번초기화.
