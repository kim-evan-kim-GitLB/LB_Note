# 온프레미스 배포 (171 서버, 별도 컨테이너)

이 디렉토리는 meetscript-ai STT 웹앱을 **개발 환경과 분리된 독립 컨테이너**로 띄워
내부망 사용자에게 상시 제공하기 위한 배포 산출물입니다.

- 단일 컨테이너가 FastAPI(API) + 프론트 dist(SPA)를 함께 서빙합니다(별도 nginx 불필요).
- 같은 171 서버의 GPU(RTX PRO 6000, Blackwell)를 패스스루로 사용합니다(cu128).
- claude 요약/추출 자격증명은 **사용자별 설정**으로만 동작합니다(전역 키 주입 없음).

## 구성 파일

- `Dockerfile` - 멀티스테이지(프론트 빌드 -> CUDA 런타임). claude CLI/ffmpeg 포함.
- `docker-compose.yml` - 포트 매핑, GPU 예약, 볼륨(모델 ro + 데이터 영속), 헬스체크.
- `entrypoint.sh` - 마운트 점검 후 `python -m src.web`(uvicorn 0.0.0.0:8088) 실행.
- `.env.deploy.example` - 환경변수 템플릿(복사해서 `.env.deploy` 작성, 커밋 금지).
- `Dockerfile.dockerignore` - 빌드 컨텍스트 슬림화 규칙.

## 전제: 코드는 git, 모델은 호스트 디스크

소규모(약 50명) 사내 서비스의 실무 표준 구성입니다.

- 코드(백엔드/프론트)는 git 에서 가져옵니다(버전 관리·재현 가능).
- 모델 가중치(3.9G)는 git 에 넣지 않습니다. 호스트 디스크에 1회 배치하고 ro 볼륨으로
  마운트합니다(이미지에 굽지 않음 - 빌드/배포가 가벼워짐).
- 단일 GPU 호스트에서 `docker compose up -d` + 재시작 정책 + 데이터 볼륨이면 충분합니다.
  (다중 호스트로 늘면 그때 이미지 레지스트리를 추가)

## 0. 부트스트랩 (호스트가 비어 있을 때, 최초 1회)

개발 컨테이너 안에만 코드/모델이 있는 상태에서 호스트로 꺼내는 단계입니다.
**아래는 모두 171 호스트 쉘에서 실행**합니다(개발 컨테이너 내부에는 docker 가 없음).

```
# (1) 코드: git clone (deploy/ 포함 브랜치)
git clone git@github.com:kim-evan-kim-GitLB/LB_Note.git      /opt/meetscript/app
git clone git@github.com:kim-evan-kim-GitLB/LB_Note-web.git  /opt/meetscript/frontend
#   각 repo 에서 배포 대상 브랜치 checkout

# (2) 모델 3.9G: git 에 없으므로 개발 컨테이너에서 1회 복사
#     <DEV>=현재 개발 컨테이너 이름/ID (docker ps 로 확인)
docker cp <DEV>:/app/models /opt/meetscript/models
#   대안: HF_TOKEN 으로 재다운로드 가능하면 그쪽이 더 깔끔(게이트 모델이면 복사가 확실)
```

## 사전 준비 (호스트)

1. nvidia-container-toolkit 설치 (컨테이너 GPU 접근).
   - 확인: `docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi`
2. 위 0번으로 코드/모델 배치 완료.
3. 환경변수 작성(`deploy/.env.deploy`).

```
cd /opt/meetscript/app/deploy
cp .env.deploy.example .env.deploy
# 아래 경로/비밀값을 실제 값으로 채움:
#   BACKEND_DIR=/opt/meetscript/app
#   FRONTEND_DIR=/opt/meetscript/frontend
#   MODELS_DIR=/opt/meetscript/models
#   JWT_SECRET=$(openssl rand -hex 32)
#   CRED_ENC_KEY=<Fernet 키>   # python3 -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
#   SITE_HOST=<171 LAN IP>     # 필수! https 인증서 이름. 틀리면 접속이 TLS 에러로 실패
#   WEB_AUTH_USERS=admin:강한비번,...
```

## 빌드 + 기동

`--env-file .env.deploy` 를 꼭 붙입니다(경로 변수 BACKEND_DIR/MODELS_DIR 등이
빌드·볼륨 치환에 쓰이기 때문 - 이건 `env_file:` 이 아니라 compose 치환용으로 읽혀야 함).

