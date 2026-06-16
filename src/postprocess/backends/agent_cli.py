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

**경계(2026-06-09 갱신):** claude/codex 경유는 내용을 Anthropic/OpenAI 클라우드로 보낸다.
종전엔 온프레미스/PII 전제와 충돌해 **게이트·벤치마크 전용**이었으나, CEO 가 클라우드 반출을
승인해 **운영 백엔드로도 허용**된다. 다만 비용은 콜 수에 좌우된다 — 추출(ExtractStage)은
회의당 1콜이라 클라우드도 ≈$0.06 으로 사실상 무시 가능하지만, 정제(CleanStage)는 segment당
1콜이라 claude -p 의 하니스 오버헤드(~25k/콜)로 ≈$4~5/회의가 든다. 따라서 대량 정제를
클라우드로 돌릴 거면 하니스 오버헤드가 없는 직접 API 백엔드(anthropic/openai)나 로컬
백엔드(ollama 등)가 비용·결정성 면에서 유리하다.

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

import contextlib
import contextvars
import json
import os
import pwd
import shutil
import subprocess
import tempfile

from src.postprocess.backends.base import LLMBackend, LLMCapabilities

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 120
DEFAULT_RETRIES = 2  # 일시 실패(타임아웃/비정상종료) 시 재시도 횟수. 긴 배치 보호용.

# 사용자별 claude 자격증명 주입 채널(웹 멀티유저). 잡 스레드가 set_active_credential 로
# 현재 사용자 자격증명을 심으면 generate() 의 sub_env 구성부가 이를 읽어 격리 주입한다.
# 값: {"type": "api_key"|"oauth_token", "secret": "..."} 또는 None(전역 폴백).
# ContextVar 라 스레드/태스크별로 격리된다(다른 사용자의 잡과 섞이지 않음).
_active_credential: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "agent_cli_active_credential", default=None
)


def set_active_credential(cred: dict | None) -> contextvars.Token:
    """현재 컨텍스트(스레드/태스크)에 사용자 자격증명을 심는다. reset 용 Token 반환.

    cred 가 None 이면 전역 폴백(서버 OAuth + HOME 교정)으로 동작. 보통은 use_credential()
    컨텍스트매니저를 쓰고, 수동 reset 이 필요할 때만 이 함수를 직접 쓴다.
    """
    return _active_credential.set(cred)


@contextlib.contextmanager
def use_credential(cred: dict | None):
    """with 블록 동안만 사용자 자격증명을 활성화하고, 빠져나오면 원복(누수 방지)."""
    token = _active_credential.set(cred)
    try:
        yield
    finally:
        _active_credential.reset(token)

# claude 구독 OAuth 가 만료/미로그인일 때 stderr 에 나타나는 표지들(소문자 비교).
# 재시도해도 풀리지 않으므로(브라우저 재로그인 필요) 즉시 AgentCLIAuthError 로 드러낸다.
_AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "/login",
    "invalid api key",
    "authentication_error",
    "unauthorized",
    "401",
    "oauth token",
    "token has expired",
    "session expired",
)


class AgentCLIAuthError(RuntimeError):
    """claude CLI 구독 인증이 만료/미로그인 상태일 때(재시도로 풀리지 않음, 재인증 필요).

    일반 호출 실패(RuntimeError)와 구분해 호출부가 "인증부터 다시" 흐름으로 분기하게 한다.
    """


def _resolved_home() -> str:
    """claude 자격증명을 찾을 HOME. sudo(root) 실행이면 SUDO_USER 홈으로 교정."""
    home = os.environ.get("HOME", "")
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user:
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return home


