## T2b. 외부 하트비트(데드맨 스위치) — `deploy/heartbeat.sh` (웹앱 밖에서 도는 호스트 cron 감시)

**목표**: T2 의 알림은 전부 FastAPI lifespan 태스크 안에서 발화하므로 컨테이너 크래시·크래시루프·OOM·호스트 다운에서는 알림이 0건이다. 웹앱과 완전히 독립된 셸 스크립트를 `deploy/` 에 동봉하고 호스트 cron 5분 주기로 돌려, 웹 프로세스가 죽어 있어도 Slack 에 알림이 도달하는 두 번째 경로를 만든다. 웹앱 코드는 한 줄도 건드리지 않는다(하위호환 리스크 0).

**우선순위 / 예상 공수**: P0 / 6~9h (스크립트 4h, 테스트 2h, README·cron 절차 1~2h)

**선행 조건**
1. 없음(코드 의존 없음). T1·T2·T3 미머지 상태에서도 착수·완료 가능하다 — 헬스 응답의 `status` 필드(T1), 미러 디렉토리(T3), `OPS_ALERT_SLACK_CHANNEL`(T2)은 **있으면 쓰고 없으면 건너뛰는** 선택 항목으로만 참조한다.
2. **[사용자 수행]** 호스트 배포 repo 의 실제 경로 확인(문서상 `/opt/meetscript/app` 과 실제 `/home/evan/LB_Note-deploy` 가 다를 수 있다). 확인 명령:
   `sudo docker inspect meetscript-caddy-171 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'` -> `.../deploy/Caddyfile` 의 상위가 실제 `deploy/` 경로다. 이 값을 아래 모든 명령의 `<DEPLOY>` 에 대입한다.
3. **[사용자 수행]** 발송 경로 1개 확보. (A) Slack Incoming Webhook URL 1개(권장 — 봇 토큰·채널 초대 없이 동작, T2 게이트와 무관하게 진행 가능) 또는 (B) 기존 `SLACK_BOT_TOKEN` + 채널 ID(T2 의 `#lbnote-ops` 확보 후). 둘 다 없으면 스크립트는 무해하게 동작하되 알림을 못 보내므로 완료 판정이 불가하다.
4. 실행 순서상 T2b 는 08-05(T2 직후, T3 직전)에 착수한다. `deploy/.env.deploy.example` 은 T1·T2 가 이미 수정한 상태이므로 **브랜치 생성 전 `git fetch origin && git checkout -b ... origin/main` 으로 main 을 재동기**한다. 뒤이어 T3/T4 가 같은 파일을 수정하므로, 본 지시서는 **내가 추가하는 줄만** 명시한다(아래 "변경 대상" 참조).

**브랜치**: `feature/171-ops-heartbeat`

---

### 변경 대상

| 파일 | 변경 내용 |
| --- | --- |
| `deploy/heartbeat.sh` | **신설**(모드 0755). 외부 하트비트 본체. 의존성은 `bash` + `curl` + coreutils(`date`/`stat`/`df`/`find`/`sed`/`awk`)까지만. `jq` 는 **선택**(있으면 응답 파싱에 쓰고, 없으면 `grep` 폴백 — 없다고 실패하지 않는다). `docker` CLI 도 선택(없거나 권한 없으면 컨테이너 컨텍스트만 생략) |
| `deploy/.env.deploy.example` | 파일 **맨 끝**에 `# ----- 외부 하트비트(deploy/heartbeat.sh, 호스트 cron) -----` 블록 신설. 추가 줄: `HB_SLACK_WEBHOOK_URL=`, `HB_SLACK_CHANNEL=`, `# HB_BACKUP_MAX_AGE_SEC=129600`, `# HB_DISK_WARN_PERCENT=80`, `# HB_DISK_ERROR_PERCENT=90`, `# HB_HEALTH_FAIL_THRESHOLD=2`, `# HB_STATE_DIR=/var/lib/lbnote-heartbeat`, `# HB_RUNBOOK_URL=`, `# HB_ONCALL=`. **기존 줄은 수정·삭제하지 않는다**(T1/T2 가 앞쪽에 추가한 블록과 물리적으로 겹치지 않게 파일 끝에만 붙인다) |
| `deploy/README.md` | `## 운영 메모` 절 **뒤**에 `## 외부 하트비트(데드맨 스위치)` 절 신설 — 설치·cron 등록 명령, 이벤트 표, 실증 절차, 중단 방법. 기존 절은 건드리지 않는다 |
| `tests/test_heartbeat_script.py` | **신설**. `bash -n` 문법 검사 + `--dry-run` 종료코드/이벤트 문자열/비밀 비노출/정상 무알림 4케이스 |
| `docs/2026-07-08-ops-deploy-runbook.md` | **편집 금지**(장 번호 충돌 방지). 런북 14장(알림 이벤트 사전)에 넣을 이벤트 3줄은 PR 본문에 적어 T7 에 넘긴다 |

---

### 구현 단계

