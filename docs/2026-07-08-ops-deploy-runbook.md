# 운영/배포 Runbook (meetscript-ai, 171 서버)

작성 2026-07-08. 이 세션에서 확인/정정된 운영 지식을 한곳에 모은다. dev 컨테이너에서의
서버 운영, 컨테이너->호스트 배포 워크플로, 도메인/IP 동작, 배포본 자격증명 이슈를 다룬다.

> 표기: 명령블록은 복사용이라 ASCII 로만 쓴다(화살표는 `->`, 하이픈은 `-`).

---

## 0. 토폴로지 한 장

| 구분 | 위치 | 정체 |
|---|---|---|
| 개발(dev) | 컨테이너 `/app`(백엔드 LB_Note) + `/home/evan/meetscript-ai`(프론트 LB_Note-web) | git 클론. 편집/검증/커밋을 여기서 함 |
| 배포(deploy) | 호스트 `evan@121.125.78.171` : `/home/evan/LB_Note-deploy`(백엔드=배포 repo) + `/home/evan/LB_Note-web`(프론트) | docker compose 로 단일 이미지 빌드/기동 |

- 컨테이너와 호스트는 **파일시스템이 안 통한다. 유일한 다리는 GitHub(git)다.**
- 컨테이너엔 **docker 가 없다.** `docker ...` 계열은 전부 호스트에서 실행한다.
- 레포 2트랙: 백엔드 `LB_Note`, 프론트 `LB_Note-web`. 커밋/push/pull 이 각각 따로 돈다.

---

## 1. dev 서버 운영 (:8000, 컨테이너 안)

dev 는 백엔드 FastAPI 만 :8000 에서 API 로 뜨고, 프론트는 Vite dev(별도)가 서빙한다
(`WEB_FRONTEND_DIST` 미설정 -> 백엔드는 /api 만 담당).

### 1.1 기동 명령 (PATH 주입 필수)

```
sudo env "PATH=/home/evan/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin" \
     WEB_PORT=8000 setsid nohup /app/.venv/bin/python -m src.web \
     >/app/output/web-server.log 2>&1 < /dev/null &
```

- 로그: `/app/output/web-server.log`
- 헬스: `curl -s http://127.0.0.1:8000/api/health`

### 1.2 [함정] sudo 로 띄우면 claude 를 못 찾아 요약/액션이 조용히 실패

- 증상: STT 는 정상 완료되는데(로컬 GPU) **요약/액션아이템만 실패**, 프론트는 "요약중"
  스피너만 남는다. GPU 사용률 0% + 서버 자식 프로세스에 `claude` 가 안 잡힘.
- 원인: 서버를 `sudo ... python -m src.web` 로 띄우면 sudo 가 PATH 를 secure_path 로
  리셋해 **`/home/evan/.npm-global/bin`(여기에 claude 있음)이 빠진다.** 요약/추출 백엔드
  `agent_cli` 가 `shutil.which("claude")` 로 탐색 -> None -> RuntimeError.
  (`src/postprocess/backends/agent_cli.py`)
- root 자체는 claude 실행 능력 있음(node `/usr/bin/node` 존재). **바이너리 PATH 탐색만** 깨짐.
- 해결: **기동 시 PATH 에 `/home/evan/.npm-global/bin` 을 주입**(위 1.1 명령).
- 안 되는 방법: `AGENT_CLI_PROGRAM=<절대경로>` 는 무효. `_build_argv` 가 program!="claude"
  면 에러로 막고(코드), argv[0] 도 문자열 "claude" 로 하드코딩되어 있어 무조건 PATH 를 탄다.
- durability: 예전 명령(`sudo ... python -m src.web`)으로 그냥 띄우면 재발. 배포
  컨테이너에는 claude 를 공용 경로(예: `/usr/local/bin`)에 두거나 이미지에 포함해야 함.

### 1.3 재기동 (PID 지정, pkill 자기매치 주의)

