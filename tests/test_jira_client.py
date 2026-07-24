"""jira_client 순수 함수 단위테스트 — _request 를 모킹해 실제 HTTP 없이 검증.

검증 불변식:
  - verify/get_projects/get_issue_types/get_create_meta/lookup_account_id 반환 정규화.
  - create_issue: fields 조립(project/issuetype/summary/description(ADF)/duedate/parent/
    assignee/reporter/extra_fields) 및 url = base_url + '/browse/' + key.
  - add_watchers: accountId 마다 POST, body=accountId 문자열.
  - _request: Basic 헤더 = base64(email:api_token), 401/403 → JiraAuthError.
  - api_token 이 예외 메시지에 새지 않는다.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_jira_client.py
"""
from __future__ import annotations

import base64
import json
from unittest import mock

import pytest

from src.web import jira_client

CFG = {"base_url": "https://litbig.atlassian.net", "email": "svc@litbig.com", "api_token": "SECRET-TOKEN"}


def test_basic_header_is_base64_email_token():
    h = jira_client._basic_header(CFG)
    assert h.startswith("Basic ")
    decoded = base64.b64decode(h[len("Basic "):]).decode()
    assert decoded == "svc@litbig.com:SECRET-TOKEN"


def test_verify_returns_trimmed_account():
    payload = {
        "accountId": "5b10abc",
        "displayName": "서비스 계정",
        "emailAddress": "svc@litbig.com",
        "extra": "ignored",
    }
    with mock.patch.object(jira_client, "_request", return_value=payload) as m:
        out = jira_client.verify(CFG)
    m.assert_called_once_with(CFG, "GET", "/rest/api/3/myself")
    assert out == {
        "accountId": "5b10abc",
        "displayName": "서비스 계정",
        "emailAddress": "svc@litbig.com",
    }


def test_get_projects_paginates_and_normalizes():
    page1 = {
        "values": [
            {"key": "AAA", "name": "프로젝트A", "style": "classic", "junk": 1},
            {"key": "BBB", "name": "프로젝트B", "style": "next-gen"},
        ],
        "isLast": False,
        "total": 3,
    }
    page2 = {
        "values": [{"key": "CCC", "name": "프로젝트C", "style": "classic"}],
        "isLast": True,
        "total": 3,
    }
    with mock.patch.object(jira_client, "_request", side_effect=[page1, page2]) as m:
        out = jira_client.get_projects(CFG, max_results=2)
    assert m.call_count == 2
    assert out == [
        {"key": "AAA", "name": "프로젝트A", "style": "classic"},
        {"key": "BBB", "name": "프로젝트B", "style": "next-gen"},
        {"key": "CCC", "name": "프로젝트C", "style": "classic"},
    ]


def test_get_issue_types():
    payload = {"issueTypes": [{"id": "10000", "name": "에픽"}, {"id": "10009", "name": "작업"}]}
    with mock.patch.object(jira_client, "_request", return_value=payload):
        out = jira_client.get_issue_types(CFG, "AAA")
    assert out == [{"id": "10000", "name": "에픽"}, {"id": "10009", "name": "작업"}]


def test_get_create_meta_parses_fields_and_caps_allowed_values():
    big_allowed = [{"id": str(i), "name": f"opt{i}"} for i in range(120)]
    payload = {
        "fields": [
            {"fieldId": "summary", "name": "Summary", "required": True, "schema": {"type": "string"}},
            {
                "fieldId": "customfield_10192",
                "name": "워크스페이스",
                "required": True,
                "schema": {"type": "option"},
                "allowedValues": big_allowed,
            },
            {"fieldId": "duedate", "name": "Due date", "required": True, "schema": {"type": "date"}},
        ]
    }
    with mock.patch.object(jira_client, "_request", return_value=payload):
        out = jira_client.get_create_meta(CFG, "AAA", "10000")
    fields = out["fields"]
    assert fields[0] == {
        "fieldId": "summary",
        "name": "Summary",
        "required": True,
        "schemaType": "string",
        "allowedValues": None,
    }
    ws = fields[1]
    assert ws["fieldId"] == "customfield_10192" and ws["required"] is True
    assert len(ws["allowedValues"]) == 50  # 상위 50개로 절단
    assert ws["allowedValues"][0] == {"id": "0", "name": "opt0"}


def test_get_create_meta_values_key_fallback():
    """구/신 버전차: 필드가 'values' 키에 담겨 와도 파싱한다."""
    payload = {"values": [{"fieldId": "summary", "name": "S", "required": True, "schema": {"type": "string"}}]}
    with mock.patch.object(jira_client, "_request", return_value=payload):
        out = jira_client.get_create_meta(CFG, "AAA", "10009")
    assert out["fields"][0]["fieldId"] == "summary"


