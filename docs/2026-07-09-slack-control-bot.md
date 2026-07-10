# LB Note Slack 컨트롤 봇 설계 (2026-07-09)

특정 Slack 채널에서 동작하며 LB Note 운영/사용자와 다음을 교류하는 봇.

1. **비밀번호 초기화** — 셀프서비스(본인만), 임시 비번은 DM 전송
2. **서버 상태 / 병목 확인** — health + metrics(+GPU) 요약
3. **공지사항 update** — 지정 Slack 채널 브로드캐스트
4. **help / 요구사항 저장** — LB Note DB `requirements` 테이블에 적재

## 결정 사항 (사용자 확정)

| 항목 | 결정 |
|------|------|
| 연결 방식 | **Socket Mode + 독립 프로세스** (공개 인바운드/TLS 불필요, 방화벽 개방 없음) |
| 비번초기화 권한 | **본인 셀프서비스** — 임시 비번은 **DM** 으로만 전달 |
| 본인 증명 | **Slack 프로필 이메일 == LB Note `username`** 정확매칭 (가정) |
| 공지 대상 | **Slack 채널 브로드캐스트만** (DB/웹 미저장) |
| 공지 권한 | **LB Note `role=admin` 만** 배포(요청자 이메일→계정 role 확인, `GET /api/admin/users` 재사용) |
| 공지 내용 소스 | **DB `notices` 테이블**(웹 관리자 콘솔 작성/관리). 봇은 최신 활성 공지를 읽어 배포. 웹앱 배너 노출로 확장 가능 |
| 요구사항 저장 | **LB Note DB `requirements` 테이블 신설** |

> **가정(중요):** LB Note 계정 `username` 이 곧 회사 이메일(`peter@litbig.com` 등, 56명 중 `admin` 만 예외).
> `email` 컬럼은 비어 있으므로 매칭 기준은 **username**. Slack 워크스페이스가 동일 `@litbig.com`
> 이메일을 쓴다는 전제. 매칭 실패 시 초기화 거부(안내 메시지).

## 아키텍처

```
Slack 워크스페이스
   │  (Socket Mode, 아웃바운드 WebSocket — 인바운드 개방 없음)
   ▼
[slack_bot 독립 프로세스]  src/slack_bot/
   │  1) 요청자 Slack email 조회 (users.info, users:read.email)
   │  2) 관리자 JWT 를 JWT_SECRET 으로 직접 서명 (sub=admin, scope 없음, 단명 TTL)
   ▼  HTTP (127.0.0.1:8088, 로컬)
[LB Note FastAPI]  src/web/app.py
   - POST /api/admin/users/{username}/reset-password   (기존 재사용)
   - GET  /api/health                                  (기존, 공개)
   - GET  /api/admin/metrics                           (기존, 관리자)
   - POST /api/requirements  · GET /api/requirements    (신설)
```

- 봇은 **별도 프로세스**라 웹서버 재기동과 독립. 크래시가 웹에 영향 없음.
- 봇→API 는 **내부망 평문 HTTP** 호출이라 노출면 없음. 호출 대상 주소는 실행 환경에 따라 다름:
  - **개발(단일 호스트)**: 웹서버가 호스트 `127.0.0.1:8088` 에 떠 있으면 기본값 그대로.
  - **온프레미스 배포(compose)**: 웹(`meetscript`)은 8088 을 호스트에 노출하지 않으므로(=`expose` 만),
    봇 컨테이너(`slackbot`)는 **compose 서비스 DNS** 로 호출한다 → `LBNOTE_API_BASE=http://meetscript:8088`.
- 관리자 권한은 봇이 **매 요청마다 단명(60초) admin JWT** 를 직접 서명해 사용 → 장기 토큰 저장 없음.
  (`user_from_token(scope=None)` 규약: scope 클레임 없는 세션 토큰만 통과 → 봇 토큰은 scope 미포함.)

## 명령 UX (채널 내 멘션 + 슬래시)

`@LBNoteBot <서브명령>` 또는 `/lbnote <서브명령>` — 동일 디스패처.

| 서브명령 | 동작 | 응답 위치 |
|----------|------|-----------|
| `비번초기화` / `reset` | 요청자 이메일→username 매칭 → 임시비번 생성 → `admin_reset_password` 호출 → **DM 전송** | DM(비번), 채널엔 "DM 확인" 안내만 |
| `상태` / `status` | **사용자용 '정상/주의' 한 줄 판정**(health 기반, 내부 수치 숨김) | 채널 |
| `공지` / `notice` | **관리자(role=admin)만.** DB 최신 활성 공지(웹 콘솔 작성)를 읽어 배포 | 채널 |
| `요구사항 <내용>` / `req` | `POST /api/requirements` 적재 | 채널(접수번호) |
| `help` / (빈 멘션) | 사용법 안내 | 채널 |

- **채널 화이트리스트**: `SLACK_ALLOWED_CHANNELS`(CSV) 설정 시 그 채널에서만 반응. 비번초기화는 어느 채널이든 **결과를 DM** 으로만 보냄(채널에 비번 노출 절대 금지).

## 백엔드 신설 — `requirements`

`src/web/store.py` 에 테이블 + `RequirementStore`(또는 기존 store 확장):

