# ThermoGuard 공장 현장 배포·전환 런북

이 문서는 실제 라인 전환 승인 후 정비 창에서만 사용한다. 저장소의 예시는
비밀값을 포함하지 않으며, 기존 운영 서비스·카메라·DB를 자동으로 변경하지
않는다.

## 운영 불변 조건

- GUI 운영 경로는 대시보드 런처가 호출하는 프로젝트 루트의 `python dashboard.py`
  하나다. 같은 PC에서 대시보드를 두 개 실행하지 않는다. lock 경로는 코드에
  `/run/thermoguard/dashboard.lock`으로 고정되어 있다.
- `monitor.py`, `pipeline.py`, `flir_collector.py`, `grab_flir_temperature.py`는
  현장 수집 경로가 아니다. 별도 collector 서비스는 반드시 비활성·마스크 상태를
  유지한다.
- 기존 `hotspot-flir-collector.service`가 남아 있다면 과거의 외부 경로를 가리킬
  수 있다. 이를 재활성화하면 현재 저장소의 차단 코드도 우회하고 카메라를 이중
  수집할 수 있으므로 대체 경로로 사용하지 않는다.
- `hotspot-backend.service`는 대시보드의 FastAPI/MariaDB 연동만 제공한다.
  systemd 서비스에 Telegram 토큰을 넣지 않는다.
- 대시보드는 전용 `thermoguard` GUI 세션에서만 실행한다. GUI를 자동 기동하는 별도
  systemd unit은 만들지 않으며, 하나의 사용자 세션에서 한 인스턴스만 허용한다.
- 릴리스 디렉터리는 불변이다. 실제 토큰·DB 비밀번호·`config.json`·대시보드 환경
  파일·열화상 데이터는 Git이나 `current` 아래에 넣지 않는다. 승인된 런처에 외부
  경로를 지정해, UI의 설정 저장도 릴리스 밖의 파일에만 반영되게 한다.
- `/run/thermoguard`와 `dashboard.lock`은 root가 부팅 시 만든다. 대시보드 계정은
  lock 파일을 읽고 flock만 잡을 수 있으며, 파일을 지우거나 바꾸거나 다른 lock
  경로로 대시보드를 실행하면 안 된다.
- FLIR GigE 카메라는 HTTP REST 이미지 엔드포인트와 ExifTool로 운영한다.
  메타데이터 온도 프로브는 `warning_interval_sec`(기본 5초), thermal+visual
  파일 촬영은 `normal_interval_sec`(기본 30초)의 독립 주기다. PySpin/Spinnaker는
  Dashboard 시작 필수 요건이 아니다.

## 권장 설치 배치

릴리스별 디렉터리와 `current` 심볼릭 링크를 사용하면 되돌리기가 명확하다.

```text
/opt/thermoguard/
├── releases/<release-id>/       # Git으로 검증한 불변 릴리스
├── current -> releases/<release-id>
├── venvs/<venv-id>/             # root 소유·검증 완료한 불변 Python 환경
└── venv -> venvs/<venv-id>      # 현재 release가 사용하는 Python 환경

/var/lib/thermoguard/
├── config.json                  # 현장별 일반 파일, dashboard가 저장 가능
└── dashboard.env                # Telegram 등 비밀값, dashboard만 읽기/쓰기

/var/log/thermoguard/
└── app.log                       # 불변 릴리스 밖의 dashboard 로그

/run/thermoguard/
└── dashboard.lock               # 재부팅 시 사라지는 호스트 공용 단일 인스턴스 lock

/etc/thermoguard/
└── hotspot-backend.env           # root 전용 DB 비밀값(PID 1이 backend에 전달)
```

서비스 템플릿의 `WorkingDirectory`와 `ExecStart`는 이 배치를 전제로 한다. 다른
경로를 사용한다면 backend unit, 대시보드 런처, 백업 절차를 함께 바꾸고 배포 전에
`systemd-analyze verify`를 통과시킨다. 릴리스 전환은 검증된 새 release·venv 디렉터리를
준비한 뒤 `current`와 `venv` 링크를 함께 바꾸는 방식으로 수행한다. 필요한 가상환경
변경도 전환 전에 별도로 구축·검증한다. 실행 중인 릴리스 안에서 `pip install`, 설정
파일 편집, 환경 파일 생성으로 상태를 섞지 않는다.

## 0. 신규 호스트: 실행 계정과 불변 릴리스 준비

### 0.1 Dashboard GUI 계정