```
# 현재 PID 확인
ps -eo pid,args | grep -E "-m src\.web" | grep -v grep
# 정확히 PID 로 종료 (pkill -f "src.web" 은 실행 중인 자기 셸까지 매치해 스크립트가 죽으니 금지)
sudo kill <PID_python> <PID_sudo_wrapper>
# 위 1.1 명령으로 재기동
```

프론트(Vite dev)는 작업트리를 `git checkout main && git pull` 로 갱신하면 HMR 로 반영된다.

---

## 2. 배포 워크플로 (컨테이너 -> 호스트)

반영은 반드시 git 을 통과한다. 호스트 파일을 컨테이너에서 직접 건드릴 수 없다.

### STEP 1. 컨테이너 안 - 편집/검증/커밋 (여기서 함)

```
# 프론트
cd /home/evan/meetscript-ai && npm run build   # tsc 포함 검증
git add <scope files>                           # git add -A 금지
git commit -m "..."

# 백엔드
cd /app
sudo PYTHONPATH=/app .venv/bin/python -m pytest -q   # + ruff
git add <scope files>
git commit -m "..."
```

- 브랜치 규칙: default 직접 커밋 금지. `feature/171-<주제>` 로 분기. 커밋 메시지에
  Jira 키(예: `[WDLABD2411-543]`) 포함. 공유 작업트리라 커밋 직전 `git branch --show-current`
  로 171 브랜치 확인.

### STEP 2. push + PR + 병합 (컨테이너 안)

```
cd /home/evan/meetscript-ai && git push -u origin <branch>   # LB_Note-web
cd /app && git push -u origin <branch>                        # LB_Note
# gh pr create ... ; gh pr merge <num> --merge
```

### STEP 3. 호스트에서 pull + 재빌드 (사용자, docker 는 호스트에만 있음)

**정정(중요): 백엔드=배포 repo 는 `LB_Note-deploy` 하나다.** `deploy/` 디렉토리가 백엔드
repo(LB_Note) 안에 들어있어서, 호스트의 백엔드 클론이 곧 compose 를 돌리는 배포 repo 다.
compose 의 백엔드 빌드 컨텍스트 기본값이 `..`(=deploy/ 의 상위 = LB_Note-deploy 루트)라
`BACKEND_DIR` 을 안 줘도 그 repo 루트를 쓴다. (이전 워크플로 문서의 "별도 BACKEND_DIR pull"
은 추론이었고 부정확 - 실제로는 아래처럼 2개 pull.)

```
ssh evan@121.125.78.171

# 1) 프론트 클론 최신화 (LB_Note-web = FRONTEND_DIR)
cd /home/evan/LB_Note-web && git fetch origin && git checkout main && git pull --ff-only

# 2) 백엔드=배포 repo 최신화 (LB_Note-deploy, 안에 deploy/ 포함)
cd /home/evan/LB_Note-deploy && git fetch origin && git checkout main && git pull --ff-only

# 3) 재빌드 (프론트+백엔드 단일 이미지로 함께)
cd /home/evan/LB_Note-deploy/deploy
docker compose --env-file .env.deploy up -d --build

# 4) 확인
docker compose --env-file .env.deploy ps
curl -k https://121.125.78.171:49152/api/health
```

- `.env.deploy` 의 `FRONTEND_DIR` 는 `/home/evan/LB_Note-web` 로 잡혀 있어야 최신 프론트
  dist 가 빌드에 들어간다.
- `compose up --build` 한 번이 프론트(`npm run build`->dist)+백엔드를 한 이미지로 굽는다.

---

## 3. 배포 구조 (:49152)

- 컨테이너 2개: `meetscript-171`(단일 이미지, FastAPI :8088 + 프론트 dist 정적 서빙) +
  `meetscript-caddy-171`(caddy, 호스트 :49152 -> 컨테이너 :443, TLS 종단).
- 접속: `https://<host>:49152`. 자체서명 인증서(첫 1회 브라우저 경고).
- 헬스: `curl -k https://<host>:49152/api/health`

---

## 4. 도메인 <-> IP: 왜 IP 로 굳는가 (현재 미적용, 참고용)