```sql
CREATE TABLE IF NOT EXISTS requirements(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL DEFAULT 'slack',   -- 'slack'|'web'
  reporter    TEXT,                             -- slack 이메일/표시명
  text        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open',     -- open|done|dropped
  created_at  TEXT NOT NULL
);
```

엔드포인트(`src/web/app.py`, `Depends(require_admin)`):

- `POST /api/requirements` — body `{text, source?, reporter?}` → `{id, created_at}` (201). `text` 필수/길이검증. `observability.audit("requirement.create", ...)`.
- `GET /api/requirements?status=open` — `{requirements:[...]}` 목록(관리자·웹 조회용).

> 공지는 DB 미저장(결정) → 테이블/엔드포인트 없음. 서버상태는 기존 엔드포인트 재사용.

## 봇 모듈 구성 — `src/slack_bot/`

- `__main__.py` — Socket Mode 진입(`sudo .venv/bin/python -m src.slack_bot`).
- `bot.py` — slack_bolt `App` + `app_mention`/슬래시 핸들러 → 디스패처.
- `lbnote_client.py` — admin JWT 서명 + LB Note API httpx 호출(reset/health/metrics/requirements).
- `handlers.py` — 4개 기능 로직(이메일 매칭·임시비번 생성·상태포맷·요구사항 적재).
- `config.py` — .env 로딩(아래 env).

의존성: `slack-bolt>=1.18`(slack_sdk 포함), `httpx`(이미 있으면 재사용) → `pyproject.toml [project.optional-dependencies] slack` 그룹.

## 환경변수 (.env 추가)

| 변수 | 용도 |
|------|------|
| `SLACK_BOT_TOKEN` | `xoxb-...` 봇 OAuth 토큰 |
| `SLACK_APP_TOKEN` | `xapp-...` Socket Mode 앱 토큰 |
| `SLACK_ALLOWED_CHANNELS` | (선택) 반응 허용 채널 ID CSV |
| `SLACK_NOTICE_CHANNEL` | (선택) 공지 브로드캐스트 대상. 미설정 시 명령 채널 |
| `LBNOTE_API_BASE` | 기본 `http://127.0.0.1:8088` |
| `JWT_SECRET` | (기존) admin JWT 서명 — 봇이 재사용 |

## Slack 앱 설정 (사용자 수동 1회)

`docs/slack-app-manifest.yml` 로 앱 생성 → Socket Mode ON → 토큰 2개를 `.env` 에.
필요 스코프: `app_mentions:read, chat:write, commands, users:read, users:read.email, im:write`.
이벤트: `app_mention`. 슬래시: `/lbnote`.

## 보안 체크리스트

- [x] 비번은 **채널 미노출**, DM 전용. 임시비번은 난수 16+자.
- [x] 초기화 후 `must_change_password=1`(기존 엔드포인트 보장) → 최초 로그인 강제변경.
- [x] 봇 admin JWT 는 **단명(60초)·요청시 서명**, 저장 안 함.
- [x] 이메일 매칭 실패/미존재 계정 → 초기화 거부(계정 열거 방지 위해 모호한 안내).
- [x] Socket Mode → 인바운드 포트/공인 URL 개방 없음.
- [ ] (운영) 봇 프로세스도 좀비 방지 위해 tini/재시작 정책 적용(웹서버와 동일 이슈).

## 온프레미스 배포 통합 (`deploy/`)

봇은 별도 이미지가 아니라 **웹과 동일 이미지(`meetscript-ai:171`)를 재사용**하되 엔트리포인트만
`python -m src.slack_bot` 으로 교체한 **별도 compose 서비스**로 뜬다.

- `pyproject.toml` `slack` extra → **`uv.lock` 에 slack-bolt 잠금**(빌드 `--frozen` 요건).
- `deploy/Dockerfile`: `uv sync ... --extra cu128 --extra slack` (이미지에 slack-bolt 포함).
- `deploy/docker-compose.yml`: `slackbot` 서비스 신설.
  - `image: meetscript-ai:171`(재빌드 없음), `entrypoint: [python, -m, src.slack_bot]`.
  - `LBNOTE_API_BASE=http://meetscript:8088`(서비스 DNS), `depends_on: [meetscript]`.
  - `restart: unless-stopped`(좀비/크래시 자동 재시작 — 보안 체크리스트의 봇 감시 항목 충족).
  - GPU reservation(`상태` 의 nvidia-smi 라인용, 없어도 봇은 동작).
- `deploy/.env.deploy.example`: `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`(+선택 채널) 항목 추가.
  `JWT_SECRET` 은 웹과 **동일 `.env.deploy` 를 공유**해야 admin JWT 가 호환된다.

기동: `cd deploy && docker compose --env-file .env.deploy up -d --build`
→ `meetscript`(웹)·`slackbot`(봇)·`caddy`(TLS) 세 컨테이너. 봇만 빼려면 서비스 지정 기동
(`docker compose ... up -d meetscript caddy`).

## 미포함(후속)

- 공지 DB/웹 배너(현재 브로드캐스트만).
- 요구사항 Jira 연동(현재 DB 적재만).
- 관리자 승인형 초기화(현재 셀프서비스만).