1. **`deploy/heartbeat.sh` 골격과 설정 로딩**
   - 셔뱅 `#!/usr/bin/env bash`, `set -uo pipefail`. **`set -e` 는 쓰지 않는다** — 점검 항목 하나가 비정상 종료해도 나머지 점검과 발송까지 반드시 도달해야 한다(부분 실패가 전체 침묵이 되면 데드맨 스위치의 의미가 사라진다).
   - 인자: `--env-file PATH`(기본 = 스크립트 디렉토리의 `.env.deploy`), `--dry-run`(발송 없이 메시지를 stdout 출력), `--alive`(alive 핑 강제 1회), `-h|--help`.
   - env 로딩: `set -a; . "$ENV_FILE"; set +a`. 파일이 없으면 `[heartbeat] env 파일 없음: <경로>` 를 stderr 에 남기고 **종료코드 2**.
   - **비밀 격리**: 로딩 직후 `HB_TOKEN="${SLACK_BOT_TOKEN:-}"` 로 옮기고 `unset SLACK_BOT_TOKEN SLACK_APP_TOKEN JWT_SECRET CRED_ENC_KEY WEB_AUTH_USERS` 한다. 이후 `docker`/`curl` 자식 프로세스 환경에 비밀이 실리지 않는다.
   - env 파일 퍼미션이 group/other 에 읽기 허용이면(`stat -c '%a'` 끝 두 자리가 `00` 이 아니면) 경고 1줄만 로그에 남기고 계속 진행한다(중단하지 않음).
   - 기본값(전부 `${VAR:-기본}` 형태, 미설정이 정상):
     `HB_HEALTH_URL`(기본 `https://127.0.0.1:${HOST_PORT:-49152}/api/health`), `HB_CURL_INSECURE=1`, `HB_HTTP_TIMEOUT_SEC=10`, `HB_HEALTH_RETRY=2`, `HB_HEALTH_FAIL_THRESHOLD=2`, `HB_ALERT_ON_DEGRADED=0`, `HB_BACKUP_DIRS=""`, `HB_BACKUP_MAX_AGE_SEC=129600`(36h), `HB_DISK_PATHS=""`, `HB_DISK_WARN_PERCENT=80`, `HB_DISK_ERROR_PERCENT=90`, `HB_DOCKER_RECLAIMABLE_GB=20`(0 이면 판정 OFF), `HB_CONTAINERS="meetscript-171 meetscript-slackbot-171 meetscript-caddy-171"`, `HB_ALIVE_HOUR=9`, `HB_COOLDOWN_SEC=3600`, `HB_STATE_DIR=/var/lib/lbnote-heartbeat`, `HB_LOG_MAX_BYTES=1048576`, `HB_SLACK_WEBHOOK_URL=""`, `HB_SLACK_CHANNEL=""`(빈값이면 `OPS_ALERT_SLACK_CHANNEL` 재사용), `HB_RUNBOOK_URL=""`, `HB_ONCALL=""`.
   - **[인터페이스 경계]** 디스크 임계 env 소유자는 T5(`WEB_DISK_WARN_PERCENT`/`WEB_DISK_ERROR_PERCENT`, 컨테이너 내부)다. 본 스크립트는 호스트 파티션을 보는 별개 관측점이므로 **`HB_` 접두 전용 변수만** 쓰고 `WEB_DISK_*` 를 읽거나 재정의하지 않는다. "두 값을 같은 수치로 유지" 라는 운영 규약은 런북 10장(T5/T7 소관)에 남긴다.

2. **상태 디렉토리와 동시 실행 방지**
   - `mkdir -p "$HB_STATE_DIR"` 후 `chmod 700`(실패는 무시). 실패로 디렉토리를 못 만들면 stderr 출력 후 종료코드 2.
   - 상태 파일: `health.fail`(연속 실패 회차), `health.alerted`(경보 발신 여부 0/1), `cooldown.<event>`(마지막 발송 epoch), `alive.date`(마지막 alive 발송 `YYYY-MM-DD`), `heartbeat.log`(실행 로그).
   - 스크립트 진입 즉시 `exec 9>"$HB_STATE_DIR/run.lock"; flock -n 9 || { echo "[heartbeat] 이전 실행 진행 중 - 스킵"; exit 0; }` 로 중복 실행을 막는다(느린 curl 이 5분 주기와 겹칠 때 이중 발송 방지).
   - 로그 자체 회전: `heartbeat.log` 크기가 `HB_LOG_MAX_BYTES` 초과면 `mv heartbeat.log heartbeat.log.1`(1세대만 유지). 무인 방치에서 감시 스크립트가 디스크를 먹는 자충수를 막는다.

