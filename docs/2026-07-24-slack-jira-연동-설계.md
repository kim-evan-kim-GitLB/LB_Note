# Slack / Jira 연동 설계 + 요구사항 봇 대화 개선 (2026-07-24)

회의록 화면 "외부 서비스 연동" 패널의 준비 중 항목(Slack / Jira)을 실구현하기 위한 설계와,
같은 세션에서 함께 반영한 Slack 봇 `요구사항` 대화 개선을 정리한다.

관련 메모리: 브레인스토밍 확정 내용은 세션 메모리(`slack-jira-integration-design`)에 있음.
Jira 티켓: WDLABD2411-589.

---

## 1. 재사용 가능한 기존 자산

- **Slack 봇**(`src/slack_bot/`, 별도 컨테이너): Socket Mode(인바운드 전용) -> 웹앱이 호출할 HTTP
  엔드포인트는 없음. 인증 `SLACK_BOT_TOKEN`(xoxb) + `SLACK_APP_TOKEN`(xapp). 이미
  `chat.postMessage` 사용(비번 DM / 공지 브로드캐스트). 스코프 `chat:write`, `users:read.email`,
  `im:write` 보유, `files:write` 미보유.
- **Google 연동**(`src/web/google_*.py`): provider별 OAuth 모듈 + per-user Fernet 토큰 테이블 +
  4-엔드포인트(status / connect / callback / DELETE) + `error_code` 계약 + 비동기잡 vs 동기액션.
  Slack/Jira 도 이 레이어링을 그대로 미러링한다.
- **프론트 프로토타입**(`integrationService.ts`): `pushToSlack`(webhook URL을 localStorage 저장),
  `pushToJira`(스텁). 토큰을 브라우저에 두는 방식이라 **폐기하고 서버측 저장으로 이관**한다.

## 2. 핵심 데이터 제약

- `ActionItem.owner`는 "팀/부서" 라벨(`string | null`) = 개인 사용자가 아님 ->
  **담당자(owner) 개인 DM 불가**. 개인 DM 을 하려면 owner 를 명부 사용자(이메일)로 연결하는
  데이터모델 확장이 선행돼야 함(후속).
- **참석자**(`participants: Speaker[]`)와 **명부(directory)**는 이메일 보유 -> Slack DM 가능.
  Gmail 발송이 이미 명부 이메일 피커를 사용 중 -> 그대로 재사용.

## 3. Slack 연동 = 참석자 회의록 DM (채널 브로드캐스트 아님)

- **인증/실행**: 기존 봇앱 재사용. 웹앱(FastAPI)에 `slack_notify.py` 추가, 워크스페이스
  `SLACK_BOT_TOKEN`으로 자체 Slack 클라이언트(봇 프로세스와 별개, 같은 토큰). Gmail 발송과
  동일한 **동기 액션 엔드포인트**.
- **DM 메커니즘**(이메일이 다리, 이메일 1건당 3~4단계):
  1. `users.lookupByEmail(email)` -> Slack 유저 ID  (`users:read.email` 보유)
  2. `conversations.open(users=[id])` -> DM 채널 ID  (`im:write` 보유)
  3. 텍스트: `chat.postMessage(channel=dm, blocks=...)`  (`chat:write` 보유)
  4. PDF: `files_upload_v2(channel=dm, file=pdf)`  (`files:write` **추가 필요** + 앱 재설치)
- **엔드포인트**: `POST /api/meetings/{id}/slack-dm {recipients:[email], attachPdf}` ->
  수신자별 결과 `{sent | no_email | not_found}` 반환(부분 실패 처리 필수).
- **수신자 선택**: 발송 시 ReviewView 기존 명부/이메일 피커 재사용.
- **내용**: 텍스트 블록(제목/요약/액션아이템) + PDF 회의록(둘 다).
- **v1 범위**: 참석자 DM만. 담당자 개인 DM 은 후속(위 owner 제약).
- **구현 시 확인**: PDF 생성이 현재 Google Drive export 에만 의존 -> Slack 은 Google 비의존
  **로컬 PDF 생성 경로** 필요(`meeting_doc.py` 재사용 검토).

## 4. Jira 연동 = 액션아이템 1:1 이슈

