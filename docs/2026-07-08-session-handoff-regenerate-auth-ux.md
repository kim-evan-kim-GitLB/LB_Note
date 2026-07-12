# 세션 Handoff - 재요약 배포 미동작 + 재인증 UX (2026-07-08)

컨텍스트 정리 후 이어서 진행하기 위한 인수인계 노트. 이 세션에서 조사/결정한 내용과
다음에 결정/구현할 열린 항목을 정리한다.

> 표기: 명령/코드 블록은 복사용 ASCII.

---

## 0. 이 세션에서 main 에 머지된 것 (배포 대기)

| PR | repo | 내용 |
|---|---|---|
| #27 | LB_Note | 회의록 Docs 요약 렌더 정상화(안건 points 표시) + 제목 날짜/시간 스탬프 |
| #28 | LB_Note | 관리자 사용자 생성/삭제 API(초기 비번 litbig1234, must_change) |
| #21 | LB_Note-web | 관리자 사용자 추가/삭제 UI |
| #29 | LB_Note | 운영 runbook 문서(`docs/2026-07-08-ops-deploy-runbook.md`) |

- dev 서버(:8000)는 새 코드로 재기동됨(PATH 주입, claude_auth ok).
- **배포 컨테이너(:49152)는 아직 재빌드 안 함.** 호스트에서 재빌드 필요(runbook STEP 3 참고).

---

## 1. 배포에서 재요약이 안 되는 이유 = 코드 버그 아님, 자격증명 부재

- 재요약(regenerate)은 최초 요약과 같은 LLM 경로: `summarize_meeting` + `extract_action_items`
  via `agent_cli`(Claude CLI). 배포 `.env` = `WEB_SUMMARIZE_BACKEND=agent_cli`,
  `WEB_EXTRACT_BACKEND=agent_cli`.
- 배포 컨테이너는 헬스에서 `claude_auth: no_credentials`. 사용자별 자격증명 미등록 +
  전역 폴백(`~/.claude/.credentials.json`) 없음 -> `claude -p` 가 "not logged in" 종료 ->
  stderr 가 `_AUTH_MARKERS` 매치 -> `AgentCLIAuthError`.
- dev 의 "claude PATH 못 찾음"과는 다른 문제(그건 PATH, 이건 credentials).
- 근본 해결: (a) 사용자별 Claude 토큰/키 등록, 또는 (b) 컨테이너에 전역 폴백 provision.

근거 파일:
- `src/web/app.py:676-706`  `_run_regenerate_job`(try/except)
- `src/web/app.py:709-729`  regenerate 엔드포인트
- `src/postprocess/backends/agent_cli.py:262-300`  자격증명 주입 + auth 마커 -> AgentCLIAuthError

---

## 2. 예외 처리 / 복구 로직 현황 = 있음(견고)

- 잡 try/except: `AgentCLIAuthError` -> `status=error, error_code="claude_auth_expired"`.
  일반 예외 -> `status=error, error=메시지`. traceback 로깅.
- agent_cli: 타임아웃/비정상 종료 -> 최대 2회 재시도. 인증 에러 -> 즉시 실패(재시도 안 함, 옳음).
- 비파괴 설계(복구 그 자체): 재요약은 미리보기만, DB 미접촉. 실패해도 기존 요약 보존.
  확정(apply)만 백업+교체, undo 로 직전 1회 복원.
- 폴링 `GET /api/ai/jobs/{id}` -> `{status:error, error, error_code}` -> 프론트 `JobError`(error_code 보존).

---

## 3. [열린 항목] 프론트 재인증 안내 UX - 확인 결과 + 개선 제안

### 확인 결과 (구현 가능성 check 완료)
- **이미 처리돼 있고 stderr 노출 없음.** `src/components/ReviewView.tsx:382-387`:
  ```
  if (err instanceof JobError && err.error_code === 'claude_auth_expired') {
    toast("AI 인증이 만료되었습니다. 설정에서 인증을 갱신해 주세요.", 'error');
  } else {
    console.error("Regenerate failed:", err);   // 기술 오류는 콘솔에만
    toast("재요약에 실패했습니다. 잠시 후 다시 시도해 주세요.", 'error');
  }
  ```
- STT 경로도 같은 분기 존재: `FileUploadView.tsx:164`, `RecorderView.tsx:290`.
- error_code 배관 완비: `ai.ts:32-56`(JobError) -> meetingService -> ReviewView.

### 아쉬운 점(개선 여지)
1. 토스트라 사라짐(지속 배너/모달 아님) -> 액션 유도 약함.
2. 설정/Claude 연동 화면으로 가는 직접 버튼 없음(사용자가 직접 찾아가야 함).
3. 문구가 상황과 불일치: 배포는 "만료"가 아니라 "미설정(no_credentials)" -> "AI(Claude) 연동이
   필요합니다" 가 더 정확.

### 제안 (feasibility: 높음, 프론트만)
- `claude_auth_expired`(재요약+STT 공통) 시: **"AI 연동이 필요합니다. [Claude 연동 설정 열기]"**
  버튼형 인라인 안내(클릭 시 설정의 Claude 자격증명 화면으로 이동).
- 재사용 가능: Google 재연동 UX(`ReviewView.tsx:507-509 getIntegrationErrorCode`, 연동 배너 패턴).
- 변경 범위: `ReviewView.tsx`(재요약 핸들러) + 필요시 공용 안내 컴포넌트 1개. LB_Note-web 만.

### >>> 다음 세션에서 사용자에게 받을 결정 <<<
- (A) 액션형 CTA 로 개선(설정 이동 버튼 포함), 또는
- (B) 문구만 정확히 손보는 최소 수정("만료" -> "연동 필요")
- 정하면 LB_Note-web 에 `feature/171-<주제>` 브랜치로 구현 -> PR.

---

## 4. 기타 배경 (참고)
- dev 서버 기동/claude PATH 함정/배포 워크플로는 `docs/2026-07-08-ops-deploy-runbook.md` 참고.
- Google redirect_uri: dev DB app_oauth_config 는 도메인으로 맞음. 도메인<->IP 고정은 미적용(그대로 두기로 함).
- 프론트 배포 문서(DEPLOY.md/DEV_DEPLOY_WORKFLOW.md)는 아직 main 미복구(별건).