def claude_auth_status() -> dict:
    """claude 자격증명 상태(토큰/키 값은 절대 노출하지 않음).

    사용자별 자격증명(_active_credential)이 주입돼 있으면 그 상태를 우선 반환한다:
      - api_key  → 만료 개념 없음 → {"ok": True, "source": "user_api_key"}.
      - oauth_token → 주입돼 있으면 ok(만료시각은 토큰 안에 있어 파일로는 못 읽음) →
        {"ok": True, "source": "user_oauth_token"}. 실제 만료는 호출 시 드러난다.
    미설정이면 전역 서버 OAuth(`~/.claude/.credentials.json`) 상태를 본다(아래).

    전역 경로: claudeAiOauth.expiresAt(ms) 로 만료 여부를 best-effort 판정한다. 자격증명
    파일이 없으면 미로그인으로 본다. 액세스 토큰은 CLI 가 refresh 로 자동 갱신할 수 있으므로,
    expiresAt 이 과거여도 즉시 사용불가를 단정하지는 않는다 → `ok=False,
    reason="access_token_expired"` 로 경고 신호만 준다(권위 신호는 실제 호출).
    """
    import time

    cred = _active_credential.get()
    if cred:
        ctype = cred.get("type")
        if ctype == "api_key":
            return {"ok": True, "source": "user_api_key"}
        if ctype == "oauth_token":
            return {"ok": True, "source": "user_oauth_token"}

    path = os.path.join(_resolved_home(), ".claude", ".credentials.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"ok": False, "reason": "no_credentials"}
    except (OSError, json.JSONDecodeError) as e:  # noqa: BLE001
        return {"ok": False, "reason": f"unreadable: {type(e).__name__}"}

    oauth = data.get("claudeAiOauth") or {}
    exp = oauth.get("expiresAt")
    if not isinstance(exp, (int, float)):
        # 자격증명은 있으나 만료시각을 못 읽음 → 존재만으로 ok(권위 신호는 실제 호출).
        return {"ok": True, "reason": "no_expiry_field"}
    exp_sec = exp / 1000 if exp > 1e12 else exp
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(exp_sec))
    if exp_sec > time.time():
        return {"ok": True, "expires_at": expires_at}
    return {"ok": False, "reason": "access_token_expired", "expires_at": expires_at}


def _join_role(messages: list[dict], role: str) -> str:
    """주어진 role 의 모든 메시지 content 를 순서대로 결합."""
    return "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == role
    ).strip()