Dashboard는 일반 사용자형 `thermoguard` 계정의 전용 GUI 세션에서만 실행한다. 이
계정은 backend의 system 계정 `thermoguard-backend`와 다르며, `sudo`, `docker`,
`root` 또는 backend 그룹에 넣지 않는다. 기존 사람의 Desktop 세션이나 공유 계정을
대신 사용하지 않는다.

신규 호스트에서 root 권한으로 계정을 한 번 만든다. 아래 `passwd`는 대화형으로
입력되므로 임시·공유 비밀번호를 명령행이나 문서에 남기지 말고, 현장 계정 정책에
따라 설정한다.

```bash
getent group thermoguard >/dev/null || sudo groupadd thermoguard
getent passwd thermoguard >/dev/null || \
  sudo useradd --create-home --home-dir /home/thermoguard \
    --shell /bin/bash --gid thermoguard thermoguard
sudo passwd thermoguard
id thermoguard
sudo -l -U thermoguard
```

마지막 명령에서 `thermoguard`에 허용된 sudo 명령이 없어야 한다. 로그인 관리자에서
이 계정의 로컬 GUI 로그인이 허용되는지도 확인한다. 현장 GUI에 필요한 최소 권한은
별도 장비 검증으로만 추가하고, 권한을 넓혀 카메라·DB 접근 문제를 우회하지 않는다.

### 0.2 Root 소유 불변 릴리스

릴리스는 승인된 Git 커밋으로부터 root가 배치한다. 작업 중인 workspace를 복사하거나
`current` 아래에서 파일을 수정하지 않는다. 아래의 staging 경로와 release ID/commit은
승인된 실제 값으로 치환한다. `git archive`는 추적된 승인 커밋만 배치하므로 로컬
미추적 파일·비밀 파일을 릴리스에 섞지 않는다.

```bash
set -euo pipefail
release_id='approved-release-id'
release_commit='approved-git-commit'
release_dir="/opt/thermoguard/releases/${release_id}"

test ! -e "$release_dir"
test ! -L "$release_dir"
test "$(git -C /secure/staging/ThermoGuard rev-parse HEAD)" = "$release_commit"
git -C /secure/staging/ThermoGuard diff --quiet
git -C /secure/staging/ThermoGuard diff --cached --quiet

sudo install -d -o root -g root -m 0755 /opt/thermoguard
sudo install -d -o root -g root -m 0755 /opt/thermoguard/releases
sudo install -d -o root -g root -m 0755 "$release_dir"
git -C /secure/staging/ThermoGuard archive --format=tar "$release_commit" | \
  sudo tar -x -C "$release_dir"
sudo chown -R root:root "$release_dir"
sudo chmod -R a-w "$release_dir"
```

`set -euo pipefail`을 지원하는 Bash에서 위 블록을 실행한다. archive 또는 extract가
실패하면 release ID를 `current`에 연결하지 않는다. `a-w`는 runtime 계정의 쓰기를
차단하고, root도 배포 절차 없이 파일을 고치지 않도록 의도를 드러낸다. release와
venv의 연결·서비스 재기동 순서는 아래 전환 절에서만 수행한다. 설정·환경·로그·dataset은
반드시 다음 절의 외부 영구 경로에만 둔다.

### 0.3 후보 venv 검증과 불변 승격

후보 venv는 [`requirements/README.ko.md`](../requirements/README.ko.md)의 절차대로
`/opt/thermoguard/candidates/<release-id>/venv`에 먼저 만든다. 후보는 release를 만들 때
검증한 동일한 staging source와 승인된 wheelhouse/네트워크 정책만 사용하며,
`factory-test.in`, `verify_factory_baseline.py`, 승인된 회귀·오프라인 점검을 모두
통과시킨다. 실행 중인 `/opt/thermoguard/venv` 또는 service venv에 `pip install`하지
않는다.

아래는 fresh host에서 후보를 실제로 만드는 예시다. `staging_root`는 0.2의 Git commit
검증을 통과한 source checkout이고, `<release-id>`는 같은 승인 release ID다. wheelhouse를
의무화한 현장에서는 조직의 승인된 `pip` index/wheelhouse 인자를 이 명령에 추가한다.