```
cd /opt/meetscript/app/deploy
docker compose --env-file .env.deploy up -d --build
docker compose --env-file .env.deploy logs -f        # 기동 로그
```

- 접속: `https://<171-LAN-IP>:49152` (HTTPS, caddy 자체서명 인증서)
- 헬스: `https://<171-LAN-IP>:49152/api/health` (claude_auth, cohere_model_exists 등 표시)
- 호스트 49152 -> caddy :443 -> 내부 meetscript:8088. 평문 8088 은 호스트에 노출되지 않는다.
  0.0.0.0 바인딩이라 LAN 어디서든 접속(포트 49152 방화벽 오픈 필요).
- 마이크 녹음: HTTPS 라 LAN IP(192.168/10.x) 접속에서도 보안 컨텍스트가 충족돼 동작한다.
  (자체서명 인증서를 브라우저가 한 번 신뢰하면 됨 — 아래 'TLS' 절 참고)
- 첫 회의 처리 시 Cohere 모델(3.9G)을 로드하므로 수십 초 지연이 정상입니다.

## TLS (명시적 self-signed 인증서)

폐쇄망이라 공인 인증서(Let's Encrypt)는 불가하므로 self-signed 인증서를 직접 만들어 caddy 에 물린다.

> **[중요/필수] 배포 전 `bash gen-cert.sh` 로 인증서를 생성하세요.**
> 사용자가 raw IP(`https://<IP>:포트`)로 접속하면 TLS 에 **SNI 가 없어**(SNI 는 호스트명 전용),
> caddy 의 `tls internal` 은 인증서를 고르지 못해 **`tlsv1 alert internal error` 로 접속이 실패**
> 합니다(개발 컨테이너에서 재현·확인). 그래서 `SITE_HOST`(접속 IP/호스트명)를 **SAN 에 박은**
> 인증서를 만들어 caddy 가 직접 물도록 합니다. 흐름:
> ```
> cd ~/LB_Note-deploy/deploy
> # .env.deploy 의 SITE_HOST 가 실제 접속 IP 인지 확인 후:
> bash gen-cert.sh                     # → deploy/certs/{cert,key}.pem 생성(SAN=SITE_HOST,localhost,127.0.0.1)
> docker compose --env-file .env.deploy up -d   # caddy 가 certs/ 를 /etc/caddy/certs 로 ro 마운트
> ```
> - `SITE_HOST` 를 바꾸면 `gen-cert.sh` 를 다시 실행하고 caddy 를 재기동하세요.
> - 인증서가 없으면 caddy 가 기동 실패하므로(파일 부재) gen-cert 선실행이 사실상 강제됩니다.
> - `deploy/certs/` 는 개인키 포함이라 `.gitignore` 처리됨(커밋 금지).

브라우저 경고 처리(자체서명):

- (간단) 첫 접속 시 '신뢰할 수 없음' 경고를 그냥 수용 — 50명 사내망이면 충분.
- (권장) 만든 `certs/cert.pem` 을 각 PC 에 신뢰 인증서로 1회 등록하면 경고가 사라진다.
  - Windows=certmgr(신뢰된 루트), macOS=키체인, 리눅스=/usr/local/share/ca-certificates 후 update-ca-certificates.

## claude 요약/추출 인증 (사용자별)

이 배포는 **전역 claude 키를 넣지 않습니다.** 각 사용자가 로그인 후 사용자 설정 화면에서
API 키 또는 구독 토큰을 입력해야 요약/추출이 동작합니다(미설정이면 transcript 는 나오되
요약/액션아이템은 빈 값).

- API 키: Anthropic 콘솔의 `sk-ant-...`
- 구독 토큰: 로컬에서 `claude setup-token` 으로 발급한 장수명 토큰

## 프롬프트 빠른 반복 (재빌드/재시작 없이)

추출/요약/정제 품질은 대부분 **프롬프트 파일**로 조정합니다(코드 변경 아님).
`prompts/` 는 호스트 git repo 에서 ro 로 bind 마운트되며, 서버는 회의 처리할 때마다
프롬프트를 새로 읽습니다(`_load_prompt` 캐시 없음). 따라서:

```
# 171 서버에서 — 재빌드/재시작 불필요
cd /home/evan/LB_Note
vi prompts/extract.ko.md        # 또는 git pull 로 변경분 받기
#   다음에 처리되는 회의부터 즉시 새 프롬프트 적용
```

- **고도화 주 대상**: `prompts/extract.ko.md`(액션아이템 추출 규칙).
  요약은 `prompts/summarize.ko.md`, 정제는 `prompts/clean.ko.md`.
- 프롬프트 수정 시 파일 상단 `prompt_version` 을 올리세요(산출물에 박혀 추적 가능).
- 권장 흐름: 개발 컨테이너에서 실제 음원 + `eval/`(score_extraction)로 품질 확인 ->
  git push -> 서버 `git pull`. (실험은 직접 편집, 정식 반영은 git 으로 이력 남기기)
- 코드 로직(`src/postprocess/stages/extract.py` 등)까지 바꾼 경우는 프롬프트와 달리
  이미지 재빌드가 필요합니다: `docker compose --env-file .env.deploy up -d --build`
  (무거운 venv 레이어는 캐시라 코드만 바뀌면 수 초~수십 초).

## 운영 메모

- 데이터 영속: host-dir bind `DATA_DIR`(예: `/opt/meetscript/data`) -> `/app/output`
  (SQLite `web/meetings.db` = 계정/비번/사용자별 자격증명 + 산출물). 호스트에 평범한 파일로 떨어짐.
  - 이관: 기존 `meetings.db` 를 `DATA_DIR/web/` 에 복사해두면 그대로 승계(컨테이너 root 가 쓰므로
    파일/디렉토리 소유·권한만 맞춰둘 것).
  - 백업(핫백업 권장): `sqlite3 /opt/meetscript/data/web/meetings.db ".backup /opt/meetscript/backup/meetings-$(date +%F).db"`
    (또는 단순 `cp`). 매일 1회 크론이면 충분.
- 비밀번호: 사용자가 바꾼 비밀번호는 재기동해도 보존됩니다(seed 정책). 관리자가 강제
  초기화하려면 `WEB_AUTH_USERS` 에서 해당 id 를 빼고 재기동 후 다시 추가하세요.
- 타임존: tzdata 미설치 환경 대비 `TZ=KST-9`(POSIX) 사용. 한국은 DST 가 없어 정확합니다.
- 포트 변경: `HOST_PORT`(기본 49152) 로 호스트 노출 포트만 바꿉니다(caddy :443 으로 매핑,
  내부 meetscript 는 8088 고정).
- TLS: caddy 가 HTTPS 종단(위 'TLS' 절). 로그인 비번/JWT/Anthropic 키가 더 이상 평문으로
  LAN 을 지나지 않습니다.

## 보안 하드닝 (2026-06-16)

- **자격증명 at-rest 암호화**: `.env.deploy` 에 `CRED_ENC_KEY`(Fernet) 설정 시 DB 의 Claude
  자격증명이 암호화 저장되고, 부팅 시 기존 평문이 자동 재암호화됩니다(멱등·무중단). 미설정이면
  평문 저장(기존 동작). **키 분실 시 자격증명 복호 불가 → JWT_SECRET 과 함께 안전 보관.**
- **초기 비밀번호 강제 변경**: 관리자/스크립트가 부여한 초기 비번(예: 공용 `axlead1234`)은
  본인이 한 번 바꿔야 데이터/AI 기능이 열립니다. 미변경 사용자는 데이터/AI/설정 엔드포인트가
  403(`error_code=must_change_password`)으로 차단되고, 프론트가 강제 변경 화면을 띄웁니다.
  로그인·비번변경·`/api/auth/me`·`/api/health` 는 항상 열려 있어 '잠김'이 없습니다.
  - 관리자가 특정 사용자의 비번을 강제 초기화(다시 강제변경 상태로)하려면 `bulk_create_users.py`
    또는 `WEB_AUTH_USERS` 재시드로 해당 계정을 upsert 하면 `must_change_password=1` 로 돌아갑니다.

## 트러블슈팅

- `nvidia-smi` 실패: nvidia-container-toolkit 미설치 또는 드라이버/CUDA 베이스 태그 불일치.
  Dockerfile 의 `nvidia/cuda:12.8.0-...` 태그를 호스트 드라이버에 맞게 조정.
- 요약/추출 빈 값: 해당 사용자가 자격증명을 설정했는지, `/api/health` 의 claude_auth 확인.
- 모델 없음 경고: `MODELS_DIR` 볼륨 마운트 경로와 디렉토리 구조 확인.