3. **점검 1 — `/api/health` 응답 코드**
   - `code=$(curl -sS ${HB_CURL_INSECURE:+-k} -o "$body" -w '%{http_code}' --max-time "$HB_HTTP_TIMEOUT_SEC" "$HB_HEALTH_URL")` 를 최대 `HB_HEALTH_RETRY` 회 시도(사이 5초 sleep). `-k` 는 self-signed(caddy `deploy/certs`) 때문에 기본 ON 이며 `HB_CURL_INSECURE=0` 로 끌 수 있다.
   - 한 번이라도 `200` 이면 이번 회차 성공. 성공 시 `health.fail` 을 0 으로 리셋하고, 직전이 경보 상태(`health.alerted=1`)였으면 이벤트 `host_health_recovered` 를 **쿨다운 면제**로 발송한 뒤 `health.alerted=0`.
   - 전부 실패면 `health.fail` 을 +1 하고, `health.fail >= HB_HEALTH_FAIL_THRESHOLD` 이고 `health.alerted=0` 이면 이벤트 `host_health_down` 발송 후 `health.alerted=1`. (5분 주기 x 임계 2 = 약 10분 내 감지. 임계 이후에는 `health.alerted` 가 1 이라 복구 전까지 재발송하지 않는다 — 크래시루프에서 5분마다 알림이 쏟아지는 것을 막는다. 억제된 회차는 `heartbeat.log` 에 `alerted_skip event=host_health_down` 를 1줄 남기고, 발신 0건이므로 종료코드는 규약표대로 `0` 이다.)
   - 200 이고 응답 본문에 `"status"` 키가 있는 경우(T1 머지 이후에만 존재)에 한해, `"status":"degraded"` 이면 `HB_ALERT_ON_DEGRADED=1` 일 때만 `health_degraded_external` 을 쿨다운 6h 로 발송한다. **기본은 0** — degraded 알림은 T2 의 인프로세스 경로가 1차 소유자이고, 여기서 기본 ON 이면 같은 사건이 두 경로로 중복 발화한다.
   - 본문 파싱은 `jq -r '.status' 2>/dev/null` 을 먼저 시도하고 실패/부재 시 `grep -o '"status"[[:space:]]*:[[:space:]]*"[^"]*"' | sed ...` 폴백. **jq 부재가 실패 사유가 되어서는 안 된다.**

4. **점검 2 — 최신 백업 스냅샷 age**
   - 대상 디렉토리 결정: `HB_BACKUP_DIRS` 가 비어 있으면 `"${DATA_DIR:-}/web/backup"` 과 `"${BACKUP_MIRROR_DIR:-}"`(T3 머지 전에는 미설정 = 자동 제외) 중 **실재하는 디렉토리만** 대상으로 삼는다. 대상이 0개면 `backup_dirs_unknown` 을 로그에만 남기고 점검을 건너뛴다(알림 없음).
   - 각 디렉토리에서 `find "$d" -maxdepth 1 -type f -name 'meetings-*.db' -printf '%T@\n' | sort -nr | head -1` 로 최신 mtime 을 얻는다.
   - **파일이 0개면 그 자체로 이상**이다 — `run_db_backup()` 은 실패해도 정상 반환하므로 "예외"가 아니라 "부재"로 판정해야 한다(합의 근거). 이벤트 `backup_stale`, 사유 `no_snapshot`.
   - 최신 파일 age 가 `HB_BACKUP_MAX_AGE_SEC` 초과면 이벤트 `backup_stale`, 사유 `age=<시간>h`. 쿨다운 12h(`HB_COOLDOWN_SEC` 와 별도 상수 `43200` 고정 — 하루 2회 이상 같은 얘기를 반복하지 않는다).
   - **`predeploy-*.db` 는 대상에서 제외**한다. 수동 배포 직전 스냅샷(T3/규칙 6의 명명)은 자동 백업 파이프라인의 신선도 신호가 아니며, 이걸 포함시키면 배포 당일에만 backup_stale 이 가려진다.
   - 디렉토리별로 판정하고 메시지에는 디렉토리별 age 를 모두 싣는다(1차만 죽었는지 미러도 죽었는지가 대응을 가른다).

5. **점검 3 — 호스트 df**
   - 대상 경로: `HB_DISK_PATHS` 가 비면 `/`, `${DATA_DIR:-}`, `${BACKUP_MIRROR_DIR:-}`, `/var/lib/docker` 중 **실재하는 것만**. 같은 파티션이 중복되면 `df -P` 출력의 마운트포인트 기준으로 dedupe 한다.
   - `df -P "$p" | awk 'NR==2{gsub("%","",$5); print $5, $6}'` 로 사용률과 마운트포인트를 얻는다. `HB_DISK_ERROR_PERCENT` 이상이면 level=error, `HB_DISK_WARN_PERCENT` 이상이면 level=warn.
   - 이벤트 `disk_high`, 쿨다운 키는 `disk_high:<마운트포인트>:<level>` 로 만들어 warn -> error 승격이 쿨다운에 막히지 않게 한다(T5 가 인프로세스에서 쓰는 규칙과 같은 개념).
   - 컨테이너 내부 `disk_usage()` 는 DATA_DIR 파티션만 본다. 여기서 `/` 와 `/var/lib/docker` 를 함께 보는 것이 이 점검의 존재 이유다.

6. **점검 4 — `docker system df` 와 컨테이너 상태**
   - `command -v docker >/dev/null && docker info >/dev/null 2>&1` 가 아니면 전부 생략하고 로그에 `docker_unavailable` 만 남긴다(rootless·권한 없음에서 스크립트가 죽지 않게).
   - 회수 가능량: `docker system df --format '{{.Type}} {{.Reclaimable}}'` 를 읽어 `Images`/`Local Volumes`/`Build Cache` 행의 값을 GB 로 환산(`awk` 로 숫자 추출 후 단위 `kB|MB|GB|TB` 배수 적용). 합계가 `HB_DOCKER_RECLAIMABLE_GB` 초과면 이벤트 `docker_reclaimable_high`(쿨다운 24h). `HB_DOCKER_RECLAIMABLE_GB=0` 이면 판정 자체를 끈다.
   - 컨테이너 컨텍스트(판정 아님, 메시지 첨부용): `HB_CONTAINERS` 각각에 대해 `docker inspect -f '{{.Name}} {{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' <name>`. **이 값이 크래시루프(restarts 증가)와 "살아 있지만 unhealthy"(`restart: unless-stopped` 라 재시작되지 않는 상태)를 사람에게 보여주는 유일한 줄**이므로 반드시 메시지에 넣는다.

