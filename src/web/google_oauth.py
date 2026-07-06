"""Google OAuth 2.0 authorization-code 흐름(서버사이드) — 회의록 Drive 동기화용.

사용자별 본인 Google 계정 연동: 동의 URL 발급 → 콜백에서 code→refresh_token 교환 → 저장
(auth.set_google_credential). 이후 동기화 때 refresh_token 으로 단기 access_token 을 재발급한다.

설계 결정(docs/2026-06-26 조사 반영):
  - 스코프 = drive.file(최소권한, 앱이 만든 파일만) + openid/email(연결계정 표시용).
  - access_type=offline + prompt=consent 로 refresh_token 재발급을 보장한다.
  - OOB 흐름(2023-01 폐지)은 쓰지 않는다 → redirect_uri 는 https 등록 도메인이어야 한다(운영 전제).
  - google 라이브러리는 **함수 안에서 지연 import** 한다 → 라이브러리 미설치 환경에서도 이 모듈
    (및 app)이 로드된다(연동은 옵트인 기능). 미설정/미설치는 endpoint 가 graceful 하게 처리.

env:
  GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REDIRECT_URI
"""
from __future__ import annotations

import base64
import json
import os

# 요청 스코프. email 은 full URL 로 명시(구글이 'email' → userinfo.email 로 확장해 반환하므로
# 요청/부여 스코프를 일치시켜 scope-change 잡음을 줄인다). openid 는 id_token(email 추출) 발급용.
#   - drive.file: 앱이 만든 회의록 문서/오디오만(최소권한, non-sensitive).
#   - calendar.events: 캘린더 일정 읽기+쓰기(양방향 연동). read(events.list)·write(events.insert/update)
#     둘 다 커버. sensitive 스코프이나 동의화면 Internal(Workspace)이라 구글 검증 불필요.
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar.events",
]

_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleOAuthError(RuntimeError):
    """OAuth 흐름 일반 오류(설정 누락·교환 실패 등)."""


class GoogleAuthExpired(GoogleOAuthError):
    """refresh_token 무효/취소(invalid_grant) — 사용자 재연동 필요. endpoint 가 error_code 로 안내."""


def _db_config() -> dict | None:
    """관리자가 인앱 설정한 앱 OAuth 클라이언트(DB). 없거나 store 미초기화면 None(→ env 폴백).

    client_id/secret/redirect_uri 는 DB(관리자 설정) 우선, 없으면 env(GOOGLE_OAUTH_*)로 폴백한다.
    google_oauth 는 store 참조를 갖지 않으므로 매 호출 시 auth 를 지연 import 해 조회한다."""
    try:
        from src.web import auth
        return auth.get_google_oauth_config()
    except Exception:  # noqa: BLE001 — store 미초기화/조회 실패는 env 폴백으로 흡수
        return None


def _client_id() -> str:
    cfg = _db_config()
    if cfg and (cfg.get("client_id") or "").strip():
        return cfg["client_id"].strip()
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    cfg = _db_config()
    if cfg and (cfg.get("client_secret") or "").strip():
        return cfg["client_secret"].strip()
    return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()


def _redirect_uri() -> str:
    cfg = _db_config()
    if cfg and (cfg.get("redirect_uri") or "").strip():
        return cfg["redirect_uri"].strip()
    return os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()


def oauth_configured() -> bool:
    """client_id/secret/redirect_uri 가 (DB 또는 env 에) 모두 있는지. google 라이브러리는 불검사."""
    return bool(_client_id() and _client_secret() and _redirect_uri())


def config_status() -> dict:
    """관리자용 앱 OAuth 설정 공개 상태(**client_secret 절대 미노출**).

    반환: {configured, source('db'|'env'|'none'), clientId, redirectUri, updatedAt}. client_id/
    redirect_uri 는 비밀이 아니라 표시한다(설정 확인용). client_secret 은 어떤 필드에도 싣지 않는다.
    """
    cfg = _db_config()
    db_has = bool(cfg and (cfg.get("client_id") or "").strip())
    env_has = bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip())
    source = "db" if db_has else ("env" if env_has else "none")
    return {
        "configured": oauth_configured(),
        "source": source,
        "clientId": _client_id(),
        "redirectUri": _redirect_uri(),
        "updatedAt": (cfg or {}).get("updated_at") if db_has else None,
    }


def _client_config() -> dict:
    return {
        "web": {
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
            "redirect_uris": [_redirect_uri()],
        }
    }


