# LB Note UX 캡처 매니페스트 (데모 스택 :3010 → :8010, 실데이터 무접촉)

스크린샷 디렉터리: `/tmp/claude-1000/-app/198729b6-d7c8-4244-a80a-71c7513343e8/scratchpad/ux/`

| 파일 | 화면 |
|---|---|
| 01-login.png | 로그인 |
| 02-dashboard.png | 대시보드 — 통계 카드(전체 회의/회의 기록/액션 아이템), 최신 회의 기록, 오늘의 일정, AI 분석 요약 |
| 03-recorder.png | 새 회의 시작(녹음) |
| 04-file-upload.png | 파일 업로드(단일 파일) |
| 05-action-items.png | 회의별 액션 |
| 06-action-extraction.png | 텍스트 액션 추출 |
| 07-calendar.png | 캘린더 회의(Google 연동) |
| 08-member-insights.png | 멤버 인사이트 |
| 09-system-settings.png | 시스템 설정 |
| 10-history.png | 회의 기록 목록 |
| 11-review.png / 11b-review-full.png | 회의록 검토(참석자·회의 요약[안건 개요+상세]·액션·자료첨부·외부연동), 11b는 전체 스크롤 |
| 12-user-menu.png | 사용자 메뉴(사용자 설정 / 관리자 콘솔 / 로그아웃) |
| 13-gmail-modal.png | 검토에서 GMAIL 전송 클릭 — 회색 비활성(OAuth 미연동) 상태 |
| 14-drive-modal.png | DRIVE로 내보내기 클릭 상태 |
| 15-admin-console.png | 관리자 콘솔 |
| 20-mobile-dashboard.png | 모바일 390px 대시보드 (※ '새 회의 시작' 버튼 세로 깨짐) |
| 21-mobile-review.png | 모바일 390px 검토 화면 |

## 소스 위치(동작/게이트 확인용)
- 프론트: `/home/evan/meetscript-ai/src/components/*.tsx`, `src/App.tsx`, `src/services/*.ts`, `src/lib/*.ts`
- 백엔드: `/app/src/web/*.py`
- 이메일 전송 모달 동작: `src/components/ReviewView.tsx`(onSend {to,cc,subject}) — 본문은 자동요약 고정 여부 확인
- 날짜 렌더: createdAt = 파이썬 isoformat(마이크로초 6자리) → JS Date 파싱 실패로 "Invalid Date" 노출
