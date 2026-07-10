"""LB Note Slack 컨트롤 봇 — Socket Mode 독립 프로세스.

특정 Slack 채널에서 비번초기화(셀프서비스·DM), 서버 상태, 공지 브로드캐스트, 요구사항 적재를
처리한다. LB Note FastAPI 를 로컬 루프백(127.0.0.1)으로 호출하며, 관리자 권한은 매 요청마다
단명(60초) admin JWT 를 직접 서명해 사용한다(장기 토큰 저장 없음).

설계: docs/2026-07-09-slack-control-bot.md
"""
