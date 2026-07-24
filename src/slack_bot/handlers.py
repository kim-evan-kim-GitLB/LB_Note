"""Slack 서브명령 로직 — 각 함수가 채널 응답 문자열을 반환(부작용은 넘겨받은 slack_client 로).

slack_bolt 를 import 하지 않는다(bot.py 전용). LB Note 호출은 lbnote_client 모듈로 한다.
"""
from __future__ import annotations

import secrets

from src.slack_bot import config, conversation, lbnote_client

# 임시 비밀번호 최소 길이(LB Note MIN_PASSWORD_LEN=8 이상 보장). token_urlsafe(12)=약 16자.
_TEMP_PW_BYTES = 12


def handle_reset(slack_client, user_id: str) -> str:
    """본인 셀프서비스 비번초기화 → 임시비번 DM. 채널엔 비번 미노출 안내만 반환.

    본인 증명: Slack 프로필 이메일 == LB Note username(정확 매칭). 매칭/존재 여부는 계정 열거
    방지를 위해 모호하게 안내한다.
    """
    try:
        info = slack_client.users_info(user=user_id)
        email = (info["user"]["profile"] or {}).get("email")
    except Exception:
        email = None
    if not email:
        return (
            "요청자의 이메일을 확인할 수 없어 비밀번호를 초기화할 수 없습니다. "
            "Slack 프로필에 회사 이메일이 설정되어 있는지 확인해 주세요."
        )
    username = email
    temp_pw = secrets.token_urlsafe(_TEMP_PW_BYTES)  # 약 16자(>=12, MIN 8 충족)
    try:
        lbnote_client.reset_password(username, temp_pw)
    except lbnote_client.UserNotFound:
        return (
            "해당 이메일과 일치하는 LB Note 계정을 찾지 못했습니다. "
            "관리자에게 문의해 주세요."
        )
    except lbnote_client.LBNoteError:
        return "비밀번호 초기화 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    # 임시 비번은 DM 으로만 전달(채널 노출 절대 금지).
    try:
        slack_client.chat_postMessage(
            channel=user_id,
            text=(
                "LB Note 임시 비밀번호가 발급되었습니다.\n"
                f"임시 비밀번호: `{temp_pw}`\n"
                "최초 로그인 시 비밀번호를 반드시 변경해야 합니다."
            ),
        )
    except Exception:
        return "임시 비밀번호 DM 전송에 실패했습니다. 잠시 후 다시 시도해 주세요."
    return "임시 비밀번호를 DM 으로 보냈습니다. 확인해 주세요."


def handle_status(client) -> str:
    """일반 사용자용 서비스 상태 — '정상/주의' 한 줄 판정만. 내부 지표는 노출하지 않는다.

    이 봇의 목적: 관리자가 없을 때 사용자가 '지금 LB Note 를 써도 되는지' 스스로 판단하고,
    문제면 무엇을 해야 하는지 안다. 그래서 백엔드명·인증·GPU·디스크 수치 같은 운영 지표는 감추고
    사용자 관점의 판정과 조치만 돌려준다. 절대 예외를 상위로 던지지 않는다(에러도 문자열).
    """
    # 1) 서버 응답(health) — 사용자에게 가장 중요한 '지금 되나?' 신호.
    try:
        client.health()
    except Exception:  # noqa: BLE001
        return (
            "⚠️ 지금 LB Note 에 연결되지 않아요.\n"
            "잠시 후 다시 시도해 주세요. 계속되면 관리자에게 알려주세요."
        )
    # 2) 저장 공간이 거의 차면 업로드가 실패할 수 있어 미리 알린다(수치는 숨김). metrics 는
    #    관리자 조회라 실패할 수 있으나, 서버가 응답한 이상 사용에는 지장 없어 정상으로 본다.
    try:
        pct = (client.metrics().get("disk") or {}).get("percent")
        if isinstance(pct, (int, float)) and pct >= 90:
            return (
                "⚠️ 저장 공간이 거의 찼어요.\n"
                "회의록 업로드가 실패할 수 있으니 관리자에게 알려주세요."
            )
    except Exception:  # noqa: BLE001
        pass
    return (
        "✅ LB Note 정상 이용 가능\n"
        "지금 회의록 업로드와 요약을 사용하실 수 있어요."
    )