def _build_argv(system: str, user: str, *, bare: bool = False) -> list[str]:
    """system/user 본문 → 실행할 argv. 셸 미경유(리스트 형태)라 이스케이프 불필요.

    bare=True(api_key 사용자)면 claude 기본 경로에 `--bare` 를 추가해 OAuth/keychain 을
    무시하고 ANTHROPIC_API_KEY 만 쓰게 한다(완전 격리). `--bare` 가
    `--append-system-prompt`/`--disable-slash-commands` 와 비호환이면 호출부가 폴백한다.
    """
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
    # claude -p: 비대화 print. 전역(미설정)/oauth_token 경로는 OAuth 또는
    # CLAUDE_CODE_OAUTH_TOKEN 을 쓰므로 --bare 미사용. api_key 사용자만 bare=True 로
    # --bare 를 추가해 ANTHROPIC_API_KEY 단독 격리(OAuth/keychain 무시).
    # system 규칙은 append-system-prompt, user 본문은 위치인자. 스킬 트리거는 차단.
    argv = [
        "claude",
        "-p",
        "--model", model,
        "--output-format", "text",
        "--disable-slash-commands",
        "--append-system-prompt", system,
    ]
    if bare:
        argv.append("--bare")
    argv.append(user)
    return argv


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

        # 사용자별 자격증명(웹 멀티유저). 미설정이면 전역 폴백.
        cred = _active_credential.get()
        cred_type = cred.get("type") if cred else None
        argv = _build_argv(system, user, bare=(cred_type == "api_key"))
        program = argv[0]
        if shutil.which(program) is None:
            raise RuntimeError(
                f"에이전트 CLI '{program}' 를 PATH 에서 찾을 수 없습니다. "
                "설치 여부 또는 AGENT_CLI_PROGRAM/AGENT_CLI_ARGV 설정을 확인하세요."
            )

        timeout = int(os.environ.get("AGENT_CLI_TIMEOUT", DEFAULT_TIMEOUT))
        retries = int(os.environ.get("AGENT_CLI_RETRIES", DEFAULT_RETRIES))
        # 오케스트레이션 노이즈/지연 차단: OMC 킬스위치 + 중립 cwd(프로젝트 CLAUDE.md 회피).
        sub_env = dict(os.environ)
        sub_env.setdefault("DISABLE_OMC", "1")
        sub_env.setdefault("OMC_SKIP_HOOKS", "1")
        if cred_type == "api_key":
            # API 키 사용자: ANTHROPIC_API_KEY 단독 격리(+argv 의 --bare). OAuth 무시 →
            # HOME 교정 불필요. 다른 사용자의 OAuth 토큰이 섞이지 않게 전역 OAuth env 도 제거.
            sub_env["ANTHROPIC_API_KEY"] = cred["secret"]
            sub_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        elif cred_type == "oauth_token":
            # 구독 토큰 사용자: CLAUDE_CODE_OAUTH_TOKEN 우선 → HOME 교정 불필요.
            # 키가 섞이지 않게 ANTHROPIC_API_KEY 는 제거.
            sub_env["CLAUDE_CODE_OAUTH_TOKEN"] = cred["secret"]
            sub_env.pop("ANTHROPIC_API_KEY", None)
        else:
            # 전역 폴백(미설정 사용자): .venv 가 root 소유라 파이프라인은 sudo(root)로 돌지만,
            # claude OAuth 자격증명은 호출자(evan) 소유다. root 의 HOME(/root)에선 못 찾아
            # "Not logged in"(exit 1) → SUDO_USER 의 HOME 으로 교정해 구독 인증을 살린다
            # (claude 가 ~user/.claude 를 읽음).
            sub_env["HOME"] = _resolved_home()

        # 일시 실패(타임아웃/비정상종료)는 재시도. 긴 배치(수백 콜)가 한 번의 blip 으로
        # 통째로 죽지 않게 한다(설계 §: backend 재요청 훅). 소진하면 RuntimeError 로 드러냄.
        last_err = ""
        for attempt in range(retries + 1):
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    env=sub_env,
                    cwd=tempfile.gettempdir(),
                    # claude -p 는 stdin 을 읽으려 대기한다(positional prompt 가 있어도).
                    # EOF 를 즉시 줘서 "no stdin data received in 3s" 지연/실패 차단.
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                last_err = f"타임아웃({timeout}s)"
                continue
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                last_err = f"exit={proc.returncode}: {stderr[:300]}"
                # 인증 만료/미로그인은 재시도해도 안 풀린다(브라우저 재로그인 필요) →
                # 일반 실패와 구분해 즉시 AgentCLIAuthError 로 드러내 "인증부터" 흐름 유도.
                low = stderr.lower()
                if any(m in low for m in _AUTH_MARKERS):
                    raise AgentCLIAuthError(
                        "claude 구독 인증이 만료되었거나 로그인되어 있지 않습니다. "
                        "서버(호출 사용자 계정)에서 `claude login` 으로 재인증이 필요합니다. "
                        f"(stderr: {stderr[:200]})"
                    )
                continue
            # claude -p --output-format text 는 모델 본문만 stdout 으로 낸다. 양끝 공백만
            # 제거. 구분자(<<<SEGMENT>>>)가 echo 되면 CleanStage 가 _unwrap 한다. 빈
            # 출력은 에러로 보지 않는다(CleanStage 가 original 로 폴백) — 재시도 낭비 방지.
            return (proc.stdout or "").strip()

        raise RuntimeError(
            f"agent_cli 호출 실패(program={program}, {retries + 1}회 시도): {last_err}"
        )

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