def test_lookup_account_id_first_result_or_none():
    with mock.patch.object(
        jira_client, "_request",
        return_value=[{"accountId": "5b10x", "displayName": "홍길동"}, {"accountId": "other"}],
    ) as m:
        out = jira_client.lookup_account_id(CFG, "hong@litbig.com")
    m.assert_called_once_with(CFG, "GET", "/rest/api/3/user/search", params={"query": "hong@litbig.com"})
    assert out == {"accountId": "5b10x", "displayName": "홍길동"}
    with mock.patch.object(jira_client, "_request", return_value=[]):
        assert jira_client.lookup_account_id(CFG, "none@litbig.com") is None


def test_adf_wraps_plain_text():
    doc = jira_client._adf("첫 문단\n\n둘째 문단")
    assert doc["type"] == "doc" and doc["version"] == 1
    assert len(doc["content"]) == 2
    assert doc["content"][0]["content"][0]["text"] == "첫 문단"


def test_create_issue_assembles_fields_and_url():
    captured = {}

    def fake_request(cfg, method, path, *, params=None, json_body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {"key": "AAA-42", "id": "99999"}

    with mock.patch.object(jira_client, "_request", side_effect=fake_request):
        out = jira_client.create_issue(
            CFG,
            project_key="AAA",
            issuetype_id="10009",
            summary="테스트 작업",
            description="설명 본문",
            duedate="2026-08-01",
            parent_key="AAA-1",
            assignee_id="acc-assignee",
            reporter_id="acc-reporter",
            extra_fields={"customfield_10192": {"id": "5"}},
        )
    assert captured["method"] == "POST" and captured["path"] == "/rest/api/3/issue"
    fields = captured["body"]["fields"]
    assert fields["project"] == {"key": "AAA"}
    assert fields["issuetype"] == {"id": "10009"}
    assert fields["summary"] == "테스트 작업"
    assert fields["description"]["type"] == "doc"  # 평문 → ADF
    assert fields["duedate"] == "2026-08-01"
    assert fields["parent"] == {"key": "AAA-1"}
    assert fields["assignee"] == {"id": "acc-assignee"}
    assert fields["reporter"] == {"id": "acc-reporter"}
    assert fields["customfield_10192"] == {"id": "5"}
    assert out == {
        "key": "AAA-42",
        "id": "99999",
        "url": "https://litbig.atlassian.net/browse/AAA-42",
    }


def test_create_issue_minimal_omits_optionals():
    with mock.patch.object(jira_client, "_request", return_value={"key": "AAA-1", "id": "1"}) as m:
        jira_client.create_issue(CFG, project_key="AAA", issuetype_id="10009", summary="s")
    fields = m.call_args.kwargs["json_body"]["fields"]
    assert set(fields) == {"project", "issuetype", "summary"}  # 선택 필드 미포함


def test_create_issue_description_dict_passthrough():
    """이미 ADF dict 이면 그대로 싣는다(_adf 재래핑 안 함)."""
    adf = {"type": "doc", "version": 1, "content": []}
    with mock.patch.object(jira_client, "_request", return_value={"key": "AAA-2", "id": "2"}) as m:
        jira_client.create_issue(
            CFG, project_key="AAA", issuetype_id="10009", summary="s", description=adf
        )
    assert m.call_args.kwargs["json_body"]["fields"]["description"] is adf


def test_add_watchers_posts_each_account():
    calls = []

    def fake_request(cfg, method, path, *, params=None, json_body=None):
        calls.append((method, path, json_body))
        return {}

    with mock.patch.object(jira_client, "_request", side_effect=fake_request):
        jira_client.add_watchers(CFG, "AAA-42", ["acc1", "", "acc2"])
    # 빈 문자열은 건너뛴다 → 2회.
    assert calls == [
        ("POST", "/rest/api/3/issue/AAA-42/watchers", "acc1"),
        ("POST", "/rest/api/3/issue/AAA-42/watchers", "acc2"),
    ]


# ---------- _request 저수준(urllib 모킹) ----------
class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_request_attaches_basic_header_and_parses_json():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["method"] = req.get_method()
        return _FakeResp(json.dumps({"accountId": "x"}).encode())

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = jira_client._request(CFG, "GET", "/rest/api/3/myself", params={"a": "b"})
    assert out == {"accountId": "x"}
    assert captured["url"] == "https://litbig.atlassian.net/rest/api/3/myself?a=b"
    assert captured["auth"].startswith("Basic ")


def test_request_401_raises_auth_error_without_token():
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(jira_client.JiraAuthError) as ei:
            jira_client._request(CFG, "GET", "/rest/api/3/myself")
    assert "SECRET-TOKEN" not in str(ei.value)


def test_request_500_raises_jira_error():
    import io
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Server Error", {}, io.BytesIO(b"boom")
        )

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(jira_client.JiraError) as ei:
            jira_client._request(CFG, "GET", "/rest/api/3/myself")
    assert not isinstance(ei.value, jira_client.JiraAuthError)


if __name__ == "__main__":
    import sys

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_jira_client ({len(fns)} cases)")
    sys.exit(0)