- **인증**: API 토큰(Basic: email + api_token), 서버측 Fernet 저장(google_credentials 패턴).
  **관리자 레벨 설정**(단일 사이트 전제): site URL / project key / issue type.
- **엔드포인트**: `POST /api/meetings/{id}/jira-issues` -> 액션아이템마다
  `POST /rest/api/3/issue`(summary=액션텍스트, description=회의링크+근거, duedate=dueDate) ->
  생성 이슈키를 액션아이템에 기록(재실행 시 중복 생성 방지).
- **owner**는 description/라벨로. **assignee 생략**(Jira Cloud 는 accountId 필요 -> 후속).

## 5. 단계

1. Slack 참석자 텍스트 DM (기존 스코프)
2. Slack PDF 첨부 (`files:write` 추가 + 앱 재설치)
3. Jira 액션아이템 이슈 생성 (관리자 설정)
4. 후속: 담당자 개인 DM(owner->명부 연결), Jira assignee(accountId), 상태 역동기화

---

## 부록. 이번 세션에서 함께 반영된 봇 변경 (구현 완료)

### A. `요구사항` 스레드(댓글) 되묻기 대화

기존 `요구사항 <내용>` 한 방(one-shot) 트리거를 스레드 기반 되묻기 대화로 교체.

```
사용자: @LBNoteBot 요구사항
봇(스레드): 입력해주시면 제가 저장할게요.
사용자(스레드): <내용>
봇(스레드): 저장이 완료되었어요. 추가적으로 입력하고 싶으신게 있으신가요?
사용자(스레드): 예            <- 정확히 '예' 일 때만 계속
봇(스레드): 입력해주시면 제가 저장할게요.   (반복)
사용자(스레드): (그 외 아무거나)  -> 요구사항 접수를 종료합니다.
```

- 상태머신: `src/slack_bot/conversation.py` — `(channel, thread_ts)` 키 인메모리, 5분 TTL,
  Lock 보호. 상태 `awaiting_text` / `awaiting_more`.
- 로직: `handlers.requirement_start` / `requirement_reply`(순수 함수, slack_bolt 미의존 -> 단위 테스트).
- 라우팅: `bot.py` — 새 멘션은 그 메시지 ts 를 스레드 루트로, 스레드 답글은 대화 진행.
  `message` 이벤트 핸들러로 @멘션 없는 답글도 수신(폴백으로 @멘션 답글도 지원).
- 규칙: 정확히 `예` 만 계속(공백 trim), 그 외/무응답(TTL)/타인 답글은 종료·무시.
  저장 실패 시 상태 유지(재입력 가능). 슬래시(`/lbnote 요구사항`)는 스레드 불가 -> 한 방 저장 폴백.

> Slack 앱 설정 변경(수동 1회): 매니페스트에 `message.channels` / `message.groups` / `message.im`
> 이벤트 + `channels:history` / `groups:history` / `im:history` 스코프 추가 후 **앱 재설치**.
> (`docs/slack-app-manifest.yml` 갱신됨.) 재설치 전에는 @멘션으로 이어가는 폴백만 동작.

### B. 요구사항 저장 권한 정리 (admin 위조 제거)

- 문제: `POST /api/requirements` 가 `require_admin` 이라, 봇이 요구사항마다 `sub="admin"`
  admin 토큰을 위조했다. -> (1) 사용자 건의가 admin 권한으로 기록되는 권한 불일치,
  (2) username 이 정확히 `admin` 인 계정이 없거나 `must_change_password=1` 이면 저장이
  403 으로 조용히 실패.
- 수정: 요구사항 인테이크 전용 스코프 도입.
  - `auth.require_requirement_writer` — 봇 intake 스코프 토큰(`scope='requirement_intake'`,
    서명만 검증·DB 조회 없음) **또는** 관리자 세션 허용. 조회/관리(list/patch)는 여전히 admin.
  - 봇 `lbnote_client` 는 admin 대신 intake 스코프 토큰을 발급(`_intake_token`).
  - `lbnote_client._request` 는 `URLError`(서버 다운/타임아웃)도 `LBNoteError` 로 감싸 일관 처리.

### 검증

- 신규 테스트: `tests/test_slack_requirement_convo.py`(대화 9케이스),
  `tests/test_requirement_intake_auth.py`(인가 5케이스). 전체 스위트 **320 통과**, ruff 클린.
