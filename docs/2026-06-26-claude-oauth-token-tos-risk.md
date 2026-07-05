# Claude Code CLI 토큰(OAuth) 인증 — 향후 지원·계정 정지 리스크 검토

작성일: 2026-06-26
관련: 피드백 기능 Claude 인증 oauth 단일화(WDLABD2411-543), LB_Note #18 / web #11

## 배경

피드백 기능에서 Claude 인증을 `oauth_token`(Claude Code CLI 토큰) 단일화로 확정한 뒤,
"CLI 토큰 방식이 향후 지원되지 않을 수 있다 / 해당 방식 사용 시 계정이 정지될 수 있다"는
이야기가 있어 공식 문서·약관 변경을 교차 확인했다.

## 1. 현재 인증 구조 (사실 확인)

- 백엔드는 토큰을 **raw Anthropic API 로 직접 호출하지 않는다.** 사용자별 자격증명을
  **공식 `claude -p`(Claude Code 바이너리)** 의 환경변수(`CLAUDE_CODE_OAUTH_TOKEN`)로 주입해
  헤드리스 실행한다. (`src/postprocess/backends/agent_cli.py:257-260`)
- 자격증명은 **사용자별 본인 것**을 각자 등록 — 단일 계정 공유 구조가 아니다.
- `api_key`(Console API 키)는 프론트 UI 에서만 제거됐고, 백엔드는 더미/후방호환으로 유지된다.
  (`src/web/auth.py:116-121`)

위 두 가지(공식 바이너리 사용 + 사용자별 토큰)가 아래 리스크 판정의 **완화 요인**이다.

## 2. "향후 지원 중단" 여부 — 절반만 사실 (구분 필요)

| 구분 | 상태 |
|---|---|
| `claude setup-token` / `CLAUDE_CODE_OAUTH_TOKEN` 자체 | 현존·지원 중. 공식 인증 문서에 "CI 파이프라인·스크립트용"으로 명시. deprecated 아님. inference 전용으로 스코프됨 |
| **서드파티 도구**에서 구독 OAuth 토큰 사용 | 금지됨 (2026-02 약관 개정, 2026-04-04 시행). OpenClaw 등 비공식 하니스가 표적 |

즉 "토큰 방식이 사라진다"가 아니라, **"공식 Claude Code 바이너리가 아닌 제3자 도구에서 구독
토큰을 쓰는 것"이 금지**됐다. 약관 문구(인용): *"Free/Pro/Max 계정으로 얻은 OAuth 토큰을 다른
product, tool, service 에서 사용하는 것은 허용되지 않는다."*

## 3. 계정 정지 리스크 — 실재함

- **Consumer Terms §13**: 중대한 위반·보안 우려 시 **사전 통보 없이 계정 정지 가능.**
  실제로 2026년 1~4월 사이 무경고 비활성화 사례 다수 보고됨.
- 공식 문서 명시: *"공식 Claude Code 바이너리를 로컬/원격/SSH 어디서 돌리든 약관 내(within ToS)."*
  → 반면 *"always-on AI 어시스턴트/서비스를 만든다면 API key 가 필요."*

### 우리 프로젝트의 위치 = 회색지대

| 안전 쪽 신호 | 리스크 쪽 신호 |
|---|---|
| 공식 `claude` 바이너리 호출 (API 재구현 아님) | 다중 사용자 **웹 서비스 백엔드로 자동화** → "product/service" 해석 여지 |
| 사용자별 본인 토큰 (계정 공유 아님) | "always-on assistant 는 API key 필요"라는 공식 신호에 근접 |
| 온프레미스 (재판매·외부노출 없음) | inference-only 스코프지만 자동화 호출 패턴 |

순수하게 "공식 바이너리를 스크립트로 호출"하는 부분은 문서가 명시적으로 허용하는 형태라
**즉각 정지 대상은 아니다.** 다만 이를 **자동화된 서비스로 감싼다**는 점이 약관 해석상 완전히
안전하다고 보장할 수 없다.

## 4. 권고 — 주목할 반전

> 이번에 "API 방식을 프론트에서 빼고 oauth 로 단일화"했는데, **약관 관점에서는 오히려 Console
> API key 방식이 자동화 서비스의 정식 경로**다. 즉 방금 비활성화한 `api_key` 경로(더미로 남겨둔
> 게 다행)가 ToS상 더 안전하다.

선택지:

1. **(가장 안전) Console API key / Claude for Teams 로 운영 경로 전환** — 자동화 서비스용으로
   약관상 정식. 더미로 남겨둔 `api_key` 인프라를 되살리면 된다. 사내/팀 단위면 Teams·Enterprise
   seat 가 정답.
2. **(현행 유지 + 리스크 수용)** oauth_token 단일화 유지하되, 각 사용자가 *본인 구독·본인 책임*으로
   본인 토큰을 등록하는 구조임을 명시. 공식 바이너리 사용이라 즉시 정지 가능성은 낮으나 약관
   회색지대임을 인지.
3. **(혼합)** UI 는 oauth 를 기본 유지하되 `api_key`(Console) 경로를 관리자/팀 용으로 다시 노출 —
   운영 안정성이 필요한 자동 정제는 API key, 개인 사용은 oauth.

운영 안정성·약관 안전성 기준으로 **1번(또는 3번) 권장.** 단, 방금 머지한 결정을 일부 되돌리는
사안이라 방향 확정 후 진행한다.

## 참고 자료

- [Authentication — Claude Code Docs (공식)](https://code.claude.com/docs/en/authentication)
- [Anthropic Officially Ends Claude Subscriptions for Third-Party Tools (Cryptika)](https://www.cryptika.com/anthropic-officially-ends-claude-subscriptions-for-third-party-tools-like-openclaw/)
- [Anthropic Banned Third-Party Claude Auth: Full Guide 2026 (Kersai)](https://kersai.com/anthropic-killed-third-party-claude-access-heres-every-workaround-that-still-works/)
- [Claude Code Account Suspended? How to Stay Safe 2026 (autonomee.ai)](https://autonomee.ai/blog/claude-code-account-suspended-banned-safe-usage/)
- [Claude Code on Claude Max: OAuth Token vs API Key 2026 (Medium)](https://lalatenduswain.medium.com/claude-code-on-claude-max-plan-understanding-oauth-token-vs-api-key-authentication-in-2026-96a6213d2cde)