DNS 는 정상이다: `lbnote.litbig.com -> 121.125.78.171`, `:49152` 도 200 응답.
그런데 앱이 도메인과 IP **둘 다 똑같이 응답**하고 IP 를 도메인으로 되돌리는 장치가 없어,
사용자가 먼저 탄 호스트가 그대로 굳는다.

근거:
- Caddy `:443` 블록은 호스트 매칭 없이 프록시만 함 -> IP->도메인 리다이렉트 없음.
- `WEB_FRONTEND_ORIGIN` 미설정 -> OAuth 콜백 후 `_google_redirect` 가 **상대경로
  `/settings`** 로 이동 -> 들어온 호스트(IP 면 IP)에 그대로 머묾.
- dev DB `app_oauth_config.redirect_uri` 는 도메인이 맞음(확인). 배포 DB 는 컨테이너에서
  못 읽으니 관리자 콘솔에서 도메인인지 확인 필요.

고정하려면(적용 시):
1. Caddy 에 IP->도메인 301 리다이렉트 추가(`deploy/Caddyfile`). TLS 핸드셰이크가 먼저라
   인증서 SAN 에 IP 도 있어야 함.
2. `.env.deploy` 에 `WEB_FRONTEND_ORIGIN=https://lbnote.litbig.com:49152`(콜백 후 절대
   도메인 리다이렉트).
3. `.env.deploy` 의 `SITE_HOST=lbnote.litbig.com,121.125.78.171` 로 두고 `gen-cert.sh`
   재실행(인증서 SAN 에 도메인 포함 -> 도메인 접속 시 이름불일치 경고 제거).

> 결정(2026-07-08): 현재는 **변경 없이 그대로 두기로 함.** 위는 나중에 고정할 때 참고.

---

## 5. [함정] 배포본 claude_auth: no_credentials

- 배포(:49152) `/api/health` 에서 `claude_auth: {ok:false, reason:"no_credentials"}` 확인됨.
- 요약/추출 백엔드가 `agent_cli`(claude)인데 컨테이너 안에 claude 자격증명이 없으면
  **회의 처리 시 STT 까지만 되고 요약/액션 생성이 실패**한다.
- 이번 Docs 렌더 수정/사용자 관리 기능은 이와 별개로 동작한다(기존 요약이 있으면 렌더는 정상).
- 해결: (a) 사용자별 자격증명(웹 설정에서 각자 Claude 토큰/키 등록) 또는 (b) 전역 폴백
  (`~/.claude/.credentials.json` 또는 `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`)을
  컨테이너에 provision. 1.2 의 PATH 함정과는 별개 이슈다(이건 자격증명, 저건 바이너리 경로).

---

## 6. 빠른 함정 요약

| 증상 | 원인 | 해결 |
|---|---|---|
| dev 에서 STT 는 되는데 요약/액션 실패 | sudo PATH 가 claude 를 못 봄 | 기동 시 PATH 에 npm-global bin 주입(1.1) |
| 배포에서 새 회의 요약 안 됨 | claude_auth no_credentials | 자격증명 provision(5장) |
| 도메인이 IP 로 굳음 | canonical host 강제 없음 | Caddy redir + WEB_FRONTEND_ORIGIN + SITE_HOST(4장, 미적용) |
| 재기동 시 스크립트가 죽음 | `pkill -f src.web` 자기매치 | PID 로 kill(1.3) |
| 배포 문서가 안 보임 | DEPLOY.md/DEV_DEPLOY_WORKFLOW.md 가 main 에 없음 | 별도 복구 필요(미완) |
| 라이브 "Google 연결 시간 초과" | DB client_secret 불일치(invalid_client) | 관리자 콘솔서 secret 재입력(7.2) |
| ssh -L 이 bind Permission denied | Windows 예약 포트(WinNAT), 서버 무관 | VS Code Ports 포워딩 or 로컬포트 변경(7.4) |
| dev 컨테이너 좀비 누적 | PID1=sleep infinity reaper 부재 | --init 붙여 recreate(호스트, 7.1) |

---

