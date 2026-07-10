"""Slack 서브명령 로직 — 각 함수가 채널 응답 문자열을 반환(부작용은 넘겨받은 slack_client 로).

slack_bolt 를 import 하지 않는다(bot.py 전용). LB Note 호출은 lbnote_client 모듈로 한다.
"""
from __future__ import annotations

import secrets
import subprocess

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
    """health + metrics(+GPU) 요약 블록. 절대 예외를 상위로 던지지 않는다(에러도 문자열)."""
    lines: list[str] = ["*LB Note 서버 상태*"]
    try:
        h = client.health()
        lines.append(
            "- 백엔드: clean="
            f"{h.get('clean_backend')} / extract={h.get('extract_backend')} / "
            f"summarize={h.get('summarize_backend')}"
        )
        lines.append(f"- 인증 사용자 수: {h.get('auth_users')}")
        ca = h.get("claude_auth") or {}
        lines.append(f"- Claude 인증: ok={ca.get('ok')} ({ca.get('reason', '')})")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- health 조회 실패: {e}")
    try:
        m = client.metrics()
        disk = m.get("disk") or {}
        lines.append(
            f"- 회의록: {m.get('meetings')}건 / 백업: {m.get('backups')}건"
        )
        if disk:
            used_gb = (disk.get("used") or 0) / (1024**3)
            total_gb = (disk.get("total") or 0) / (1024**3)
            lines.append(
                f"- 디스크: {used_gb:.1f}/{total_gb:.1f} GB ({disk.get('percent')}%)"
            )
    except Exception as e:  # noqa: BLE001
        lines.append(f"- metrics 조회 실패: {e}")
    gpu = _gpu_line()
    if gpu:
        lines.append(gpu)
    return "\n".join(lines)


def _gpu_line() -> str | None:
    """nvidia-smi 로 GPU 사용률/메모리 1줄. 없으면 None(예외 삼킴)."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        first = out.stdout.strip().splitlines()[0]
        util, mem_used, mem_total = (p.strip() for p in first.split(","))
        return f"- GPU: 사용률 {util}% / 메모리 {mem_used}/{mem_total} MB"
    except Exception:
        return None


def handle_notice(slack_client, text: str, channel_id: str) -> str:
    """공지 브로드캐스트 — SLACK_NOTICE_CHANNEL(없으면 명령 채널)로 게시."""
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
    return f"요구사항 접수 완료 (#{created.get('id')})"


def help_text() -> str:
    """전체 서브명령 사용법(한글)."""
    return (
        "*LB Note Bot 사용법*\n"
        "`@LBNoteBot <서브명령>` 또는 `/lbnote <서브명령>`\n"
        "- `상태` / `status` — 서버 상태(백엔드·인증·디스크·GPU) 요약\n"
        "- `비번초기화` / `reset` — 본인 LB Note 비밀번호 초기화(임시비번 DM 전송)\n"
        "- `공지 <내용>` / `notice <내용>` — 지정 채널에 공지 브로드캐스트\n"
        "- `요구사항 <내용>` / `req <내용>` — 요구사항/건의 접수\n"
        "- `help` — 이 도움말"
    )
