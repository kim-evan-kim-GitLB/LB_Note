"""Jira Cloud REST v3 클라이언트 — 서비스 계정 API 토큰(Basic auth) 기반 순수 함수 모듈.

앱 레벨 단일 서비스 계정(사용자별 아님)으로 Jira 를 조회/생성한다. 인증은 base64(email:api_token)
를 `Authorization: Basic ...` 로 실어 보낸다(Jira Cloud 공식 방식). base_url 예: https://litbig.atlassian.net,
경로는 /rest/api/3/... .

설계(google_drive.py 미러링):
  - cfg dict `{base_url, email, api_token}` 를 받는 순수 함수 — store/전역 상태 참조 없음.
  - HTTP 는 **함수 내부에서 지연 import 한 stdlib urllib.request** 로 호출한다(신규 의존성 없음,
    google_drive.py 의 stream_media/revoke 와 동일 방식). 모듈 상단엔 표준 import 만.
  - 에러는 JiraError(일반)·JiraAuthError(401/403)로 감싼다. **api_token 은 예외 메시지·로그에
    절대 싣지 않는다**(cfg 를 통째로 포매팅하지 않는다).
  - Phase 1: 조회 위주 + create_issue/add_watchers 클라이언트 함수는 제공하되 실제 라이브 호출은
    엔드포인트로 노출하지 않는다(단위테스트는 _request 모킹).
"""
from __future__ import annotations

import base64
import json

# createmeta 등에서 allowedValues 가 매우 큰 필드(사용자/버전 목록 등)를 상위 N개로 자른다.
_MAX_ALLOWED_VALUES = 50


class JiraError(RuntimeError):
    """Jira API 호출 일반 실패(비2xx·타임아웃·연결오류·JSON 파싱 실패 등). 토큰 미포함."""


class JiraAuthError(JiraError):
    """인증/권한 실패(401/403) — 서비스 계정 토큰 무효/권한 부족. 엔드포인트가 error_code 로 매핑."""


