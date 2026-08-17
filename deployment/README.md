# Deployment assets

공장 현장 전환에는 [FACTORY_RUNBOOK.ko.md](FACTORY_RUNBOOK.ko.md)를 사용한다.

- `env/`: 비밀값 없는 환경 파일 예시
- `systemd/`: root 전용 `EnvironmentFile`과 별도 `thermoguard-backend` 계정을
  사용하는 백엔드 unit 템플릿
- `tmpfiles.d/`: root가 생성하는 호스트 공용 Dashboard lock 규칙
- `bin/`: 외부 config/env/log 경로를 고정하는 Dashboard 런처

예시를 복사해 채운 실제 환경 파일은 저장소 밖의 보호된 경로에만 둔다.