```bash
set -eu
release_id='approved-release-id'
release_commit='approved-git-commit'
staging_root='/secure/staging/ThermoGuard'
candidate_root="/opt/thermoguard/candidates/${release_id}"
candidate_venv="${candidate_root}/venv"

test "$(git -C "$staging_root" rev-parse HEAD)" = "$release_commit"
git -C "$staging_root" diff --quiet
git -C "$staging_root" diff --cached --quiet
sudo install -d -o root -g root -m 0755 /opt/thermoguard
sudo install -d -o root -g root -m 0755 /opt/thermoguard/candidates
sudo test ! -e "$candidate_root"
sudo test ! -L "$candidate_root"
sudo install -d -o root -g root -m 0755 "$candidate_root"
sudo test ! -e "$candidate_venv"
sudo test ! -L "$candidate_venv"
sudo python3.10 -m venv "$candidate_venv"
sudo "$candidate_venv/bin/python" -m pip install \
  -r "$staging_root/requirements/factory-test.in"
sudo "$candidate_venv/bin/python" \
  "$staging_root/requirements/verify_factory_baseline.py"
```

다음은 승인된 후보를 새 root 소유 버전 경로로 승격하는 명령이다. `venv_id`는 release와
Python/플랫폼 기준선을 추적할 수 있는 승인 식별자로 정하고, 이미 존재하는 버전 경로는
재사용·덮어쓰지 않는다. 후보가 동일 파일시스템의 새 호스트라면 `mv`는 원자적 이동이다.
다른 파일시스템이면 후보를 해당 파일시스템에서 미리 생성하거나, 새 빈 경로로 복사 후
검증하여 완전한 tree가 확인된 뒤에만 아래 링크 전환 단계로 진행한다.

```bash
set -eu
release_id='approved-release-id'
venv_id='approved-venv-id'
candidate_venv="/opt/thermoguard/candidates/${release_id}/venv"
venv_dir="/opt/thermoguard/venvs/${venv_id}"
staging_root='/secure/staging/ThermoGuard'

sudo test -x "$candidate_venv/bin/python"
sudo "$candidate_venv/bin/python" \
  "$staging_root/requirements/verify_factory_baseline.py"
sudo install -d -o root -g root -m 0755 /opt/thermoguard/venvs
sudo test ! -e "$venv_dir"
sudo test ! -L "$venv_dir"
sudo mv "$candidate_venv" "$venv_dir"
sudo chown -R root:root "$venv_dir"
sudo chmod -R a-w "$venv_dir"
sudo test -x "$venv_dir/bin/python"
```

승격 직전 검증 스크립트는 후보 Python과 0.2에서 Git commit을 확인한 staging source로
실행하며 패키지를 설치·변경하지 않는다. 기록에는 `venv_id`, `pip freeze --all`,
Python/OS 버전, 후보 검증·회귀 결과를 남긴다. 새 venv가 불변 상태로 준비되어도 아직
`venv` 링크·서비스·Dashboard는 바꾸지 않는다.

## 1. 전환 전 백업과 기준선 기록

1. 승인된 Git 커밋 ID와 배포 담당자·시간을 변경 기록에 남긴다. 현재 설정의
   내용 대신 SHA-256 해시를 기록하면 비밀값을 노출하지 않고 동일성을 확인할 수
   있다.
2. 권한이 제한된 백업 위치에 다음을 보관한다.

   - 이전 릴리스 디렉터리 또는 Git 커밋 ID
   - `/etc/systemd/system/hotspot-backend.service`
   - `/etc/thermoguard/hotspot-backend.env`
   - `/var/lib/thermoguard/config.json`, `/var/lib/thermoguard/dashboard.env`
   - 현장 DBA 절차로 생성·검증한 MariaDB 백업

3. 백업 복원 권한, DB 복원 담당자, 롤백 판단자를 전환 시작 전에 지정한다.
4. 이전 상태를 읽기 전용으로 확인한다.

   ```bash
   git rev-parse HEAD
   sudo systemctl status hotspot-backend.service --no-pager
   sudo systemctl is-enabled hotspot-flir-collector.service
   ```

`hotspot-flir-collector.service`가 없으면 변경 기록에 `not-found`로 남긴다. 활성
상태이면 원인을 확인한 뒤 승인된 정비 창에서만 중지한다.

## 2. 영구 설정·환경 파일과 공용 lock 준비

현장별 파일은 불변 릴리스 밖에 둔다. Dashboard 프로세스는 비밀 파일의 소유자인
전용 `thermoguard` 런타임 계정으로 실행한다. `thermoguard` 그룹은 root가 만든
lock 파일의 read 권한에만 사용하며, 다른 계정에 Telegram 환경 파일 읽기·설정 파일
쓰기·lock 파일 변경 권한을 부여하는 수단이 아니다.