## 7. 2026-07-21 업데이트 (배포 init, OAuth 환경분리, dev 포트, 접속 트러블슈팅)

이 시점부터 dev 백엔드 기본 포트가 바뀌었고(1장 :8000 표기는 과거값), 아래 항목들이 추가/정정됨.

### 7.1 좀비 프로세스 / 컨테이너 init

- 원인: 컨테이너 PID1 이 `python`(배포 웹) 또는 `sleep infinity`(dev)라 고아 자식을 reap 못함
  -> 좀비 무한 누적. agent_cli(claude)가 띄우는 node 자식이 주 발생원. dev 에서 수만 개 관측.
- 좀비는 **포트/메모리를 쥐지 않는다**(이미 죽음). PID 슬롯만 점유 -> 극단 누적 시 fork 실패
  위험. `kill` 무의미(이미 죽은 프로세스). "과거 PID 가 포트 점유" 는 오진 - 죽으면 커널이
  소켓 즉시 회수한다.
- 배포: `deploy/docker-compose.yml` 의 meetscript/slackbot 에 `init: true` 추가(머지됨).
  적용 = `docker compose --env-file .env.deploy up -d`(재빌드 불필요, 재생성 시 기존 좀비 소멸).
- dev 컨테이너: 미적용. `docker restart` 는 임시(재발), 근본해결은 `--init`(tini) 붙여
  recreate. **컨테이너 안에서 docker 불가 -> 호스트에서.** recreate 전 `docker inspect
  lbnote_dev` 로 볼륨/포트/GPU 옵션 확보. dev 웹서버는 수동기동이라 재생성 후 재기동 필요.

### 7.2 Google OAuth: 환경별 콜백 분리 + invalid_client 함정

- redirect_uri 진실원천 = **DB `app_oauth_config`**(관리자 콘솔 설정, `.env` 를 덮음). 매 요청
  DB 에서 읽어 재기동 불필요.
- 환경별 값(2026-07-20 분리):
  - 라이브: `https://lbnote.litbig.com:49152/api/integrations/google/callback`
  - dev: `http://localhost:<dev접속포트>/api/integrations/google/callback` (접속 포트와 일치.
    백엔드 단일오리진이면 그 포트)
  - dev/라이브가 **같은 클라이언트**(`842915815403-3mquu750...`)를 쓰면 Google 콘솔 승인
    URI 에 양쪽 콜백을 **둘 다** 등록해야 함.
- **invalid_client 함정(2026-07-20 실사건)**: 라이브 "연결 시간 초과" 진범 = DB client_secret
  이 클라이언트와 불일치(`invalid_client: The provided client secret is invalid`, 토큰교환
  단계). 프론트는 이걸 "시간 초과"로 뭉갠다. **진단은 `docker logs meetscript-171` 의
  exchange traceback.** 해결 = 관리자 콘솔서 secret 재입력(dev/라이브 각자).
- 설정의 "연결됨"은 **토큰 존재만** 의미(유효성 아님). Google 비번 변경 시 gmail.send 스코프라
  refresh_token 자동폐기 -> 캘린더 "인증 만료" vs 설정 "연결됨" 모순 표시. 해결 = 재연동.

### 7.3 dev 서버 포트 (WEB_PORT) / 실행 모드

- `WEB_PORT`(env/.env)로 바인딩 포트 지정. 2026-07-21 현재 dev `.env` = `WEB_PORT=3000`.
- 두 모드:
  - (A) **단일오리진**: `WEB_FRONTEND_DIST=<프론트 dist>` 설정 시 백엔드가 SPA+API 를 한
    포트에서 서빙. `npm run build` 후 `python -m src.web` 하나로 끝(접속 포트 하나).
  - (B) **Vite dev 분리**: 프론트 `WEB_FRONTEND_PORT=3000 WEB_API_TARGET=http://localhost:<be포트>
    npm run dev`, 백엔드는 별도 포트. HMR 개발용.
