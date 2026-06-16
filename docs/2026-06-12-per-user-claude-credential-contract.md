# 사용자별 claude 자격증명 API 계약 (per-user claude credential)

작성일: 2026-06-12 / 대상: meetscript-ai 프론트(별도 repo) 설정 화면 핸드오프용.

## 배경

agent_cli 백엔드(`claude -p`)로 요약/추출을 수행할 때, 종전엔 서버 전역 1개 자격증명
(evan 의 `~/.claude` OAuth)만 사용했다. 이제 **웹 사용자별로** API 키 또는 구독 토큰을
설정해 각자 자기 인증으로 claude 를 호출할 수 있다. 미설정 사용자는 기존 전역 OAuth 폴백.

- 자격증명은 서버 SQLite `claude_credentials` 테이블에 평문 보관(현 .env/~/.claude 수준).
- secret(키/토큰 원문)은 **어떤 API 응답에도 포함되지 않는다.** 상태 조회는 설정 여부/종류/
  갱신시각만 노출.
- 모든 엔드포인트는 `auth.require_user`(Bearer JWT) 보호 → 자기 자격증명만 조회/변경.

## 자격증명 종류

| cred_type     | 주입 방식                                          | 비고 |
|---------------|----------------------------------------------------|------|
| `api_key`     | subprocess env `ANTHROPIC_API_KEY` + argv `--bare` | 만료 없음. OAuth/keychain 무시(완전 격리). |
| `oauth_token` | subprocess env `CLAUDE_CODE_OAUTH_TOKEN`           | 구독 토큰. 토큰 우선이라 HOME 교정 불필요. |
| (미설정)      | 전역 OAuth + HOME 교정(SUDO_USER 홈)               | 기존 동작 폴백. |

## 엔드포인트

모든 요청에 `Authorization: Bearer <JWT>` 필요(프론트 axios 인터셉터가 이미 주입).

### 1) GET /api/settings/claude-credential

현재 사용자 자격증명 상태(secret 비노출).

응답 200:

```json
{ "configured": true, "type": "api_key", "updated_at": "2026-06-12T14:03:21" }
```

미설정 시:

```json
{ "configured": false, "type": null, "updated_at": null }
```

### 2) PUT /api/settings/claude-credential

자격증명 저장 + 저장 직후 가벼운 검증 호출(claude 로 "ping" 1콜)로 유효성 확인.
검증이 실패해도 **저장은 유지**되고 `verification.ok=false` 로 알려준다.

요청 body:

```json
{ "cred_type": "api_key", "secret": "sk-ant-..." }
```

`cred_type` 은 `"api_key"` 또는 `"oauth_token"`. 그 외 값/빈 secret 은 400.

응답 200(검증 성공):

```json
{
  "status": { "configured": true, "type": "api_key", "updated_at": "2026-06-12T14:03:21" },
  "verification": { "ok": true, "detail": "검증 호출 성공" }
}
```

응답 200(저장은 됐으나 검증 실패 — 잘못된 키/만료 토큰 등):

```json
{
  "status": { "configured": true, "type": "oauth_token", "updated_at": "2026-06-12T14:05:10" },
  "verification": { "ok": false, "detail": "인증 실패: claude 구독 인증이 만료되었거나 ..." }
}
```

응답 400(잘못된 입력):

```json
{ "detail": "알 수 없는 cred_type: 'foo' (지원: api_key, oauth_token)" }
```

> 검증 호출은 claude CLI 가 서버에 설치돼 있을 때만 의미가 있다. CLI 미설치/타임아웃이면
> `verification.ok=false, detail="검증 호출 실패: ..."` 로 내려가며, 저장 자체는 유지된다.

### 3) DELETE /api/settings/claude-credential

현재 사용자 자격증명 삭제 → 전역 OAuth 폴백으로 복귀.

응답 200:

```json
{ "ok": true, "cleared": true, "status": { "configured": false, "type": null, "updated_at": null } }
```

`cleared` 는 실제로 삭제된 행이 있었는지(이미 미설정이면 false).

## 프론트 설정 UI 가이드

설정(Settings) 화면에 "claude 자격증명" 섹션을 둔다.

1. **진입 시** `GET /api/settings/claude-credential` 로 현재 상태를 읽어 표시.
   - `configured=false` → "설정 안 됨(서버 기본 인증 사용)" 안내.
   - `configured=true` → "설정됨 · 종류: API 키 / 구독 토큰 · 갱신: {updated_at}" 표시.
     secret 은 서버가 절대 돌려주지 않으므로 마스킹된 placeholder(예: `••••••••`)만 보여준다.
2. **종류 선택**: 라디오 2개 — `API 키`(api_key) / `구독 토큰`(oauth_token).
   - API 키: Anthropic 콘솔의 `sk-ant-...` 키. 만료 없음, 완전 격리.
   - 구독 토큰: `claude setup-token` 등으로 발급한 `CLAUDE_CODE_OAUTH_TOKEN`.
3. **secret 입력**: password 타입 input(화면 표시 마스킹). 붙여넣기 허용.
4. **저장**: `PUT` 호출 → 응답의 `verification.ok`/`detail` 을 토스트/배지로 표시.
   - `ok=true` → "저장 및 검증 완료" 초록 배지.
   - `ok=false` → "저장됨(검증 실패: {detail})" 주황 경고 — 사용자가 키/토큰을 다시 확인하게.
   - 저장 후 `status` 로 상태 영역 갱신.
5. **삭제**: "기본 인증으로 되돌리기" 버튼 → `DELETE` 호출 → 상태 갱신.

### 처리 흐름 주의

- `/api/ai/process` · `/api/ai/extract-actions` 는 호출한 사용자의 자격증명을 자동으로
  주입한다(프론트가 별도 헤더를 보낼 필요 없음 — JWT 의 사용자로 서버가 조회).
- 잡 처리 중 claude 인증이 끊기면 잡 폴링 응답이 `status="error",
  error_code="claude_auth_expired"` 로 내려온다 → 프론트는 "claude 자격증명을 확인/갱신
  하세요" 안내 + 위 설정 화면으로 유도.
- `/api/health` 의 `claude_auth` 는 **서버 전역** 상태다(사용자별 아님). 사용자별 상태는 위
  `GET /api/settings/claude-credential` 로 확인한다.
```