def handle_notice(slack_client, channel_id: str, user_id: str) -> str:
    """공지 조회/배포. 내용 작성·게시 결정은 웹 관리자 콘솔에서 이뤄지고, 봇은 최신 활성 공지를 읽어온다.

    - **모든 사용자**: 현재 등록된 공지를 요청한 자리에서 '읽어' 보여준다(가져오기).
    - **관리자(role=admin)**: 추가로 SLACK_NOTICE_CHANNEL(없으면 명령 채널)에 브로드캐스트한다(밀어주기).

    읽기는 누구에게나 연다(공지는 사용자에게 보이라고 쓴 것). '아무나 공지 채널에 브로드캐스트'만
    관리자로 막는다.
    """
    # 1) 최신 공지 조회(누구나 읽기 가능).
    try:
        notice = lbnote_client.get_latest_notice()
    except Exception:
        return "공지 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    if not notice or not (notice.get("body") or "").strip():
        return "현재 등록된 공지가 없습니다."
    title = (notice.get("title") or "").strip()
    body = notice["body"].strip()
    text = "📢 *공지*\n" + (f"*{title}*\n" if title else "") + body

    # 2) 요청자 권한 확인 — 관리자면 공지 채널에 브로드캐스트, 아니면 이 자리에서 읽어 보여줌.
    role = None
    try:
        info = slack_client.users_info(user=user_id)
        email = (info["user"]["profile"] or {}).get("email")
        if email:
            role = lbnote_client.get_user_role(email)
    except Exception:
        role = None  # 확인 실패 시 안전하게 '읽기'로 처리(브로드캐스트 안 함).
    if role == "admin":
        target = config.SLACK_NOTICE_CHANNEL or channel_id
        try:
            slack_client.chat_postMessage(channel=target, text=text)
        except Exception:
            return "공지 게시에 실패했습니다. 채널 설정을 확인해 주세요."
        return f"공지를 <#{target}> 채널에 게시했습니다."
    # 일반 사용자(또는 권한 확인 불가): 읽어서 이 자리에 보여준다.
    return text


# ---------- 요구사항: 스레드 되묻기 대화 ----------
# 사용자가 `요구사항` 을 치면 그 메시지의 스레드(댓글)에서 '입력받기 → 저장 → 추가?' 를 반복한다.
# 정확히 '예' 일 때만 추가 입력을 더 받고, 그 외 응답은 대화를 종료한다.
_MSG_PROMPT = "입력해주시면 제가 저장할게요."
_MSG_CONFIRM_MORE = "저장이 완료되었어요. 추가적으로 입력하고 싶으신게 있으신가요? (예가 아니면 대화를 종료합니다)"
_MSG_END = "요구사항 접수를 종료합니다. 감사합니다."
_MSG_SAVE_ERR = "요구사항 접수 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
_YES = "예"  # '예라고 정확하게 입력할 때만' 추가 진행.


def _save_and_ask_more(
    client, channel: str, thread_ts: str, user: str, content: str, reporter: str | None
) -> str:
    """요구사항 1건 저장 → 성공 시 awaiting_more 로 전이하고 '추가?' 안내.

    실패 시 상태를 awaiting_text 로 유지(전이 안 함)해 같은 스레드에서 재입력할 수 있게 한다.
    """
    try:
        client.create_requirement(content, reporter)
    except Exception:
        return _MSG_SAVE_ERR
    conversation.set_state(channel, thread_ts, user, conversation.STATE_AWAITING_MORE)
    return _MSG_CONFIRM_MORE


