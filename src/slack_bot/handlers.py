"""Slack 서브명령 로직 — 각 함수가 채널 응답 문자열을 반환(부작용은 넘겨받은 slack_client 로).

slack_bolt 를 import 하지 않는다(bot.py 전용). LB Note 호출은 lbnote_client 모듈로 한다.
"""
from __future__ import annotations

import secrets

from src.slack_bot import config, lbnote_client

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


def handle_notice(slack_client, text: str, channel_id: str, user_id: str) -> str:
    """공지 브로드캐스트 — **LB Note 관리자(role=admin)만** 배포 가능.

    공지는 관리자→사용자 방향이라 다른 셀프서비스 명령과 달리 권한 게이트를 둔다. 요청자 Slack
    이메일→LB Note 계정 role 을 확인해 admin 이 아니면 거부한다(SLACK_NOTICE_CHANNEL 없으면 명령 채널).
    """
    # 1) 관리자 권한 확인(요청자 이메일 == LB Note username 가정).
    try:
        info = slack_client.users_info(user=user_id)
        email = (info["user"]["profile"] or {}).get("email")
    except Exception:
        email = None
    if not email:
        return (
            "공지 권한을 확인할 수 없습니다. "
            "Slack 프로필에 회사 이메일이 설정되어 있는지 확인해 주세요."
        )
    try:
        role = lbnote_client.get_user_role(email)
    except Exception:
        return "공지 권한 확인 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    if role != "admin":
        return "공지는 관리자만 배포할 수 있습니다."
    # 2) 관리자 확인됨 → 브로드캐스트.
    target = config.SLACK_NOTICE_CHANNEL or channel_id
    try:
        slack_client.chat_postMessage(channel=target, text=f"📢 *공지*\n{text}")
    except Exception:
        return "공지 게시에 실패했습니다. 채널 설정을 확인해 주세요."
    return f"공지를 <#{target}> 채널에 게시했습니다."


def handle_requirement(client, text: str, reporter: str | None) -> str:
    """요구사항 적재 → 접수번호 반환."""
    try:
        created = client.create_requirement(text, reporter)
    except Exception:
        return "요구사항 접수 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    return (
        f"요구사항이 접수됐어요. (접수번호 #{created.get('id')})\n"
        "확인 후 반영하겠습니다. 감사합니다."
    )


def help_text() -> str:
    """전체 서브명령 사용법(한글)."""
    return (
        "*LB Note Bot 사용법*\n"
        "`@LBNoteBot <서브명령>` 또는 `/lbnote <서브명령>`\n"
        "- `상태` / `status` — 지금 LB Note 를 이용할 수 있는지 확인\n"
        "- `비번초기화` / `reset` — 본인 LB Note 비밀번호 초기화(임시비번 DM 전송)\n"
        "- `공지 <내용>` / `notice <내용>` — 공지 배포(관리자 전용)\n"
        "- `요구사항 <내용>` / `req <내용>` — 요구사항/건의 접수\n"
        "- `help` — 이 도움말"
    )