7. **점검 5 — 1일 1회 alive 핑**
   - 현재 시각의 `%H` 가 `HB_ALIVE_HOUR` 이상이고 `alive.date` 가 오늘이 아니면 이벤트 `alive` 를 **쿨다운 면제**로 발송하고 `alive.date` 를 오늘로 기록한다. `--alive` 인자를 주면 조건 무시하고 강제 1회(설치 직후 경로 검증용).
   - alive 메시지에는 health 코드, 디렉토리별 백업 age, 디스크 사용률, 컨테이너 상태 요약을 모두 싣는다. **알림 경로 자체의 고장(토큰 만료·봇 재설치·채널 archive)을 "매일 오는 것이 안 왔다"로 사람이 감지**하게 만드는 것이 목적이다.

8. **메시지 조립(ASCII 안전)**
   - 형식(고정, 화살표는 `->`):
     ```
     [LB_Note heartbeat] <event>
     host=<hostname> time=<ISO8601 로컬시각>
     health: <URL> -> <code> (연속 실패 <n>회)
     backup: <dir1> age=<h>h / <dir2> age=none
     disk: / 71% | /opt/meetscript/data 64%
     docker: reclaimable=12.3GB | meetscript-171 exited exit=137 restarts=4 health=none
     detail: <이벤트별 한 줄>
     runbook: <HB_RUNBOOK_URL>
     oncall: <HB_ONCALL>
     ```
   - `runbook`/`oncall` 은 값이 비면 줄 자체를 생략한다.
   - JSON 조립에 `jq` 를 쓰지 않는다. `_json_escape()` 를 정의해 백슬래시 -> `\\`, 큰따옴표 -> `\"`, 개행 -> `\n` 으로 치환한 뒤 `printf '{"text":"%s"}'`(webhook) 또는 `printf '{"channel":"%s","text":"%s"}'`(chat.postMessage) 로 만든다. 페이로드는 임시 파일에 쓰고 `--data-binary @<file>` 로 넘긴다(argv 길이 제한·따옴표 지옥 회피).

9. **Slack 발송(웹앱 비경유) — 토큰 주입과 실패 시 동작**
   - **웹앱 API 를 절대 호출하지 않는다.** 웹앱이 죽은 상태가 이 스크립트의 주 사용 시나리오다.
   - 경로 우선순위: (A) `HB_SLACK_WEBHOOK_URL` 이 있으면 Incoming Webhook, (B) 없고 `HB_TOKEN`(=`.env.deploy` 의 `SLACK_BOT_TOKEN`) + 채널(`HB_SLACK_CHANNEL` 또는 `OPS_ALERT_SLACK_CHANNEL`) 이 있으면 `chat.postMessage`, (C) 둘 다 없으면 발송하지 않고 로그에 `slack_not_configured` 만 남긴다.
   - **비밀을 argv 에 싣지 않는다**(`ps` 로 다른 사용자에게 보인다). URL 과 Authorization 헤더는 `curl -K -`(stdin config)로 넘긴다:
     ```
     printf 'url = "https://slack.com/api/chat.postMessage"\nheader = "Authorization: Bearer %s"\nheader = "Content-Type: application/json; charset=utf-8"\n' "$HB_TOKEN" \
       | curl -sS -K - --max-time "$HB_HTTP_TIMEOUT_SEC" -X POST --data-binary @"$payload" -o "$resp" -w '%{http_code}'
     ```
     (`-K -` 는 stdin, `--data-binary @file` 은 파일이라 충돌하지 않는다. webhook 경로도 같은 방식으로 `url = "..."` 만 config 로 넘겨 URL 을 argv 에서 뺀다.)
   - 성공 판정: HTTP 200 **그리고** (webhook: 본문이 `ok`) / (API: 본문에 `"ok":true` 포함). 실패면 3초 후 **1회만** 재시도한다(무한 재시도 금지 — cron 5분 주기 안에서 끝나야 한다).
   - 최종 실패 시: `heartbeat.log` 에 `slack_send_failed http=<code> error=<Slack error 코드>` 를 남기고 종료코드 20 으로 끝낸다. **토큰·webhook URL·채널 ID 원문은 로그·stdout 어디에도 쓰지 않는다**(T2 의 audit 비밀 금지 규약과 동일 수준).
   - 쿨다운: 이벤트별 `cooldown.<key>` 파일의 epoch 와 비교해 `HB_COOLDOWN_SEC`(이벤트별 상수 오버라이드 존재: backup_stale 12h, docker_reclaimable_high 24h) 이내면 발송을 건너뛰고 로그에 `cooldown_skip event=<key>` 를 남긴다. `alive` 와 `host_health_recovered` 는 면제. 억제로 이번 실행의 발신이 0건이 되면 종료코드는 `20` 이 아니라 `0` 이다(억제는 실패가 아니다 — 규약표 참조).
   - 여러 이벤트가 동시에 뜨면 **1회 실행당 Slack 메시지는 최대 3건**으로 제한하고 초과분은 마지막 메시지에 `외 N건` 으로 합친다(디스크 대상 4개가 동시에 넘칠 때의 폭주 방지).