```bash
sudo install -d -o thermoguard -g thermoguard -m 0750 /var/lib/thermoguard
sudo install -d -o thermoguard -g thermoguard -m 0750 /var/lib/thermoguard/dataset
sudo install -d -o thermoguard -g thermoguard -m 0750 /var/lib/thermoguard/calibration
sudo install -d -o thermoguard -g thermoguard -m 0750 /var/log/thermoguard
sudo install -o thermoguard -g thermoguard -m 0640 \
  config.example.json /var/lib/thermoguard/config.json
sudo install -o thermoguard -g thermoguard -m 0600 \
  deployment/env/thermoguard-dashboard.env.example \
  /var/lib/thermoguard/dashboard.env
sudo -u thermoguard editor /var/lib/thermoguard/config.json
sudo -u thermoguard editor /var/lib/thermoguard/dashboard.env
```

`config.example.json` 전체를 출발점으로 사용한다. 일부 항목만 가진 JSON을 새로
만들면 엄격한 설정 검증에서 거부될 수 있다. `config.json`은 심볼릭 링크가 아닌
일반 파일로 두고, 승인된 정비 절차 또는 Dashboard 설정 화면을 통해서만 변경한다.
`dashboard.env`는 `TELEGRAM_ENABLED=false`로 시작한다. Telegram 토큰·수신자·활성화
설정은 이 파일에만 두며, backend DB 환경 파일이나 systemd unit에 넣지 않는다.
예시의 dataset, overlay, homography 경로는 모두 릴리스 밖의 절대 경로다. 현장
볼륨의 승인된 전용 하위 폴더로 바꾸는 것은 가능하지만, 공장 모드에서는 상대 경로나
release 내부 경로가 거부된다.

`/run`은 재부팅 때 비워지므로 source-controlled tmpfiles 규칙으로 root 소유 lock
디렉터리와 lock 파일을 매 부팅마다 만든다. 대시보드 계정에는 group-read 권한만
있으면 exclusive flock을 잡을 수 있다. directory/file 쓰기 권한을 주면 잠금 자체를
바꿔 중복 카메라 소유를 우회할 수 있으므로 금지한다.

```bash
sudo install -o root -g root -m 0644 \
  deployment/tmpfiles.d/thermoguard.conf /etc/tmpfiles.d/thermoguard.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/thermoguard.conf
stat -c '%U:%G %a %n' /run/thermoguard /run/thermoguard/dashboard.lock
```

lock 경로는 코드에서 `/run/thermoguard/dashboard.lock`으로 고정되어 있으며 환경변수로
재지정할 수 없다. 위 `stat`의 기대값은 각각 `root:thermoguard 750`,
`root:thermoguard 640`이다. source-controlled 런처를 root 소유·0755로 설치한다. 이 런처는
외부 설정·Telegram 환경·로그 경로를 모두 고정해, 릴리스 전환으로 토큰·설정·로그가
사라지거나 release 안에 쓰이지 않게 한다.

```bash
sudo install -o root -g root -m 0755 \
  deployment/bin/thermoguard-dashboard \
  /usr/local/bin/thermoguard-dashboard
```

`THERMOGUARD_DASHBOARD_ENV`의 파일은 Dashboard UI가 Telegram 설정을 저장할 때도
계속 사용하므로, 릴리스 전환으로 토큰이나 활성화 상태가 사라지지 않는다.

런처는 전용 `thermoguard` GUI 세션에서 실행한다. 다른 사용자 세션에서 우회해
직접 `python dashboard.py`를 실행하지 않는다.

백엔드는 GUI Dashboard 계정과 다른 `thermoguard-backend` 서비스 계정으로 실행한다.
DB 환경 파일은 root만 파일 시스템에서 읽을 수 있게 두고, systemd PID 1이 서비스
시작 직전에 해당 환경값을 backend 프로세스에 전달한다. 따라서 Dashboard GUI 계정에
DB 비밀번호 파일 또는 backend 프로세스 환경을 읽을 권한을 주지 않는다.

```bash
getent group thermoguard-backend >/dev/null || \
  sudo groupadd --system thermoguard-backend
getent passwd thermoguard-backend >/dev/null || \
  sudo useradd --system --gid thermoguard-backend --no-create-home \
    --shell /usr/sbin/nologin thermoguard-backend
sudo install -d -o root -g root -m 0700 /etc/thermoguard
sudo install -o root -g root -m 0600 \
  deployment/env/hotspot-backend.env.example \
  /etc/thermoguard/hotspot-backend.env
sudoedit /etc/thermoguard/hotspot-backend.env
```

