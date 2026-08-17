# 데이터베이스 기준선과 전환 절차

이 디렉터리는 **자동 마이그레이션 실행기**가 아니다. 현장 DB에 예고되지 않은
DDL을 적용하는 위험을 피하기 위해, 현재 릴리스는 읽기 전용 기준선 검사만
제공한다.

기준선은 상위 디렉터리의 [`schema_manifest.json`](../schema_manifest.json)에
소스 관리된다. 이 파일에는 필요한 17개 테이블과 해당 테이블 DDL의 SHA-256
지문만 포함되며, DB 접속 정보·운영 데이터·시드 데이터는 포함하지 않는다.

## 현장 전환 전 확인

백엔드 가상환경과 `Project_hotspot/backend` 작업 경로에서 다음을 실행한다.

```bash
./venv/bin/python schema_preflight.py --json
./venv/bin/python schema_preflight.py --verify-fingerprint --json
```

공장 설치에서는 backend 서비스와 같은 보호 환경 파일을 명시하되, DB 비밀번호를
명령행에 쓰지 않는다. 이 파일은 root만 읽고, 점검 명령도 root가 실행한다.

```bash
cd /opt/thermoguard/current/Project_hotspot/backend
sudo /usr/bin/env -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  PYTHONDONTWRITEBYTECODE=1 \
  THERMOGUARD_BACKEND_ENV=/etc/thermoguard/hotspot-backend.env \
  /opt/thermoguard/venv/bin/python schema_preflight.py --verify-fingerprint --json
```

첫 명령은 `SHOW TABLES`로 필요한 테이블의 존재만 검사한다. 두 번째 명령은
추가로 각 테이블에 `SHOW CREATE TABLE`을 실행해, 정규화한 DDL이
`schema_manifest.json`의 기준선과 일치하는지 확인한다. 두 명령 모두
`CREATE`, `ALTER`, `INSERT`, `UPDATE`, `DELETE`, `DROP`을 실행하지 않는다.

- 종료 코드 `0`: 기준선 검사 통과
- 종료 코드 `1`: 필수 테이블 누락 또는 구조 드리프트
- 종료 코드 `2`: DB 연결 또는 매니페스트를 확인할 수 없음

`drifted`, `not_ready`, `error` 결과에서는 대시보드 자동 기동을 진행하지 말고,
운영 DB 백업과 승인된 변경 이력을 먼저 확인한다. 매니페스트를 운영 DB 결과에
맞춰 즉시 바꾸는 것은 기존 드리프트를 정상으로 승인하는 행위이므로 금지한다.

## 기준선 지문의 범위

지문은 테이블 이름을 정렬한 뒤 각 `SHOW CREATE TABLE` 결과를 연결하여 계산한다.
운영 중 증가하는 `AUTO_INCREMENT` 카운터만 `<dynamic>`으로 정규화하고, 컬럼,
인덱스, 제약 조건, 문자셋 및 테이블 옵션의 다른 차이는 모두 드리프트로
취급한다. 따라서 MariaDB 버전 또는 기본 문자셋을 바꾸는 작업도 사전 검증이
필요하다.

## 스키마 변경 승인 절차

1. 운영과 분리된 스테이징 DB에서, 리뷰되고 백업 가능한 변경안을 적용한다.
2. 기존 API 계약과 백업/복구 절차를 검증한다.
3. 스테이징에서 `schema_preflight.py --verify-fingerprint --json`의
   `actual_fingerprint`을 검토한다.
4. 승인된 변경의 테이블 목록과 지문만 `schema_manifest.json`에 반영하고,
   변경 사유와 복구 절차를 같은 변경 요청에 기록한다.
5. 현장 배포 직전에 다시 읽기 전용 검사를 실행한다. 이 저장소의 도구가
   현장 DB에 DDL을 실행하도록 확장하지 않는다.

신규 빈 DB를 만들기 위한 `0001_initial_schema.sql`은 의도적으로 제공하지
않는다. 검증되지 않은 전체 DDL을 새 설비 또는 기존 설비에 적용하면 운영 DB의
변경 이력과 충돌할 수 있다. 신규 구축은 승인된 DB 백업/프로비저닝 산출물과
함께 진행한 뒤, 위 읽기 전용 검사를 통과해야 한다.
