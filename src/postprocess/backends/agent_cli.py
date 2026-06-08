"""헤드리스 에이전트 CLI 백엔드 (REAL).

인-세션 핸드오프 모드(src/postprocess/handoff.py 의 emit/collect)의 **자동화 졸업본**.
핸드오프 모드는 사람/세션 에이전트가 work-order 의 cleaned 를 수동으로 채우지만, 이
백엔드는 그 정제를 segment 단위로 **에이전트 CLI 를 헤드리스로 호출**해 자동 수행한다.

기본 경로는 `claude -p`(비대화 print 모드). system 규칙은 `--append-system-prompt`,
정제 대상 본문은 위치인자로 준다. 모델은 AGENT_CLI_MODEL(기본 sonnet) 로 핀.

**인증 주의:** `--bare` 는 ANTHROPIC_API_KEY/apiKeyHelper 만 쓰고 OAuth/keychain 을
읽지 않는다 → 구독(OAuth) 인증 환경에선 "Not logged in" 으로 실패. 그래서 `--bare` 를
쓰지 않고, 대신 오케스트레이션 노이즈/지연을 차단하기 위해 ① 서브프로세스 env 에
DISABLE_OMC=1·OMC_SKIP_HOOKS=1 ② `--disable-slash-commands`(스킬 트리거 차단)
③ 중립 cwd(임시 디렉터리)에서 실행(프로젝트 CLAUDE.md 자동탐색 회피)을 적용한다.

**경계(중요):** claude/codex 경유는 내용을 Anthropic/OpenAI 클라우드로 보낸다 →
온프렘/PII 전제와 충돌. 따라서 이 백엔드는 **품질 게이트·벤치마크 전용**이며(설계 §8
졸업 판정용), 실제 운영 정제는 로컬 백엔드(local_vllm/ollama)로 졸업시킨다.

환경변수:
  - AGENT_CLI_PROGRAM : 호출할 CLI (claude[기본] | codex | omc). claude 외엔
                        AGENT_CLI_ARGV 로 완전한 argv 템플릿을 직접 줘야 한다.
  - AGENT_CLI_MODEL   : claude 모델 별칭/ID (기본 "sonnet").
  - AGENT_CLI_TIMEOUT : 콜당 타임아웃 초 (기본 120).
  - AGENT_CLI_ARGV    : (고급) JSON 배열로 argv 템플릿 직접 지정. 토큰 "{system}"
                        "{user}" 가 각각 system/user 본문으로 치환된다. 지정 시
                        PROGRAM/MODEL 무시.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from src.postprocess.backends.base import LLMBackend, LLMCapabilities

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 120


def _join_role(messages: list[dict], role: str) -> str:
    """주어진 role 의 모든 메시지 content 를 순서대로 결합."""
    return "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == role
    ).strip()


def _build_argv(system: str, user: str) -> list[str]:
    """system/user 본문 → 실행할 argv. 셸 미경유(리스트 형태)라 이스케이프 불필요."""
    template = os.environ.get("AGENT_CLI_ARGV")
    if template:
        # 고급 경로: 사용자가 argv 템플릿을 직접 준다(codex/omc 등). 토큰 치환.
        try:
            raw = json.loads(template)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"AGENT_CLI_ARGV JSON 파싱 실패: {e}") from e
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise RuntimeError("AGENT_CLI_ARGV 는 문자열 배열이어야 합니다.")
        return [tok.replace("{system}", system).replace("{user}", user) for tok in raw]

    program = os.environ.get("AGENT_CLI_PROGRAM", "claude").strip().lower()
    if program != "claude":
        raise RuntimeError(
            f"AGENT_CLI_PROGRAM={program!r} 의 기본 호출 규약은 미정의입니다. "
            "AGENT_CLI_ARGV 로 argv 템플릿을 직접 지정하세요(토큰 {system}/{user})."
        )

    model = os.environ.get("AGENT_CLI_MODEL", DEFAULT_MODEL)
    # claude -p: 비대화 print. (--bare 는 OAuth 미지원이라 미사용 — docstring 참조.)
    # system 규칙은 append-system-prompt, user 본문은 위치인자. 스킬 트리거는 차단.
    return [
        "claude",
        "-p",
        "--model", model,
        "--output-format", "text",
        "--disable-slash-commands",
        "--append-system-prompt", system,
        user,
    ]


class AgentCLIBackend(LLMBackend):
    """세션 에이전트 CLI(기본 claude -p)를 헤드리스로 호출하는 정제 백엔드."""

    name = "agent_cli"

    def generate(
        self,
        messages: list[dict],
        *,
        schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        seed: int | None = 0,
    ) -> str:
        """messages → CLI 헤드리스 호출 → 정제 본문 텍스트.

        schema 는 무시한다(현 정제는 plain text in/out, capabilities().json_mode=False).
        temperature/seed 는 claude CLI 가 노출하지 않으므로 best-effort 로도 전달 못 함
        → determinism="none"(capabilities 참조).
        """
        system = _join_role(messages, "system")
        user = _join_role(messages, "user")
        if not user:
            return ""

        argv = _build_argv(system, user)
        program = argv[0]
        if shutil.which(program) is None:
            raise RuntimeError(
                f"에이전트 CLI '{program}' 를 PATH 에서 찾을 수 없습니다. "
                "설치 여부 또는 AGENT_CLI_PROGRAM/AGENT_CLI_ARGV 설정을 확인하세요."
            )

        timeout = int(os.environ.get("AGENT_CLI_TIMEOUT", DEFAULT_TIMEOUT))
        # 오케스트레이션 노이즈/지연 차단: OMC 킬스위치 + 중립 cwd(프로젝트 CLAUDE.md 회피).
        sub_env = dict(os.environ)
        sub_env.setdefault("DISABLE_OMC", "1")
        sub_env.setdefault("OMC_SKIP_HOOKS", "1")
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=sub_env,
                cwd=tempfile.gettempdir(),
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"agent_cli 호출이 {timeout}s 안에 끝나지 않았습니다(AGENT_CLI_TIMEOUT). "
                f"program={program}"
            ) from e

        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:500]
            raise RuntimeError(
                f"agent_cli 호출 실패(exit={proc.returncode}, program={program}): {err}"
            )

        # claude -p --output-format text 는 모델 본문만 stdout 으로 낸다.
        # 양끝 공백만 제거. 구분자(<<<SEGMENT>>>)가 echo 되면 CleanStage 가 _unwrap 한다.
        return (proc.stdout or "").strip()

    def capabilities(self) -> LLMCapabilities:
        """claude CLI 기준 능력.

        - json_mode=False : 현 정제는 plain text 경로(스키마 강제는 호출부가 안 함).
        - ctx_window      : 세그먼트 단위 호출이라 사실상 무제약(모델 200k 컨텍스트).
        - tool_call=False : 정제 변환에 도구 미사용(-p 순수 텍스트).
        - determinism="none" : claude CLI 가 seed/temperature 를 노출하지 않음(설계 §7).
                               비결정 잔여는 [D] 검증·리페어 게이트로 흡수.
        """
        return LLMCapabilities(
            json_mode=False, ctx_window=200_000, tool_call=False, determinism="none"
        )