이 파일에는 `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`만 둔다.
편집기 화면, `systemctl show`, 셸 이력, 로그에 비밀번호를 남기지 않는다.

## 3. 승인 전환: release·venv 링크와 backend 재기동

`/opt/thermoguard/venv`는 실행 중인 workspace 환경을 복사한 것이 아니라,
0.3의 후보 환경 절차와 `verify_factory_baseline.py`를 통과해
`/opt/thermoguard/venvs/<venv-id>`에 승격된 root 소유 환경이어야 한다. 이 작업과
wheelhouse 검증은 정비 창 전에 끝내며, 실행 중인 service venv에서 설치·업그레이드하지
않는다.

기존 backend는 이미 실행 중이라면 `enable --now`만으로 새 `current` release나 `venv`
링크를 다시 읽지 않는다. 따라서 아래 순서를 승인된 정비 창에서 **하나의 Bash 관리
세션**으로 수행한다. Dashboard를 먼저 GUI의 정상 종료로 닫고, backend를 멈춘 뒤에만
두 링크를 바꾼다. Dashboard 프로세스를 `kill -9`로 종료하거나, backend가 살아 있는
상태에서 링크를 바꾸거나, 검증 전 Dashboard를 시작하면 안 된다.

```bash
set -eu
release_id='approved-release-id'
venv_id='approved-venv-id'
release_dir="/opt/thermoguard/releases/${release_id}"
venv_dir="/opt/thermoguard/venvs/${venv_id}"

transition_failed() {
  status=$?
  echo 'transition failed; backend was stopped and Dashboard must not start' >&2
  sudo systemctl stop hotspot-backend.service || true
  exit "$status"
}
trap transition_failed ERR

# Dashboard는 이 블록 전에 GUI에서 정상 종료한다. backend가 있다면 먼저 정지한다.
backend_load_state="$(sudo systemctl show -p LoadState --value hotspot-backend.service)"
case "$backend_load_state" in
  not-found) ;;
  loaded) sudo systemctl stop hotspot-backend.service ;;
  *)
    echo "unexpected backend unit state: $backend_load_state" >&2
    exit 1
    ;;
esac
if sudo systemctl is-active --quiet hotspot-backend.service; then
  echo 'backend is still active; do not switch release or start Dashboard' >&2
  exit 1
fi

# 롤백 가능한 기존 target을 변경 기록에도 남긴다. 새 설치라면 빈 값일 수 있다.
previous_release="$(readlink -f /opt/thermoguard/current 2>/dev/null || true)"
previous_venv="$(readlink -f /opt/thermoguard/venv 2>/dev/null || true)"
if [ ! -d "$previous_release" ]; then previous_release=''; fi
if [ ! -d "$previous_venv" ]; then previous_venv=''; fi
printf 'previous_release=%s\nprevious_venv=%s\n' "$previous_release" "$previous_venv"

sudo test -d "$release_dir"
sudo test -x "$venv_dir/bin/python"
sudo test ! -e /opt/thermoguard/.current.next
sudo test ! -L /opt/thermoguard/.current.next
sudo test ! -e /opt/thermoguard/.venv.next
sudo test ! -L /opt/thermoguard/.venv.next

# mv -T performs an atomic replacement of each symlink on the same filesystem.
sudo ln -s "releases/${release_id}" /opt/thermoguard/.current.next
sudo ln -s "venvs/${venv_id}" /opt/thermoguard/.venv.next
sudo mv -Tf /opt/thermoguard/.current.next /opt/thermoguard/current
sudo mv -Tf /opt/thermoguard/.venv.next /opt/thermoguard/venv
sudo chown -h root:root /opt/thermoguard/current /opt/thermoguard/venv
readlink -f /opt/thermoguard/current
readlink -f /opt/thermoguard/venv

sudo install -o root -g root -m 0755 \
  /opt/thermoguard/current/deployment/bin/thermoguard-dashboard \
  /usr/local/bin/thermoguard-dashboard
sudo install -o root -g root -m 0644 \
  /opt/thermoguard/current/deployment/systemd/hotspot-backend.service \
  /etc/systemd/system/hotspot-backend.service
sudo systemd-analyze verify /etc/systemd/system/hotspot-backend.service
sudo systemctl daemon-reload
sudo systemctl enable hotspot-backend.service
sudo systemctl reset-failed hotspot-backend.service
sudo systemctl restart hotspot-backend.service
sudo systemctl status hotspot-backend.service --no-pager
curl --fail --silent --show-error http://127.0.0.1:8000/api/health
curl --fail --silent --show-error http://127.0.0.1:8000/api/ready
cd /opt/thermoguard/current/Project_hotspot/backend
sudo /usr/bin/env -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  PYTHONDONTWRITEBYTECODE=1 \
  THERMOGUARD_BACKEND_ENV=/etc/thermoguard/hotspot-backend.env \
  /opt/thermoguard/venv/bin/python schema_preflight.py --json --verify-fingerprint
sudo journalctl -u hotspot-backend.service -n 200 --no-pager
sudo systemctl show hotspot-backend.service \
  -p ActiveState -p SubState -p NRestarts --no-pager
trap - ERR
```