10. **종료코드 규약(테스트·cron 판정의 기준) — 판정 축은 "이번 실행에서 메시지를 내보냈는가"다**

    이상 유무가 아니라 **발신 건수**를 축으로 삼는다. `alive` 는 이상이 아니지만 발신이므로 `10` 이다(이렇게 정의해야 "발송 경로가 살아 있음"을 종료코드 하나로 확인할 수 있고, alive 실증 기준과 어긋나지 않는다).

    | 코드 | 나오는 상황 | 이 코드를 기대하는 수용 기준 |
    | --- | --- | --- |
    | `0` | 이번 실행의 **발신 0건**. 세 경우가 모두 여기 해당: (a) 이상 이벤트 0건이고 alive 조건 미충족, (b) 이벤트는 떴으나 쿨다운·`health.alerted=1` 로 전부 억제, (c) `flock` 실패로 스킵. 구분은 종료코드가 아니라 `heartbeat.log` 의 `cooldown_skip`/`alerted_skip`/`lock_skip` 으로 한다 | `test_healthy_no_alert`, 중복 억제 실증의 2·3회차 |
    | `10` | 이번 실행에서 메시지 **1건 이상**을 실제 발송 성공(webhook 200+`ok` / API 200+`"ok":true`), 또는 `--dry-run` 으로 1건 이상 stdout 출력. **이상 이벤트든 `alive` 든 동일하게 10** | `--alive` 실증, `host_health_down`/`host_health_recovered`/`backup_stale` 실증, `test_dry_run_reports_down_and_stale` |
    | `20` | 발신 대상이 1건 이상인데 **실발송에 실패**(HTTP 비200·`ok:false`·타임아웃, 1회 재시도 후에도 실패) 또는 발송 경로 미구성(`slack_not_configured`). `--dry-run` 에서는 이 코드가 나오지 않는다(출력이 곧 성공이므로 미구성이어도 10) | 없음(정상 경로에서 기대하지 않는 코드. 관측 시 "막히면" 절로 이동) |
    | `2` | 설정 오류 — `--env-file` 경로 부재, `HB_STATE_DIR` 생성·쓰기 실패, 알 수 없는 인자 | 없음 |

    - 발신 건수가 여러 건이고 그중 일부만 실패하면 **20**(부분 실패를 성공으로 보고하지 않는다).
    - `--dry-run` 은 Slack 발송만 건너뛰고 상태 파일 갱신·로그 기록은 정상 수행한다(연속 실패 카운터 동작을 그대로 검증하기 위해).
    - cron 은 종료코드로 아무것도 하지 않는다(`MAILTO=""`). 종료코드는 **수동 실증과 pytest 의 판정 수단**이다.

11. **`deploy/.env.deploy.example` 블록 추가** — "변경 대상" 표의 줄만 파일 **끝**에 추가한다. 값이 있는 것은 `HB_SLACK_WEBHOOK_URL=`, `HB_SLACK_CHANNEL=` 둘뿐이고 나머지는 전부 주석 처리된 기본값 안내다(미설정 = 문서화된 기본 동작).

12. **`deploy/README.md` 절 추가** — `## 외부 하트비트(데드맨 스위치)`:
    - 왜 필요한가 2문장(인프로세스 알림은 프로세스가 죽으면 0건 / `restart: unless-stopped` 는 unhealthy 를 재시작하지 않는다).
    - 설치 + cron 등록 [사용자 수행] 복사 블록(아래 13단계와 동일 문자열).
    - 이벤트 표: `host_health_down`, `host_health_recovered`, `backup_stale`, `disk_high`, `docker_reclaimable_high`, `alive`, (옵션) `health_degraded_external` — 각 행에 "무엇을 뜻하나 / 첫 대응 1줄".
    - **종료코드 표(구현 단계 10 의 표를 그대로 복사)** — 수동 실행 시 사람이 결과를 해석하는 유일한 근거이므로 README 에도 반드시 싣는다. `0`=발신 0건(정상 또는 억제), `10`=발신 1건 이상 성공(alive 포함), `20`=발신 대상이 있는데 발송 실패/미구성, `2`=설정 오류.
    - 중단 방법: crontab 에서 해당 줄 삭제(코드 롤백 불필요).

13. **[사용자 수행] cron 등록 절차(복사 가능 형태)** — README 와 PR 본문에 그대로 싣는다. `<DEPLOY>` 는 선행 조건 2에서 확인한 실제 경로.
    ```
    sudo install -d -m 700 /var/lib/lbnote-heartbeat
    sudo chmod 755 <DEPLOY>/heartbeat.sh
    sudo bash <DEPLOY>/heartbeat.sh --env-file <DEPLOY>/.env.deploy --alive --dry-run   # 설치 전 리허설(발송 없음)
    sudo bash <DEPLOY>/heartbeat.sh --env-file <DEPLOY>/.env.deploy --alive             # 실제 1건 수신 확인
    sudo crontab -e
    ```
    crontab 에 아래 2줄을 추가한다(첫 줄은 cron 자체 메일 억제):
    ```
    MAILTO=""
    */5 * * * * /bin/bash <DEPLOY>/heartbeat.sh --env-file <DEPLOY>/.env.deploy >>/var/log/lbnote-heartbeat.cron.log 2>&1
    ```
    등록 확인: `sudo crontab -l | grep heartbeat.sh` 가 1줄 출력.
    (5분 주기 x 임계 2회 = 최대 약 10분 지연으로 감지. 더 빠르게 원하면 `*/2` + `HB_HEALTH_FAIL_THRESHOLD=2` 로 약 4분.)

