# LB NOTE Phase 4 — 운영 안정화 설계 (2026-07-27)

- 상위 에픽: AX-1 (AX-TF)
- 선행: Phase 3 `WDLABD2411-589` 외부 앱 연동 완성 및 실환경 검증 (완료, 2026-07-24)
- 이 문서의 범위: Phase 4 를 "운영 안정화"로 재정의하고 작업 항목·완료 조건·일정을 확정한다.

---

## 0. 범위 재정의 — 왜 "보안 강화"가 아니라 "운영 안정화"인가

Phase 3 티켓 본문은 Phase 4~5 를 "보안 강화(HTTPS·자격증명 암호화)와 온프레미스 정식 배포"로
예고했다. 그러나 그 보안 항목들은 **Phase 3 기간 중 이미 코드로 해소**됐다(아래 증거).

| 예고된 Phase 4 항목 | 현 상태 | 증거 |
|---|---|---|
| HTTPS(평문 전송 차단) | 적용됨 | `deploy/Caddyfile` — Caddy TLS 종단, IP SAN self-signed, `:49152 -> 443` |
| 자격증명 at-rest 암호화 | 적용됨 | `src/web/auth.py:40` `CRED_ENC_KEY` Fernet, Jira api_token·OAuth secret 도 동일 |
| 공용 약비번 강제 변경 | 적용됨 | `src/web/auth.py:103` `must_change_password` DEFAULT 1, 재설정 경로도 1 강제 |

남은 것은 보안 설계가 아니라 **무인 운영 내구성**이다. 정식 배포(Phase 5)를 하려면 사람이
붙어 있지 않아도 되는 상태가 선행돼야 한다. 그래서 Phase 4 = 운영 안정화로 잡는다.

**신규 기능은 동결한다.** Slack 참석자 DM(설계 완료·미구현), Jira 상태 역동기화, owner->명부
연결은 Phase 5 또는 별도 트랙으로 미룬다. 단 하나의 예외는 T2(운영 알림)인데, 이는 기능이
아니라 감지 결과를 사람에게 전달하는 운영 배관이다.

---

## 1. 목표

1. 라이브(`:49152`)를 `main` 과 동기화하고, 그 상태를 재현 가능한 절차로 고정한다.
2. 이미 갖춘 **감지**(워치독·헬스 스윕·백업)에 **알림**과 **복구 검증**을 붙여,
   조용한 실패(silent failure)가 사람에게 도달하게 만든다.
3. 무인 방치 시 시스템을 망가뜨리는 누적형 리스크(로그·디스크·백업 단일 지점)를 제거한다.

---

## 2. 현 상태 인벤토리 (증거 기반)

이미 구현돼 있어 **Phase 4 에서 다시 만들지 않는다**. 라이브 반영·검증만 한다.

| 자산 | 위치 | 비고 |
|---|---|---|
| DB 스냅샷 백업 | `maintenance.run_db_backup`, `app.py:100` | 기본 ON, 1일 주기, 최근 7개 보존 |
| STT 스톨 워치독 | `app.py:207` `_stt_stall_watchdog_loop`, `_scan_stt_stalls` | `warning=stt_stalled`, `STT_STALL_MARK_ERROR` 옵션 |
| AI 잡 진단 | `GET /api/admin/ai-jobs` | phase/queue/reasonHint 노출 |
| claude 자격증명 헬스 | `GET /api/admin/claude-credential-health`, 6h 스윕 | `decrypt_failed` 구분 |
| 자격증명 실시간 재검증 | `GET /api/settings/claude-credential/verify` | 사용자 단위 |
| staging/백업 정리 | `maintenance.run_cleanup_once` | staging 1일, 백업 7일 |
| 디스크 사용량 조회 | `maintenance.disk_usage()`, admin 진단 | **조회만** — 알람 없음 |
| 헬스 엔드포인트 | `GET /api/health` | `ok`, `claude_auth` 등 |
| 컨테이너 헬스체크 | `deploy/docker-compose.yml:62` | 30s 간격, curl `/api/health` |
| 좀비 방지 | compose `init: true` (meetscript·slackbot) | PID1 = tini |
| 감사 로그 | `observability.audit`, `WEB_AUDIT_LOG` | stdout + 선택적 파일 |
| 오디오 Range 스트리밍 | `app.py:1873~` | 206 부분요청, 단기 토큰 |
| 재요약 편집 보존 | `merge_preserve_edited` | `preserve_edited` 모드 |