이 블록은 Bash에서 `set -e`와 `ERR` trap을 사용한다. `restart`, health/readiness,
schema fingerprint 중 하나라도 실패하면 `transition_failed`가 backend를 다시 중지하고
오류 코드로 종료한다. 마지막 `trap - ERR`에는 앞의 모든 검증이 성공한 경우에만
도달한다. 명령 일부를 복사해 따로 실행하면 이 fail-closed 보장이 사라지므로 블록을
한 관리 세션에서 그대로 실행한다.

`/api/health`는 FastAPI 프로세스 liveness이고, `/api/ready`는 DB에 읽기 전용
연결 검사를 수행하는 readiness다. 위 명령이 하나라도 실패하면 **Dashboard를 시작하지
않고** backend도 중지한 채 아래 rollback 절차 또는 사고 분석으로 넘어간다. `restart`는
명시적이므로 기존 active backend가 이전 release/venv 메모리를 계속 쓰는 상태를 허용하지
않는다. 운영 환경에서는 `uvicorn --reload` 또는 다중 worker를 사용하지 않는다.
각 symlink 교체는 원자적이지만 두 링크를 동시에 바꿀 수는 없다. 그 사이 backend와
Dashboard가 모두 중지되어 있으므로 어느 프로세스도 release/venv 혼합 조합을 관측하지
않는다.

새 링크로 기동한 backend의 `--verify-fingerprint`는 17개 필수 테이블뿐 아니라,
source-controlled `schema_manifest.json`의 정규화된 `SHOW CREATE TABLE` SHA-256
기준선과 비교한다. `not_ready`, `drifted`, 또는 exit code 1/2이면 운전 전환을 중단한다. 이 명령은
마이그레이션을 수행하지 않는다. 의도된 스키마 변경도 DBA 승인, 백업, 별도 검증된
릴리스와 기준선 갱신 없이는 현장에서 적용하지 않는다. 위 명령은 경로만 환경변수로
넘기며 DB 값은 인자·표준 출력·셸 이력에 넣지 않는다. root가 보호 파일을 읽어 수행하므로
`sudo -u thermoguard`로 바꾸지 않는다.

### 3.1 전환 실패 시 rollback

새 backend의 restart, `/api/health`, `/api/ready` 또는 schema preflight가 실패하면
Dashboard를 시작하지 않는다. 이전 release와 venv target이 위 기록에 모두 있고 디렉터리가
존재할 때만, 정비 승인 하에 다음을 수행한다. 기존 target이 없는 첫 설치 실패에서는
링크를 임의로 지우거나 legacy collector를 되살리지 말고 backend를 중지한 채 원인을
분석한다.