def _build_flow(state: str | None = None):
    """google_auth_oauthlib Flow 생성(지연 import). 미설정이면 GoogleOAuthError.

    requests-oauthlib 는 부여 스코프가 요청과 다르면 예외를 던진다(구글이 스코프를 재정렬/확장).
    OAUTHLIB_RELAX_TOKEN_SCOPE 로 이 잡음을 완화한다(스코프 축소가 아니라 표현 차이일 뿐)."""
    if not oauth_configured():
        raise GoogleOAuthError(
            "Google OAuth 미설정 — GOOGLE_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI 를 설정하세요."
        )
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as e:  # 라이브러리 미설치
        raise GoogleOAuthError(f"google-auth-oauthlib 미설치: {e}") from e
    # PKCE 비활성화(autogenerate_code_verifier=False): 우리는 동의(build_consent_url)와 콜백
    # (exchange_code)에서 각각 별개의 Flow 인스턴스를 새로 만드는 stateless 설계라, 라이브러리가
    # 자동 생성하는 code_verifier 가 두 단계 사이에 유실된다 → 토큰 교환 시 (invalid_grant)
    # "Missing code verifier". client_secret 을 가진 confidential 웹앱이라 PKCE 는 선택사항이므로
    # 끈다(대신 client_secret 으로 클라이언트를 인증). 이러면 code_challenge 자체를 안 보낸다.
    return Flow.from_client_config(
        _client_config(),
        scopes=GOOGLE_SCOPES,
        state=state,
        redirect_uri=_redirect_uri(),
        autogenerate_code_verifier=False,
    )


def build_consent_url(state: str) -> str:
    """동의 URL 생성. state 는 서명 JWT(신원+CSRF). offline+consent 로 refresh_token 보장."""
    flow = _build_flow(state=state)
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url


def _email_from_id_token(id_token: str | None) -> str | None:
    """id_token(JWT) payload 에서 email 추출(표시용, best-effort). 검증은 생략 — 토큰 교환은
    구글과의 TLS 직결이라 출처가 신뢰되며, email 은 보안 결정에 쓰지 않는다(표시 전용)."""
    if not id_token:
        return None
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # base64 패딩 보정
        data = json.loads(base64.urlsafe_b64decode(payload_b64))
        email = data.get("email")
        return str(email) if email else None
    except Exception:  # noqa: BLE001 — 표시용 부가정보라 실패는 None 으로 흡수
        return None


def exchange_code(code: str) -> dict:
    """authorization code → 토큰. 반환: {refresh_token, access_token, expiry, email}.

    refresh_token 이 없으면(offline/consent 누락 등) GoogleOAuthError — 저장할 오프라인 자격증명이
    없으면 이후 동기화가 불가하므로 연동 실패로 간주한다.
    """
    flow = _build_flow()
    try:
        flow.fetch_token(code=code)
    except Exception as e:  # noqa: BLE001 — oauthlib 계열 다양한 예외 → 일반 오류로 매핑
        raise GoogleOAuthError(f"토큰 교환 실패: {type(e).__name__}: {e}") from e
    creds = flow.credentials
    refresh_token = getattr(creds, "refresh_token", None)
    if not refresh_token:
        raise GoogleOAuthError(
            "refresh_token 을 받지 못했습니다(계정 이미 승인됨?). 재연동 시 prompt=consent 로 재발급됩니다."
        )
    return {
        "refresh_token": refresh_token,
        "access_token": getattr(creds, "token", None),
        "expiry": getattr(creds, "expiry", None),
        "email": _email_from_id_token(getattr(creds, "id_token", None)),
    }


def refresh_access_token(refresh_token: str) -> str:
    """refresh_token → 단기 access_token. 무효/취소(invalid_grant)면 GoogleAuthExpired."""
    if not oauth_configured():
        raise GoogleOAuthError("Google OAuth 미설정.")
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise GoogleOAuthError(f"google-auth 미설치: {e}") from e
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=_client_id(),
        client_secret=_client_secret(),
        scopes=GOOGLE_SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as e:
        # invalid_grant(권한 철회·만료·비번 변경) → 재연동 필요로 구분.
        raise GoogleAuthExpired(f"refresh_token 무효(재연동 필요): {e}") from e
    except Exception as e:  # noqa: BLE001
        raise GoogleOAuthError(f"access_token 갱신 실패: {type(e).__name__}: {e}") from e
    if not creds.token:
        raise GoogleOAuthError("access_token 이 비어 있습니다.")
    return creds.token