---

## 3. 갭 — Phase 4 가 메울 것

| ID | 갭 | 왜 문제인가 | 근거 |
|---|---|---|---|
| G1 | 라이브-main 드리프트 | P2 UX·신규기능(캘린더/Docs/네비)·Jira 연동이 `main` 에만 있고 라이브 미반영. 배포 간격이 길수록 롤백 단위가 커진다 | 배포는 호스트 수동(컨테이너에 호스트 SSH 키 미등록) |
| G2 | 감지는 되는데 알리지 않음 | `stt_stalled`·자격증명 invalid·백업 실패가 audit 로그와 admin 화면에만 남는다. 관리자가 들여다볼 때까지 아무도 모른다 | `observability.py` 에 알림 경로 없음, 웹앱에 Slack 발신 모듈 없음(`src/web/` 에 slack 파일 부재) |
| G3 | 백업 단일 지점·복구 미검증 | 스냅샷이 DB 와 **같은 볼륨**(`DATA_DIR`)에 쌓인다. 볼륨/디스크 사고 시 원본과 함께 소실. 복원 절차가 실행된 적 없음 | `maintenance.db_backup_dir(store)`, compose `DATA_DIR:/app/output` |
| G4 | 로그 무한 증식 | compose 에 `logging:` 설정이 없어 docker json-file 기본(무제한). `WEB_AUDIT_LOG` 파일도 로테이션 없음 | `deploy/docker-compose.yml` 에 `max-size` 없음, `observability._ensure_handler` 는 `FileHandler` |
| G5 | 회귀 자동화 부재 | 테스트 39개 파일이 있으나 로컬 수동 실행. 배포 전 회귀 게이트가 사람 기억에 의존 | `.github/workflows/` 없음 |
| G6 | 디스크 임계 알람 없음 | 오디오 원본·모델·백업이 누적되는데 임계 도달을 사전에 알 방법이 없다 | `disk_usage()` 는 진단 응답용, 임계 판정 로직 없음 |
| G7 | 배포 후 스모크 절차 암묵 | 재빌드 후 무엇을 확인해야 "정상"인지 문서화된 체크리스트가 없다 | `docs/2026-07-08-ops-deploy-runbook.md` 는 명령 위주, 검증 항목 미정의 |

---

## 4. 작업 항목

우선순위: **P0 = Phase 5(정식 배포) 진입 차단 요인**, P1 = 무인 운영 필수, P2 = 개선.

### T1. 라이브 동기 배포 + 스모크 체크리스트 (P0, G1/G7)