```bash
set -eu
previous_release='/opt/thermoguard/releases/<previous-release-id>'
previous_venv='/opt/thermoguard/venvs/<previous-venv-id>'

rollback_failed() {
  status=$?
  echo 'rollback validation failed; backend was stopped and Dashboard must not start' >&2
  sudo systemctl stop hotspot-backend.service || true
  exit "$status"
}
trap rollback_failed ERR

sudo systemctl stop hotspot-backend.service
sudo test -d "$previous_release"
sudo test -x "$previous_venv/bin/python"
sudo test ! -e /opt/thermoguard/.current.rollback
sudo test ! -L /opt/thermoguard/.current.rollback
sudo test ! -e /opt/thermoguard/.venv.rollback
sudo test ! -L /opt/thermoguard/.venv.rollback
sudo ln -s "$previous_release" /opt/thermoguard/.current.rollback
sudo ln -s "$previous_venv" /opt/thermoguard/.venv.rollback
sudo mv -Tf /opt/thermoguard/.current.rollback /opt/thermoguard/current
sudo mv -Tf /opt/thermoguard/.venv.rollback /opt/thermoguard/venv
sudo chown -h root:root /opt/thermoguard/current /opt/thermoguard/venv
sudo install -o root -g root -m 0755 \
  "$previous_release/deployment/bin/thermoguard-dashboard" \
  /usr/local/bin/thermoguard-dashboard
sudo install -o root -g root -m 0644 \
  "$previous_release/deployment/systemd/hotspot-backend.service" \
  /etc/systemd/system/hotspot-backend.service
sudo systemctl daemon-reload
sudo systemctl reset-failed hotspot-backend.service
sudo systemctl restart hotspot-backend.service
curl --fail --silent --show-error http://127.0.0.1:8000/api/health
curl --fail --silent --show-error http://127.0.0.1:8000/api/ready
cd /opt/thermoguard/current/Project_hotspot/backend
sudo /usr/bin/env -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  PYTHONDONTWRITEBYTECODE=1 \
  THERMOGUARD_BACKEND_ENV=/etc/thermoguard/hotspot-backend.env \
  /opt/thermoguard/venv/bin/python schema_preflight.py --json --verify-fingerprint
trap - ERR
```

rollback 블록도 같은 방식으로 동작한다. `restart` 이후의 health/readiness/schema
검증이 실패하면 아직 활성인 rollback backend까지 `rollback_failed`가 중지한다.
따라서 `trap - ERR` 해제 전의 실패를 무시하거나 `|| true`로 감싸지 않는다.

rollback backend도 health·readiness·schema preflight를 다시 통과하기 전에는 Dashboard를
재개하지 않는다. rollback 실패는 운영 복귀 승인이 아니라 사고 상태이며, backend를
중지한 채 담당자·DBA·라인 책임자에게 이관한다.

## 4. 레거시 collector 차단

레거시 collector가 설치되어 있다면, 대시보드를 처음 시작하기 전에 아래 상태를
만든다. 이 명령은 실제 서비스를 바꾸므로 승인된 전환 창에서만 실행한다.

```bash
sudo systemctl disable --now hotspot-flir-collector.service
sudo systemctl mask hotspot-flir-collector.service
sudo systemctl is-enabled hotspot-flir-collector.service
```

기대값은 `masked`다. `monitor.py`, `pipeline.py` 또는 collector 스크립트를
대체 서비스로 등록하지 않는다. 롤백 시에도 이 collector를 되살리지 않는다.

## 5. 대시보드 승인 시험

실제 카메라에 연결되는 단계이므로 라인 담당자와 함께 수행한다.

1. 전용 `thermoguard` GUI 세션에서 기존 대시보드나 레거시 수집기가 없는지
   확인하고, `/run/thermoguard`와 lock 파일이 `root:thermoguard 750/640`으로
   provision되었는지 확인한다.
2. `/var/lib/thermoguard/config.json`의 카메라 IP, ROI, 임계값, 보존 기간,
   백엔드 URL을 두 사람이
   교차 검토한다.
   - `config.example.json`의 초기값은 `backend.enabled=false`다. 이 상태에서는
     Dashboard가 카메라 수집을 시작하지 않는 것이 정상이다. 전환 담당자는 이 값을
     우회해 켜거나 config 파일을 임의 편집하지 않는다.
   - Settings에서 승인된 공장·생산라인·로봇·카메라 Backend asset을 등록하고,
     저장 성공으로 받은 카메라 식별자와 backend 연결을 두 사람이 확인한 뒤에만
     `backend.enabled` 상태로 수집 승인을 진행한다. 등록 또는 저장에 실패하면
     Dashboard를 시작하지 않고 Backend 문제를 먼저 해결한다.
   - Settings 저장 전 촬영을 정지하고 이전 CaptureSession과 GigE reader 종료가
     완료될 때까지 기다린다. 공장 모드에서는 수집 중 설정 저장이 거부된다. 저장은
     Backend asset 등록, ROI threshold 동기화, 로컬 `config.json` 원자적 교체 순으로
     수행되며, 어느 단계든 실패하면 촬영을 정지한 채 원인을 해결하고 다시 승인한다.
   - `0 < warning_delta < critical_delta`여야 한다.
   - Backend의 활성 `threshold_profiles`에도 같은 순서, 유한 온도값, 양수 hotspot
     크기와 `min_hotspot_size <= min_hotspot_size_max`, 음수가 아닌 cooldown이
     적용되어야 한다. 활성 ROI는 `(camera_id, roi_name)`당 한 버전만 있어야 한다.
     전환 전 읽기 전용 감사에서 기존 위반·중복이 발견되면 Dashboard나 배포 스크립트로
     자동 수정하지 말고 DBA 승인 정리 및 재검증이 끝날 때까지 현장 전환을 차단한다.
   - 데이터셋은 볼륨 루트가 아닌 전용 하위 폴더여야 한다. 예:
     `/media/thermoguard-data/line-a`.
   - 무결성 복구, metadata 재생성, 삭제 보존 작업은 대시보드의 자동 타이머에서
     실행되지 않는다. 별도 승인된 유지보수 절차에서만, 백업 확인 후 전용 폴더에
     marker를 명시적으로 만든다.
     ```bash
     /opt/thermoguard/venv/bin/python -c \
       "from thermal_monitoring.data.cleanup import initialize_dataset_marker; \
       initialize_dataset_marker('/approved/dataset/subdirectory')"
     ```
     marker 생성은 삭제 권한을 부여하므로 볼륨 루트·홈·저장소 루트에는 실패해야
     정상이다.