- 기동은 1.1 의 PATH 주입 규칙 그대로. 헬스 `curl -s http://127.0.0.1:<port>/api/health`.
- 리스닝 확인: 이 컨테이너는 ss/lsof 가 netlink 제약으로 **빈 출력** -> `/proc/net/tcp`
  직접 파싱(state 0A=LISTEN, 포트 hex)이나 `curl` 로 확인.

### 7.4 dev 접속: `bind ... Permission denied` (Windows 측 문제)

- 증상: `ssh -L <lport>:172.17.0.2:<rport> evan@host` 가 `bind [127.0.0.1]:<lport>:
  Permission denied` 로 실패. 3000, 13000 등 여러 포트 동시.
- **원인 100% 클라이언트(Windows). 서버 포트/자원(좀비 등)과 무관.** `-L` 콜론 앞(로컬 리슨)
  바인드에서 죽는 것이라 원격 `172.17.0.2:<rport>` 은 시도조차 안 됨.
- `Permission denied`(WSAEACCES) != `Address already in use`(WSAEADDRINUSE). 전자는
  **Windows 예약 포트 범위**(Hyper-V/WinNAT/WSL2/Docker Desktop 이 부팅마다 큰 블록 예약,
  재부팅마다 바뀜), 후자만 실제 점유. `172.17.0.2` 대역 = 그 PC 에 Docker Desktop 존재 신호.
- 확인(Windows): `netsh interface ipv4 show excludedportrange protocol=tcp`
- 해결(권장순):
  1. **VS Code Ports 패널 -> Forward a Port -> 3000**. VS Code 가 로컬 포트를 빈 값으로 자동
     배정해 예약범위 회피. 이미 쓰는 도구라 최선.
  2. ssh 고집 시 로컬 포트만 예약 밖 값으로: `ssh -L 23000:172.17.0.2:3000 evan@host`
     -> `http://localhost:23000`. (서버 포트는 안 바꿈.)
- **서버 쪽엔 고칠 것 없음.** 서버 바인딩 포트를 바꿔도 이 에러는 안 사라진다.

### 7.5 신규 기능/노브 (이번 세션 머지)

- 잡 취소 실동작: `POST /api/ai/jobs/{id}/cancel`(소유자만). 대기 잡 즉시 이탈, 실행 잡은
  claude 서브프로세스 그룹 SIGKILL 로 조기 중단. (LB_Note#39)
- STT/LLM 세마포어 분리: `WEB_LLM_CONCURRENCY`(기본 2) 신설 -> A 요약 중 B STT 시작 가능.
  잡 파이프라인이 `transcribe_to_segments`(GPU)/`enrich_to_contract`(LLM) 2단계로 분리됨. (LB_Note#39)
- 녹음 정지 후 '녹음 완료' 핸드오프 화면(원본 PC 저장/분석 시작 분리). (LB_Note-web#31)
- 구글 캘린더 '보는 달' 기준 조회(과거 일정 표시). 백엔드 무변경(timeMin/timeMax 이미 지원). (LB_Note-web#32)

### 7.6 admin 비번 초기화

- 시드 = `.env`/`.env.deploy` 의 `WEB_AUTH_USERS=admin:<pw>`(최초 부팅만 적용, 이후 DB 가
  진실원천). dev 시드값은 `changeme1234`.
- 초기화: `auth.admin_reset_password('admin', '<seed>')` (must_change_password=1 재설정 ->
  첫 로그인 시 강제 변경, admin API 는 변경 전 403 가능). DB 즉시 반영, 재기동 불필요.
- 금지: `WEB_AUTH_USERS` 에서 빼고 prune 재기동으로 리셋 -> 과거 meetings.db 무백업 소실 사고.

---

## 참고
- 관련 배포 산출물: `deploy/README.md`(백엔드 repo, 커밋됨), `deploy/docker-compose.yml`,
  `deploy/Caddyfile`, `deploy/.env.deploy.example`.
- 프론트 repo 에 `docs/DEPLOY.md`(재배포 런북, 브랜치 `feature/171-google-integration-ui`
  에만 있고 main 미병합)와 `docs/DEV_DEPLOY_WORKFLOW.md`(untracked)가 있음 - main 복구 필요.