- 호스트에서 `LB_Note-deploy`/`LB_Note-web` 최신화 후 `docker compose --env-file .env.deploy up -d --build`.
- 배포 직후 확인 항목을 **체크리스트로 고정**해 runbook 에 추가한다.
  1. `docker compose ps` 전 서비스 healthy
  2. `GET /api/health` -> `ok:true`, `claude_auth` 상태 기록(배포본 자격증명은 사용자별 등록 필요)
  3. 로그인 -> 회의 1건 업로드 -> STT -> 요약/액션 생성까지 end-to-end 1회
  4. Google 연동 3종 상태 표시 정상, Jira 등록 다이얼로그가 기본 프로젝트로 열림(PR #51 반영 확인)
  5. `GET /api/admin/ai-jobs`, `GET /api/admin/claude-credential-health` 응답 확인
- **완료 조건**: 라이브 `git rev-parse HEAD` == `origin/main`, 위 5항목 전부 통과 로그 확보.

### T2. 운영 알림 채널 (P0, G2)

- 웹앱에 **발신 전용 Slack 모듈**(`src/web/slack_notify.py`)을 신설한다. 워크스페이스
  `SLACK_BOT_TOKEN` 재사용, `chat.postMessage` 만 사용(보유 스코프 `chat:write`).
- 알림 대상 이벤트(최소 집합):
  | 이벤트 | 트리거 | 심각도 |
  |---|---|---|
  | `stt_stalled` | 스톨 워치독 스캔이 신규 스톨 마킹 | WARN |
  | `cred_invalid` | 자격증명 스윕에서 invalid/`decrypt_failed` 신규 발생 | WARN |
  | `backup_failed` | `run_db_backup` 예외 또는 스냅샷 미생성 | ERROR |
  | `disk_high` | T5 워터마크 초과 | WARN/ERROR |
  | `health_degraded` | `/api/health` 비정상 상태 전이 | ERROR |
- **중복 억제 필수**: 동일 이벤트 키는 쿨다운(기본 1시간) 내 1회만 발송. 스톨 스캔이 30초
  주기이므로 억제 없이는 알림 폭주가 된다.
- 대상 채널/DM 은 env(`OPS_ALERT_SLACK_CHANNEL`)로 지정, 미설정 시 무동작(기존 동작 불변).
- 이 모듈은 Phase 5 의 "참석자 회의록 DM" 이 그대로 얹힐 토대가 된다(`conversations.open` +
  `files_upload_v2` 만 추가하면 됨).
- **완료 조건**: 5개 이벤트를 인위적으로 유발해 Slack 수신 확인, 쿨다운 동작 확인, 미설정 시
  기존 동작 무변화 확인.

### T3. 백업 이중화 + 복구 리허설 (P0, G3)

- 스냅샷 사본을 **DATA_DIR 밖 별도 경로**(`WEB_DB_BACKUP_MIRROR_DIR`, 예: 호스트의 다른 마운트)
  에 1부 더 둔다. 미설정 시 현행 동작 유지.
- 백업 결과를 audit 이벤트로 남기고 실패 시 T2 로 알린다.
- **복구 리허설**: 스냅샷 1개를 임시 경로에 복원해 기동 -> 로그인·회의 목록 조회까지 확인하는
  절차를 runbook 에 기록하고 1회 실제 수행한다(라이브 DB 는 건드리지 않는다).
- **완료 조건**: 미러 경로에 사본 생성 확인, 복구 리허설 로그 확보, 백업 실패 알림 1회 검증.

### T4. 로그 로테이션 (P1, G4)

- compose 각 서비스에 `logging: {driver: json-file, options: {max-size: 50m, max-file: 5}}` 추가.
- `WEB_AUDIT_LOG` 파일 핸들러를 `RotatingFileHandler`(예: 50MB x 5)로 교체.
- **완료 조건**: 설정 반영 후 로그 파일 상한 동작 확인, 기존 audit 포맷 불변.

### T5. 디스크 임계 워터마크 (P1, G6)

- `maintenance` 에 임계 판정 추가: WARN 80% / ERROR 90%(env 조정 가능).
- 주기 스캔(기존 cleanup 루프에 편승)에서 임계 초과 시 T2 알림 + audit.
- 오디오 원본 보존 정책을 명시한다(Drive 업로드 완료분의 로컬 원본 정리 기준 재확인).
- **완료 조건**: 임계 강제 하향으로 알림 발화 검증, 보존 정책 문서화.

### T6. CI 회귀 게이트 (P1, G5)

- `.github/workflows/ci.yml`: push/PR 에서 `ruff check` + `pytest`(GPU·모델 비의존 테스트만).
- 모델 가중치·CUDA 가 필요한 테스트는 마커로 분리해 CI 에서 제외한다.

**[제약] 이 항목은 다른 T 보다 비용이 크다 — 착수 전 아래를 먼저 처리한다.**

- `src.web.app` 을 임포트하면 **torch 가 함께 로드된다**(측정 확인). 즉 웹 테스트만 돌리려 해도
  러너에 torch 설치가 필요해 매 PR 마다 수 GB 다운로드·수 분 소요가 붙는다.
- 대응은 둘 중 하나를 택한다.
  1. **의존성 격리(권장)**: 웹 계층이 STT 백엔드를 지연 임포트하도록 정리해 torch 없이
     임포트 가능하게 만든다. 근본 해결이지만 별도 리팩터 공수가 든다.
  2. **CPU-only torch 설치**: 러너에 CPU 휠만 설치. 간단하나 캐시 없이는 느리다.
     `uv sync --extra cu128` 은 작동 중인 torch 를 깨뜨린 이력이 있으므로 CI 의존성 설치는
     **로컬 개발 환경과 분리된 경로**로 구성한다.
- 사내 코드를 GitHub 클라우드 러너에서 실행하는 것에 대한 정책 확인이 선행돼야 한다.
  불가 판정이면 **로컬 pre-push 훅**(ruff + pytest)으로 대체하고 T6 를 종료한다.
- **완료 조건**: PR 에서 체크가 돌고 실패 시 머지 차단 상태 확인. 정책상 클라우드 CI 불가 시
  pre-push 훅 동작 확인으로 대체하고 그 판단 근거를 문서에 남긴다.

### T7. 운영 런북 갱신 (P2, G7)

- `docs/2026-07-08-ops-deploy-runbook.md` 에 배포 스모크 체크리스트(T1), 복구 절차(T3),
  알림 이벤트 사전(T2), 임계값 표(T5)를 반영한다.
- **완료 조건**: 런북만 보고 제3자가 배포-검증-복구를 수행 가능.

---

## 5. Phase 4 완료 게이트

1. 라이브 = `main` 동기, 스모크 5항목 통과 로그 확보.
2. 5종 운영 알림이 Slack 으로 도달하고 중복 억제가 동작.
3. 백업 사본이 DATA_DIR 밖에 존재하고, 복구 리허설 1회 성공 기록 존재.
4. 로그·디스크 상한이 설정돼 무인 방치 시 증식하지 않음.
5. CI 가 PR 회귀 게이트로 동작.
6. 런북이 위 절차를 모두 담고 있음.

---

## 6. 비범위 (Phase 5 또는 별도 트랙)

- Slack 참석자 회의록 DM(텍스트/PDF), `files:write` 스코프 추가·앱 재설치
- Jira 상태 역동기화, assignee(accountId) 지정, owner->명부 연결
- 다중 사용자 동시성 확장, 외부 모니터링 스택(Prometheus 등) 도입
- 공인 인증서/도메인 전환(현재 폐쇄망 self-signed 유지)

---

## 7. 일정 및 공수

- 시작 2026-07-28 / 종료 2026-08-14 / 예상 3주
- 주차 배분
  - 1주차(07-28~08-01): **T1** 라이브 동기 배포 + 스모크 체크리스트. 회귀 수습 여유 포함.
  - 2주차(08-03~08-07): **T2** 운영 알림, **T3** 백업 이중화 + 복구 리허설.
  - 3주차(08-10~08-14): **T4** 로그, **T5** 디스크, **T6** CI, **T7** Runbook.
- **왜 2주가 아니라 3주인가**: Phase 3 는 공수 2w 로 잡았으나 실제 기록된 timeSpent 는 1d 5h 였다.
  "N주"는 캘린더 기간이지 풀타임 가용 시간이 아니다. 여기에 (a) T1 의 회귀 수습 비용이 미지수이고
  (b) T6 가 torch 의존 때문에 다른 항목보다 무겁다는 점을 반영해 3주로 잡는다.
- 전제: 호스트 배포는 사용자 수동 수행(컨테이너에 호스트 SSH 키 미등록) — 왕복 지연을 일정에 포함.
- 종료일은 08-15(광복절·토)를 피해 08-14(금)로 둔다.

---

## 8. 리스크

| 리스크 | 영향 | 완화 |
|---|---|---|
| 알림 폭주 | Slack 소음으로 알림 자체가 무시됨 | 쿨다운·이벤트 키 중복 억제를 T2 필수 요건으로 못박음 |
| 배포 중 라이브 중단 | 사용자 사용 시간대 충돌 | 업무 시간 외 배포, 롤백 커밋 사전 확보 |
| 복구 리허설이 운영 DB 오염 | 데이터 손상 | 임시 경로·별도 포트 기동, 라이브 볼륨 미마운트 |
| CI 가 torch 의존으로 무거워짐 | 매 PR 수 GB 설치·수 분 지연, 최악은 게이트 방치 | T6 제약 절 참조 — 의존성 격리 우선, 불가 시 CPU-only 휠, 정책 불가 시 pre-push 훅으로 대체 |
| 미배포 누적분이 커서 회귀 위험 | 배포 후 다발 장애 | T1 을 최우선, 스모크 체크리스트로 즉시 검증. 1주차 전체를 T1 에 배정 |
| 우선순위 근거가 코드 관측뿐 | T2/T5 임계값이 실제와 어긋남 | T1 수행 시 라이브 최근 30일 회의 건수·STT 실패·자격증명 만료 횟수를 함께 수집해 T2/T5 임계값을 보정 |