3. 설정과 런타임을 읽기 전용으로 검사한다.

   ```bash
   cd /opt/thermoguard/current
   sudo -u thermoguard env \
     THERMOGUARD_CONFIG=/var/lib/thermoguard/config.json \
     THERMOGUARD_DASHBOARD_ENV=/var/lib/thermoguard/dashboard.env \
     THERMOGUARD_LOG_DIR=/var/log/thermoguard \
     THERMOGUARD_FACTORY_MODE=1 \
     /opt/thermoguard/venv/bin/python -m thermal_monitoring.preflight --online
   ```

   `preflight --online`은 카메라와 `/api/ready`를 호출한다. 라인 담당자가 승인한
   시험 창에서만 실행하며, 이 명령과 앞 단계의 fingerprint 검사가 모두 오류 없이
   끝나야 한다. HTTP thermal 응답에서 ExifTool 온도 메타데이터를 읽고,
   5초 프로브와 30초 thermal+visual 저장이 각각 유지되는지 확인해야 한다.
4. 승인된 런처로 한 번만 실행한다.

   ```bash
   /usr/local/bin/thermoguard-dashboard
   ```

5. 정상 촬영, 의도한 시험 경보, DB 저장, Telegram 전달(활성화했을 때), GUI 종료
   뒤 서비스 상태를 각각 기록한다. Critical Telegram은 느리거나 사용할 수 없는
   DB 저장을 기다리지 않고 즉시 시도된다. 따라서 `alert_id`를 얻기 전에 Telegram이
   도착할 수 있다. 이후 최대 15초 동안 alert ID를 얻으면 감사 기록을 best-effort로
   추가하지만, 지연·장애 시 `notification_deliveries` 이력이 없을 수 있다.
   Telegram 도착과 DB 이벤트·감사 이력은 각각 별도로 판정한다. 한 항목이라도
   실패하면 운전 전환 대신 롤백 판단으로 넘어간다.

## 6. 롤백

1. 대시보드는 GUI의 정상 종료로 멈추고, 백엔드는 정비 승인 하에 멈춘다.
2. release 또는 venv 전환 실패는 [3.1 전환 실패 시 rollback](#31-전환-실패-시-rollback)의
   명령으로 이전 `current`와 `venv` 링크를 함께 원자적으로 되돌린다. 이전 release의
   systemd unit도 함께 복원하고 `daemon-reload` 뒤 명시적으로 backend를 재시작한다.
3. 백업한 `/etc/thermoguard/hotspot-backend.env`,
   `/var/lib/thermoguard/config.json`, `/var/lib/thermoguard/dashboard.env`는
   실제로 변경된 경우에만 권한을 보존해 복원한다. DB 변경이 있었다면 사전에 지정한
   DBA가 해당 백업과 검증 절차로만 복원한다.
4. rollback backend의 `/api/health`, `/api/ready`, journal, schema fingerprint를
   재확인한다. 모두 통과할 때까지 Dashboard를 시작하지 않는다.
5. 모든 확인을 통과한 뒤에만 승인된 런처로 Dashboard를 한 번 실행한다.

롤백은 대시보드 이전 릴리스로의 복귀이지 레거시 collector의 재가동이 아니다.
collector 마스크 해제는 별도 사고 승인과 원인 분석 없이는 금지한다.