14. **`tests/test_heartbeat_script.py` 신설** — 컨테이너 안에서 외부 네트워크 없이 도는 4케이스. 경로는 `pathlib.Path(__file__).resolve().parents[1] / "deploy" / "heartbeat.sh"`.
    - `test_bash_syntax_ok`: `subprocess.run(["bash", "-n", str(script)])` 의 `returncode == 0`.
    - **모든 케이스의 tmp env 에 `HB_ALIVE_HOUR=25` 를 반드시 넣는다.** `%H` 는 최대 `23` 이라 alive 조건이 절대 성립하지 않게 되고, 상태 디렉토리가 매번 새 `tmp_path` 라 `alive.date` 가 비어 있다는 사실 때문에 실행 시각에 따라 종료코드가 0/10 사이에서 흔들리는 것을 막는다(alive 발신도 10 이므로 이 고정이 없으면 `test_healthy_no_alert` 가 오전 9시 이후에만 깨진다).
    - `test_dry_run_reports_down_and_stale`: tmp env 파일(`HB_HEALTH_URL=http://127.0.0.1:9/api/health`(discard 포트 -> 즉시 연결 거부), `HB_HEALTH_RETRY=1`, `HB_HEALTH_FAIL_THRESHOLD=1`, `HB_BACKUP_DIRS=<빈 tmpdir>`, `HB_DISK_PATHS=<tmpdir>`, `HB_CONTAINERS=`, `HB_ALIVE_HOUR=25`, `HB_STATE_DIR=<tmpdir>/state`) + `--dry-run` -> `returncode == 10`(규약표: dry-run 출력 1건 이상), stdout 에 `host_health_down` 과 `backup_stale` 둘 다 포함.
    - `test_no_secret_in_output`: 위 env 에 `SLACK_BOT_TOKEN=xoxb-UNITTESTSECRET` 를 추가하고 `--dry-run` 실행 -> stdout+stderr+`heartbeat.log` 어디에도 `UNITTESTSECRET` 없음. 추가로 스크립트 원문에 `curl` 과 `Bearer` 가 **같은 줄에** 나타나지 않음을 정규식으로 단언(토큰이 argv 로 새는 구현을 회귀 차단).
    - `test_healthy_no_alert`: 표준 라이브러리 `http.server.ThreadingHTTPServer` 로 200 + `{"status":"ok"}` 를 주는 로컬 서버를 임의 포트에 띄우고, 백업 디렉토리에 방금 만든 `meetings-<ts>.db` 더미 파일 1개를 두고(`HB_ALIVE_HOUR=25`, `HB_DOCKER_RECLAIMABLE_GB=0`) 실행 -> `returncode == 0`(규약표: 발신 0건), stdout 에 `heartbeat]` 알림 블록이 없음(알림 0건).
    - 테스트는 `sudo` 없이도 도는 경로만 쓴다(모든 상태 디렉토리를 `tmp_path` 로 주입). `docker` 미설치 환경에서 스킵되지 않고 통과해야 한다.

---

### 수용 기준

