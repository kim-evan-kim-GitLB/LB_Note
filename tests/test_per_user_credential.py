"""사용자별 claude 자격증명 회귀 테스트 — 저장/주입/비노출 불변식.

검증 불변식:
  - 자격증명 미설정(ContextVar None) → 전역 폴백(HOME 교정), --bare 없음.
  - type=api_key → argv 에 --bare, sub_env 에 ANTHROPIC_API_KEY(전역 OAuth 토큰 제거).
  - type=oauth_token → sub_env 에 CLAUDE_CODE_OAUTH_TOKEN(ANTHROPIC_API_KEY 제거), --bare 없음.
  - credential_status 는 secret 을 절대 반환하지 않는다.
  - claude_auth_status 가 ContextVar 자격증명을 반영(secret 미노출).

CLI 실행부(_run_cancellable)를 모킹해 argv/env 를 캡처(실제 claude 미호출). 임시 DB.
실행: sudo PYTHONPATH=/app .venv/bin/python tests/test_per_user_credential.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import src.postprocess.backends.agent_cli as ac


def _gen_capture(cred):
    """use_credential(cred) 컨텍스트에서 generate() 1콜 → 호출된 (argv, env) 캡처."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(argv, sub_env, timeout):
        captured["argv"] = argv
        captured["env"] = sub_env
        return _Proc()

    backend = ac.AgentCLIBackend()
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    with mock.patch.object(ac.shutil, "which", return_value="/usr/bin/claude"), \
         mock.patch.object(ac.AgentCLIBackend, "_run_cancellable", staticmethod(_fake_run)):
        with ac.use_credential(cred):
            backend.generate(msgs)
    return captured["argv"], captured["env"]


def test_no_credential_global_fallback():
    argv, env = _gen_capture(None)
    assert "--bare" not in argv, argv
    assert "ANTHROPIC_API_KEY" not in env or env.get("ANTHROPIC_API_KEY") == os.environ.get("ANTHROPIC_API_KEY")
    assert "HOME" in env  # 전역 폴백은 HOME 교정


def test_api_key_uses_bare_and_env():
    argv, env = _gen_capture({"type": "api_key", "secret": "sk-test-123"})
    assert "--bare" in argv, argv
    assert env.get("ANTHROPIC_API_KEY") == "sk-test-123"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # 토큰 혼입 방지


def test_oauth_token_sets_env_no_bare():
    argv, env = _gen_capture({"type": "oauth_token", "secret": "oauth-tok-xyz"})
    assert "--bare" not in argv, argv
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oauth-tok-xyz"
    assert "ANTHROPIC_API_KEY" not in env  # 키 혼입 방지


def test_auth_status_reflects_contextvar_without_secret():
    with ac.use_credential({"type": "api_key", "secret": "sk-secret"}):
        st = ac.claude_auth_status()
    assert st["ok"] is True and st.get("source") == "user_api_key"
    assert "sk-secret" not in str(st)
    with ac.use_credential({"type": "oauth_token", "secret": "tok-secret"}):
        st2 = ac.claude_auth_status()
    assert st2["ok"] is True and st2.get("source") == "user_oauth_token"
    assert "tok-secret" not in str(st2)


def test_store_roundtrip_and_status_hides_secret():
    os.environ["JWT_SECRET"] = "test-secret-cred"
    os.environ["WEB_AUTH_USERS"] = "admin:pw1"
    import importlib

    import src.web.auth as auth
    importlib.reload(auth)
    with tempfile.TemporaryDirectory() as td:
        store = auth.init(Path(td) / "users.db")
        # 미설정 상태
        assert store.credential_status("admin") == {"configured": False, "type": None, "updated_at": None}
        assert store.get_credential("admin") is None
        # 저장
        store.set_credential("admin", "api_key", "sk-super-secret")
        cred = store.get_credential("admin")  # 내부용: secret 포함
        assert cred["type"] == "api_key" and cred["secret"] == "sk-super-secret"
        # 공개 상태: secret 절대 비노출
        st = store.credential_status("admin")
        assert st["configured"] is True and st["type"] == "api_key" and st["updated_at"]
        assert "sk-super-secret" not in str(st) and "secret" not in st
        # 잘못된 type → ValueError
        try:
            store.set_credential("admin", "bogus", "x")
            assert False, "ValueError 여야 함"
        except ValueError:
            pass
        # clear
        assert store.clear_credential("admin") is True
        assert store.credential_status("admin")["configured"] is False
        assert store.clear_credential("admin") is False  # 이미 없음


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_per_user_credential ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