def requirement_start(
    client, channel: str, thread_ts: str, user: str, inline_text: str, reporter: str | None
) -> str:
    """`요구사항` 최초 트리거. 반환 문자열은 스레드(thread_ts)에 게시한다.

    - thread_ts 없음(슬래시 등 스레드 불가): 되묻기 대화 없이 한 방 저장/안내(하위호환).
    - 인라인 내용 없음: 대화 시작 후 입력 요청.
    - 인라인 내용 있음: 바로 저장하고 '추가?' 로 이어감.
    """
    content = (inline_text or "").strip()
    if not thread_ts:
        if not content:
            return "요구사항 내용을 입력해 주세요. 예) `요구사항 화자분리 기능`"
        try:
            created = client.create_requirement(content, reporter)
        except Exception:
            return _MSG_SAVE_ERR
        return (
            f"요구사항이 접수됐어요. (접수번호 #{created.get('id')})\n"
            "확인 후 반영하겠습니다. 감사합니다."
        )
    conversation.start(channel, thread_ts, user, conversation.STATE_AWAITING_TEXT)
    if not content:
        return _MSG_PROMPT
    return _save_and_ask_more(client, channel, thread_ts, user, content, reporter)


def requirement_reply(
    client, channel: str, thread_ts: str, user: str, text: str, reporter: str | None
) -> str | None:
    """진행 중 되묻기 대화의 스레드 답글 한 스텝. 관리 대상이 아니면 None(무시).

    - awaiting_text: 답글을 요구사항으로 저장 → awaiting_more, '추가?' 반환.
    - awaiting_more: 정확히 '예' → awaiting_text, 입력 요청 / 그 외 → 종료.
    """
    state = conversation.get(channel, thread_ts, user)
    if state is None:
        return None
    body = (text or "").strip()
    if state == conversation.STATE_AWAITING_MORE:
        if body == _YES:
            conversation.set_state(channel, thread_ts, user, conversation.STATE_AWAITING_TEXT)
            return _MSG_PROMPT
        conversation.clear(channel, thread_ts)
        return _MSG_END
    # STATE_AWAITING_TEXT
    if not body:
        return _MSG_PROMPT  # 빈 답글 → 다시 요청
    return _save_and_ask_more(client, channel, thread_ts, user, body, reporter)


def help_text() -> str:
    """전체 서브명령 사용법(한글) — 각 기능의 사용 예시 포함."""
    return (
        "*LB Note Bot 사용법*\n"
        "`@LBNoteBot <명령>` 또는 `/lbnote <명령>` 으로 부릅니다.\n"
        "\n"
        "*상태* / `status` — 지금 LB Note 를 이용할 수 있는지 확인\n"
        "  · 예) `@LBNoteBot 상태`\n"
        "\n"
        "*비번초기화* / `reset` — 본인 비밀번호 초기화 → 임시비번을 DM 으로 받음\n"
        "  · Slack 프로필 이메일 = LB Note 계정 기준\n"
        "  · 예) `@LBNoteBot 비번초기화`\n"
        "\n"
        "*요구사항* / `req` — 요구사항·건의 접수(부르면 그 메시지 *스레드(댓글)* 에서 진행)\n"
        "  · `@LBNoteBot 요구사항` → 스레드에 내용 입력 → 저장 후 '추가?' 물으면\n"
        "    정확히 `예` 면 계속, 그 외 입력이면 종료\n"
        "  · 한 줄로: `@LBNoteBot 요구사항 화자분리 기능` (바로 저장 후 추가 여부 확인)\n"
        "\n"
        "*공지* / `notice` — 현재 공지를 확인(내용은 웹 콘솔에서 작성)\n"
        "  · 누구나 확인 가능. 관리자가 부르면 공지 채널에 배포까지 진행\n"
        "  · 예) `@LBNoteBot 공지`\n"
        "\n"
        "*help* — 이 도움말"
    )