- [ ] `bash -n /app/deploy/heartbeat.sh` 종료코드 0
- [ ] `test -x /app/deploy/heartbeat.sh` 종료코드 0 (실행 비트)
- [ ] `sudo .venv/bin/python -m pytest tests/test_heartbeat_script.py -q` -> `4 passed`, 0 failed
- [ ] 전체 스위트: **착수 직전 커밋**(브랜치 생성 직후 `origin/main` HEAD)에서 `sudo .venv/bin/python -m pytest tests -q` 를 **1회 측정해 그 통과 수를 baseline 으로 PR 본문에 기록**하고, 작업 완료 후 같은 명령이 **failed 0건 且 통과 수 >= baseline + 4**(이번 신규 테스트 수). 절대값을 기준으로 쓰지 않는다 — T2b 는 T1·T2 머지 뒤에 착수하므로 그 브랜치들이 추가한 테스트만큼 baseline 이 올라간다. (참고값: 2026-07-27 main 실측 `367 passed`. 착수 시점이 다르면 이 숫자는 성립하지 않으므로 판정 기준으로 쓰지 말 것.)
- [ ] `sudo .venv/bin/python -m ruff check tests/test_heartbeat_script.py` -> 종료코드 0 이고 `All checks passed!` 출력 (저장소 전체 `ruff check src tests` 의 기존 2건은 T6 소관이므로 여기서 판정하지 않는다)
- [ ] 토큰 비노출: `grep -nE 'curl.*Bearer|curl.*hooks\.slack\.com' /app/deploy/heartbeat.sh` 가 **0줄** 출력(종료코드 1) 이고, `grep -c -- '-K -' /app/deploy/heartbeat.sh` >= 1
- [ ] 웹앱 무영향: `git diff --name-only origin/main...HEAD | grep -c '^src/'` 결과가 **0**
- [ ] 런북 미편집: `git diff --name-only origin/main...HEAD | grep -c 'ops-deploy-runbook'` 결과가 **0**
- [ ] env 예시 반영: `grep -c '^HB_SLACK_WEBHOOK_URL=' /app/deploy/.env.deploy.example` == 1, `grep -c '^HB_SLACK_CHANNEL=' /app/deploy/.env.deploy.example` == 1
- [ ] README 반영: `grep -c '^## 외부 하트비트' /app/deploy/README.md` == 1 이고, 같은 절에 `*/5 * * * *` 크론 표현식이 1건 이상 존재(`grep -c '\*/5 \* \* \* \*' /app/deploy/README.md` >= 1)
- [ ] jq 비의존 실증: `command -v jq` 가 실패하는 이 컨테이너에서 위 pytest 4건이 전부 통과(= jq 없이 동작)
- [ ] 종료코드 규약 자기정합: 구현 단계 10 의 표에 있는 4개 코드(`0`/`10`/`20`/`2`)가 각각 어떤 수용 기준에 대응하는지 표의 3열이 비어 있지 않고, 아래 실증 항목이 기대하는 코드가 그 표와 일치한다(리뷰어 체크. 표와 다른 코드를 기대하는 항목이 1건이라도 있으면 불합격)
- [ ] **[사용자 수행]** alive 경로 실증: `sudo bash <DEPLOY>/heartbeat.sh --env-file <DEPLOY>/.env.deploy --alive; echo rc=$?` -> Slack 에 `[LB_Note heartbeat] alive` 1건 수신(스크린샷) 이고 `rc=10`(규약표: 발신 1건 성공. alive 는 이상이 아니지만 발신이므로 0 이 아니다)
- [ ] **[사용자 수행]** 데드맨 실증(합의 G2(a)): `sudo docker kill meetscript-171` 후
      `sudo HB_HEALTH_FAIL_THRESHOLD=1 bash <DEPLOY>/heartbeat.sh --env-file <DEPLOY>/.env.deploy; echo rc=$?` 실행 -> Slack 에 `host_health_down` **1건** 수신(스크린샷), `rc=10`.
      이어서 `sudo docker start meetscript-171` 후(헬스가 200 을 돌려줄 때까지 대기) 동일 명령 재실행 -> `host_health_recovered` 1건 수신, `rc=10`
- [ ] **[사용자 수행]** 중복 억제 실증: 컨테이너를 죽인 채로 위 명령을 연속 3회 실행 -> Slack 수신은 **총 1건**이고 회차별 종료코드가 `10`, `0`, `0`(2·3회차는 `health.alerted=1` 로 억제 = 발신 0건 = 규약표의 `0`). `sudo grep -c alerted_skip /var/lib/lbnote-heartbeat/heartbeat.log` >= 2 로 억제 기록을 확인한다
- [ ] **[사용자 수행]** backup_stale 실증: `sudo HB_BACKUP_MAX_AGE_SEC=1 bash <DEPLOY>/heartbeat.sh --env-file <DEPLOY>/.env.deploy; echo rc=$?` -> `backup_stale` 1건 수신, `rc=10`, 메시지에 대상 디렉토리별 age 가 표기됨
- [ ] **[사용자 수행]** cron 등록 확인: `sudo crontab -l | grep -c heartbeat.sh` == 1, 등록 20분 후 `sudo tail -5 /var/log/lbnote-heartbeat.cron.log` 에 실행 흔적 4건 내외 존재
- [ ] **[사용자 수행]** 상태 파일 생성 확인: `sudo ls /var/lib/lbnote-heartbeat` 에 `health.fail`, `heartbeat.log` 존재하고 디렉토리 모드가 `700`(`stat -c '%a' /var/lib/lbnote-heartbeat`)

---

### 하위호환 / 롤백

