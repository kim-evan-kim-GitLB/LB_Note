"""participants.json 기반 사용자 계정 일괄 생성/갱신 스크립트(일회성·재실행 가능).

각 참가자에 대해:
  - username   = email (예: peter@ltbig.com)
  - password   = 공통 초기 비번(기본 axlead1234) — upsert 라 재실행 시 비번이 초기화됨에 유의
  - displayName = canonical(한글 이름)
  - englishName = email @앞부분을 보기 좋게 포맷(jenny.lee -> "Jenny Lee") — 아바타 이니셜·보조표기
  - jobTitle    = participants.json 의 role(직함). 권한 role 과 별개.
  - 권한 role   = 전원 'user' (관리자 별도 미부여)

실행(가상환경은 sudo 필요):
    sudo .venv/bin/python tools/bulk_create_users.py
    sudo .venv/bin/python tools/bulk_create_users.py --password axlead1234 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.web.auth import UserStore  # noqa: E402

DEFAULT_PARTICIPANTS = PROJECT_ROOT / "config" / "participants.json"


def english_name_from_email(email: str) -> str:
    """이메일 @앞부분 -> 보기 좋은 영어 이름. '.'/'_' 를 공백으로, 각 토큰 첫 글자 대문자."""
    local = email.split("@", 1)[0]
    tokens = local.replace(".", " ").replace("_", " ").split()
    return " ".join(t[:1].upper() + t[1:] for t in tokens) or local


def main() -> int:
    ap = argparse.ArgumentParser(description="participants.json 기반 계정 일괄 생성")
    ap.add_argument("--participants", type=Path, default=DEFAULT_PARTICIPANTS)
    ap.add_argument("--password", default="axlead1234", help="공통 초기 비밀번호")
    ap.add_argument("--role", default="user", help="권한 role(전원 동일)")
    ap.add_argument("--dry-run", action="store_true", help="DB 변경 없이 미리보기만")
    args = ap.parse_args()

    data = json.loads(args.participants.read_text(encoding="utf-8"))
    parts = data.get("participants", [])
    if not parts:
        print("참가자가 없습니다.", file=sys.stderr)
        return 1

    store = UserStore()
    existing = set(store.usernames())

    created, updated, rows = 0, 0, []
    for p in parts:
        email = (p.get("email") or "").strip()
        if not email:
            continue
        canonical = (p.get("canonical") or email).strip()
        job_title = (p.get("role") or "").strip() or None
        eng = english_name_from_email(email)
        is_new = email not in existing
        rows.append((email, canonical, eng, job_title, "신규" if is_new else "갱신"))
        if not args.dry_run:
            store.upsert(
                email,
                args.password,
                display_name=canonical,
                role=args.role,
                english_name=eng,
                job_title=job_title,
            )
        created += int(is_new)
        updated += int(not is_new)

    # 보고(한글)
    w_email = max(len(r[0]) for r in rows)
    w_kr = max(len(r[1]) for r in rows)
    w_en = max(len(r[2]) for r in rows)
    print(f"{'이메일(계정)':<{w_email}}  {'한글이름':<{w_kr}}  {'영어이름':<{w_en}}  직함  상태")
    print("-" * (w_email + w_kr + w_en + 20))
    for email, kr, en, title, state in rows:
        print(f"{email:<{w_email}}  {kr:<{w_kr}}  {en:<{w_en}}  {title or '-'}  {state}")

    verb = "미리보기" if args.dry_run else "처리 완료"
    print(f"\n{verb}: 총 {len(rows)}명 (신규 {created} / 갱신 {updated}). 비번='{args.password}', 권한 role='{args.role}'.")
    if not args.dry_run:
        print(f"DB: {store.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
