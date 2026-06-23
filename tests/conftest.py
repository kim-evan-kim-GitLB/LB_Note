"""pytest 공통 설정 — 실 DB 격리 방어 심층(defense-in-depth).

배경: src.web.app 은 import 시 store=MeetingStore()/users=auth.init() 를 실행한다. 과거 테스트가
DEFAULT_DB_PATH 패치를 빠뜨려 **실 운영 DB(output/web/meetings.db)를 2회 변조**(1회는 prune 로
비번 리셋)한 사고가 있었다. 여기서 두 겹으로 막는다:

  1. MEETSCRIPT_BLOCK_DEFAULT_DB=1 을 **conftest import 시점**(테스트 모듈 수집/임포트 전)에 설정
     → 가드레일(src/web/store.py·auth.py _guard_default_db) 발동. 패치 누락 시 실 DB 를 여는
     순간 RuntimeError 로 즉시 실패(=실 DB 미변조). 정상 부팅(env 미설정)엔 영향 없음.
  2. autouse(session) 픽스처가 store/auth 의 DEFAULT_DB_PATH 를 세션 전용 임시 경로로 패치
     → 런타임에 무인자 MeetingStore()/auth.init() 가 실 경로 대신 임시 DB 를 쓰게 한다.

가드레일 동작을 의도적으로 검증하는 테스트가 있으면 그 안에서 env 를 토글하면 된다.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 수집(=테스트 모듈 import) 전에 가드레일을 켠다. 모듈 최상단 import 시점에 실행되므로
# pytest 가 어떤 테스트 파일을 import 하기도 전에 MEETSCRIPT_BLOCK_DEFAULT_DB=1 이 보장된다.
os.environ["MEETSCRIPT_BLOCK_DEFAULT_DB"] = "1"


@pytest.fixture(scope="session", autouse=True)
def _isolate_default_db():
    """세션 전체에서 store/auth 의 DEFAULT_DB_PATH 를 임시 경로로 패치(런타임 방어)."""
    import src.web.store as storemod
    import src.web.auth as authmod

    store_orig = storemod.DEFAULT_DB_PATH
    auth_orig = authmod.DEFAULT_DB_PATH
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "meetings.db"
        storemod.DEFAULT_DB_PATH = tmp_db
        authmod.DEFAULT_DB_PATH = tmp_db
        try:
            yield tmp_db
        finally:
            storemod.DEFAULT_DB_PATH = store_orig
            authmod.DEFAULT_DB_PATH = auth_orig