- **웹앱 코드 변경 0**: `src/**` 를 건드리지 않는다. 컨테이너 이미지·재빌드·재시작이 필요 없고, 기존 API·DB·스케줄러 동작에 영향이 없다. 롤백 리스크가 구조적으로 0 이다.
- **compose 무수정**: `deploy/docker-compose.yml` 을 건드리지 않으므로 T1/T3/T4 의 compose 수정과 충돌하지 않는다. 유일한 공유 파일은 `deploy/.env.deploy.example` 이며, **파일 끝에만 블록을 추가**해 T1/T2 가 앞쪽에 넣은 블록과 물리적 충돌을 피한다. 리베이스 시 확인할 충돌 지점은 이 파일 1개뿐이다.
- **미구성이 안전한 기본값**: `HB_SLACK_WEBHOOK_URL`·`HB_SLACK_CHANNEL`·`OPS_ALERT_SLACK_CHANNEL` 이 전부 비면 스크립트는 점검만 하고 로그만 남긴다(발송 0건, `slack_not_configured` 기록). 종료코드는 규약표대로 **이상 이벤트가 없으면 `0`, 이벤트가 있는데 보낼 곳이 없으면 `20`** 이다 — 즉 미구성 자체가 종료코드를 오염시키지 않는다. 실행 자체가 시스템에 어떤 부작용도 만들지 않는다(읽기 + 상태 디렉토리 쓰기뿐).
- **T1/T3 미머지 상태 호환**: 헬스 응답에 `status` 키가 없으면 degraded 판정을 조용히 생략하고, `BACKUP_MIRROR_DIR` 이 미설정이면 1차 백업 디렉토리만 본다. 나중에 T1/T3 가 머지되면 **재배포 없이** 다음 cron 실행부터 자동으로 항목이 늘어난다.
- **롤백 절차**: `sudo crontab -e` 로 해당 1줄 삭제(즉시 완전 정지). 파일까지 없애려면 `sudo rm -rf /var/lib/lbnote-heartbeat /var/log/lbnote-heartbeat.cron.log`. 코드 롤백이 필요하면 이 PR 만 revert 해도 다른 T 와 충돌하지 않는다.
- **커밋·푸시**: 사용자가 명시 요청할 때만. 브랜치명에 `171` 포함, main 직접 커밋 금지, PR 로 병합. 커밋 메시지에는 Phase 4 Jira 키를 포함하되 Atlassian MCP 가 끊겨 있어 조회가 불가하므로 **사용자에게 키를 직접 입력받는다**(추측 금지).

---

### 막히면

- **발송 경로가 둘 다 없다**(webhook 미발급 + T2 의 `#lbnote-ops` 채널·초대 미완): 코드는 그대로 완성하고 cron 등록까지 진행하되, 완료 판정(G2(a))은 열어 둔다. **Incoming Webhook 1개 발급이 T2 게이트보다 빠른 경로**이므로 사용자에게 webhook 우선 발급을 먼저 요청한다. 발급이 조직 정책상 불가하면 T2 채널 확보 일정에 종속됨을 명시하고 D2(08-06) 리스크로 상신한다.
- **cron 이 root 가 아닌 계정으로 등록돼 `.env.deploy` 를 못 읽는다**(퍼미션 600 + root 소유): `sudo crontab -e`(root crontab)로 등록한다. 그래도 안 되면 `HB_SLACK_WEBHOOK_URL` 만 담은 별도 파일(`/etc/lbnote-heartbeat.env`, 모드 600)을 만들어 `--env-file` 로 지정한다. **`.env.deploy` 의 퍼미션을 완화하는 우회는 금지**(JWT_SECRET·CRED_ENC_KEY 가 같은 파일에 있다).
- **`docker` 명령 권한 없음/rootless**: 컨테이너 상태·`docker system df` 항목만 생략하고 health/backup/df 로 운용한다(설계상 이미 폴백). 컨테이너 컨텍스트가 없으면 크래시루프 판별이 어려워지므로, 대신 `HB_CONTAINERS=` 로 비우고 런북 14장 대응 절차에 "수동으로 `docker ps -a` 확인" 을 넣도록 T7 에 전달한다.
- **`/api/health` 가 caddy 를 통해서만 열려 있어 로컬에서 200 이 안 나온다**: `HB_HEALTH_URL` 을 `https://127.0.0.1:${HOST_PORT}/api/health` 로 유지하고 `-k` 를 확인한다. 그래도 실패하면 `SITE_HOST` 를 쓴 URL(`https://<SITE_HOST>:<HOST_PORT>/api/health`)로 바꿔 1회 검증한다. 두 경로 모두 실패하는데 브라우저 접속은 되는 경우는 호스트 방화벽/loopback 정책 문제이므로 사용자에게 에스컬레이션한다.
- **호스트 자체가 다운되면 이 스크립트도 안 돈다**(설계상 한계): 이 경우 유일한 신호는 "매일 오던 alive 핑이 안 온다" 뿐이다. 외부 SaaS(healthchecks.io 등) 로 push 감시를 붙이는 것은 폐쇄망 반출 정책 사안이므로 **에이전트가 임의로 도입하지 않고** 사용자 결재로 올린다(합의 unresolved-1 저장소 공개 여부와 같은 성격).
- **종료코드 20 이 계속 나온다**(발신 대상은 있는데 못 보냄): `heartbeat.log` 마지막 줄이 `slack_not_configured` 면 발송 경로 미설정이고, `slack_send_failed http=<code> error=<code>` 면 실발송 실패다. 후자에서 `error=invalid_auth`/`account_inactive` 는 토큰 폐기, `channel_not_found`/`not_in_channel` 은 채널 ID 오타 또는 봇 미초대, `http=000` 은 네트워크·TLS(폐쇄망 프록시) 문제다. **로그에 토큰 원문이 없어야 정상**이므로 디버깅을 위해 토큰을 로그에 찍는 임시 수정은 금지하고, `--dry-run` 으로 메시지 본문만 확인한다.
- **알림이 5분마다 반복된다**: `health.alerted` 상태 파일이 갱신되지 않는 것이다(상태 디렉토리 쓰기 권한 또는 매 실행마다 다른 `HB_STATE_DIR`). `sudo ls -l /var/lib/lbnote-heartbeat` 로 파일 mtime 을 확인한다. 쿨다운 값을 키우는 방식으로 덮지 말 것 — 상태 영속이 깨진 것이 진짜 원인이다.