def _basic_header(cfg: dict) -> str:
    """base64(email:api_token) → 'Basic ...' 헤더 값. 반환값은 로그/예외에 싣지 말 것."""
    email = (cfg.get("email") or "").strip()
    token = cfg.get("api_token") or ""
    raw = f"{email}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _request(
    cfg: dict,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict | list:
    """Jira REST 호출(Basic 헤더 부착) → 파싱된 JSON(dict|list).

    base_url + path 로 요청한다(path 는 '/rest/api/3/...' 형태). params 는 쿼리스트링으로,
    json_body 는 JSON 본문으로 보낸다. 비2xx 는 JiraError(401/403 은 JiraAuthError), 타임아웃/
    연결오류도 JiraError 로 감싼다. **예외 메시지에 토큰/헤더/cfg 를 넣지 않는다.**
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    base = (cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise JiraError("Jira base_url 이 비어 있습니다.")
    url = base + path
    if params:
        # None 값은 제외하고 쿼리스트링 구성.
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean)}"
    headers = {
        "Authorization": _basic_header(cfg),
        "Accept": "application/json",
    }
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — DB/관리자 설정 base_url
            body = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        # 본문은 진단용으로만 읽되, 서비스 응답에 토큰이 담기지 않으므로 상태코드+짧은 사유만 남긴다.
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        if status in (401, 403):
            raise JiraAuthError(f"Jira 인증/권한 실패({status})") from None
        raise JiraError(f"Jira API 오류({status}): {detail}") from None
    except urllib.error.URLError as e:
        # reason 만 남긴다(요청 헤더/토큰 미포함).
        raise JiraError(f"Jira 연결 실패: {e.reason}") from None
    except TimeoutError:
        raise JiraError("Jira 요청 타임아웃") from None
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise JiraError(f"Jira 응답 JSON 파싱 실패: {e}") from None


def verify(cfg: dict) -> dict:
    """연결·인증 확인 — GET /rest/api/3/myself → {accountId, displayName, emailAddress} 만 추려 반환."""
    data = _request(cfg, "GET", "/rest/api/3/myself")
    return {
        "accountId": data.get("accountId"),
        "displayName": data.get("displayName"),
        "emailAddress": data.get("emailAddress"),
    }


def get_projects(cfg: dict, *, max_results: int = 50) -> list[dict]:
    """접근 가능한 프로젝트 목록(페이징 순회) → [{key, name, style}].

    style=project.style('classic'=company-managed | 'next-gen'=team-managed) — 프론트가
    company/team 프로젝트를 구분하는 데 쓴다.
    """
    out: list[dict] = []
    start_at = 0
    while True:
        page = _request(
            cfg,
            "GET",
            "/rest/api/3/project/search",
            params={"startAt": start_at, "maxResults": max_results},
        )
        values = page.get("values") or []
        for p in values:
            out.append(
                {"key": p.get("key"), "name": p.get("name"), "style": p.get("style")}
            )
        if page.get("isLast") or not values:
            break
        start_at += len(values)
        if start_at >= int(page.get("total", 0) or 0):
            break
    return out


def get_issue_types(cfg: dict, project_key: str) -> list[dict]:
    """프로젝트의 이슈타입 목록 → [{id, name}] (에픽/작업 등). project.get 의 issueTypes 사용."""
    data = _request(
        cfg,
        "GET",
        f"/rest/api/3/project/{project_key}",
        params={"expand": "issueTypes"},
    )
    types = data.get("issueTypes") or []
    return [{"id": t.get("id"), "name": t.get("name")} for t in types]


def _parse_allowed_values(field: dict) -> list[dict] | None:
    """createmeta 필드의 allowedValues → [{id, name}] (상위 _MAX_ALLOWED_VALUES 개). 없으면 None.

    allowedValues 항목은 필드 종류마다 키가 다르다(id + name|value|displayName). name 이 없으면
    value/displayName 으로 폴백한다.
    """
    allowed = field.get("allowedValues")
    if not allowed:
        return None
    out: list[dict] = []
    for a in allowed[:_MAX_ALLOWED_VALUES]:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or a.get("value") or a.get("displayName")
        out.append({"id": a.get("id"), "name": name})
    return out or None


def get_create_meta(cfg: dict, project_key: str, issuetype_id: str) -> dict:
    """이슈 생성 폼 메타(신 createmeta API) → {fields: [...]}.

    GET /rest/api/3/issue/createmeta/{projectKeyOrId}/issuetypes/{issueTypeId} (Jira Cloud 신 API,
    구 /createmeta?projectKeys=... 폐기 대체). 프론트가 폼을 렌더할 수 있게 필드 배열로 정규화:
      [{fieldId, name, required, schemaType, allowedValues:[{id,name}]|None}]
    allowedValues 가 너무 크면 상위 50개로 자른다(_parse_allowed_values).
    """
    data = _request(
        cfg,
        "GET",
        f"/rest/api/3/issue/createmeta/{project_key}/issuetypes/{issuetype_id}",
        params={"maxResults": 200},
    )
    # 신 API 는 페이지 형태로 fields 를 'fields' 또는 'values' 키에 담는다(버전차 방어).
    raw_fields = data.get("fields")
    if raw_fields is None:
        raw_fields = data.get("values") or []
    fields: list[dict] = []
    for f in raw_fields:
        schema = f.get("schema") or {}
        fields.append(
            {
                "fieldId": f.get("fieldId") or f.get("key"),
                "name": f.get("name"),
                "required": bool(f.get("required")),
                "schemaType": schema.get("type"),
                "allowedValues": _parse_allowed_values(f),
            }
        )
    return {"fields": fields}


def lookup_account_id(cfg: dict, email: str) -> dict | None:
    """이메일 → 계정 — GET /rest/api/3/user/search?query={email} → 첫 결과 {accountId, displayName}.

    결과가 없으면 None. (엔드포인트는 없을 때 {"accountId": null} 로 감싼다.)
    """
    data = _request(
        cfg, "GET", "/rest/api/3/user/search", params={"query": email}
    )
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return {
        "accountId": first.get("accountId"),
        "displayName": first.get("displayName"),
    }


def _adf(text: str) -> dict:
    """평문 문자열 → Atlassian Document Format(ADF) doc. description 필드용.

    빈 줄로 구분된 문단을 각각 paragraph 로 만든다(단순 변환). Jira v3 는 description 을 ADF 로
    받으므로 평문을 이 헬퍼로 감싼다.
    """
    paragraphs = (text or "").split("\n\n")
    content = []
    for para in paragraphs:
        node: dict = {"type": "paragraph", "content": []}
        if para:
            node["content"].append({"type": "text", "text": para})
        content.append(node)
    if not content:
        content = [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": content}


def create_issue(
    cfg: dict,
    *,
    project_key: str,
    issuetype_id: str,
    summary: str,
    description: str | dict | None = None,
    duedate: str | None = None,
    parent_key: str | None = None,
    assignee_id: str | None = None,
    reporter_id: str | None = None,
    extra_fields: dict | None = None,
) -> dict:
    """이슈 생성 — POST /rest/api/3/issue → {key, id, url}.

    fields 조립 규칙:
      - project={key}, issuetype={id}, summary 는 항상.
      - description: 평문 문자열이면 _adf 로 감싸고, 이미 dict(ADF)면 그대로 싣는다.
      - duedate('YYYY-MM-DD'), parent={key}(Task→Epic 연결, company-managed),
        assignee={id}, reporter={id}(권한 없으면 Jira 가 400 → JiraError 전파).
      - extra_fields 는 마지막에 병합(createmeta 필수 커스텀필드 등을 호출측이 조립).
    url = base_url + '/browse/' + key.
    """
    fields: dict = {
        "project": {"key": project_key},
        "issuetype": {"id": issuetype_id},
        "summary": summary,
    }
    if description is not None:
        fields["description"] = _adf(description) if isinstance(description, str) else description
    if duedate:
        fields["duedate"] = duedate
    if parent_key:
        fields["parent"] = {"key": parent_key}
    if assignee_id:
        fields["assignee"] = {"id": assignee_id}
    if reporter_id:
        fields["reporter"] = {"id": reporter_id}
    if extra_fields:
        fields.update(extra_fields)
    data = _request(cfg, "POST", "/rest/api/3/issue", json_body={"fields": fields})
    key = data.get("key")
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    return {
        "key": key,
        "id": data.get("id"),
        "url": f"{base}/browse/{key}" if key else None,
    }


def add_watchers(cfg: dict, issue_key: str, account_ids: list[str]) -> None:
    """이슈에 워처 추가 — accountId 마다 POST /rest/api/3/issue/{key}/watchers.

    watchers 는 이슈 생성 payload 에 넣을 수 없어 생성 후 별도로 붙인다. body 는 JSON 문자열
    accountId(즉 '"5b10..."'). 각 호출 실패는 그대로 전파(호출측이 best-effort 로 감쌀 수 있다).
    """
    for aid in account_ids:
        if not aid:
            continue
        _request(
            cfg, "POST", f"/rest/api/3/issue/{issue_key}/watchers", json_body=aid
        )
