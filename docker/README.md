# Docker 실행

현재 DB 없이 실시간 수집할 때는 저장소 루트에서
`uv run nara collect --keyword "안내판" --limit 500 --workers 4 --out catalog-live.xlsx`를
실행합니다. Docker를 시작할 필요가 없습니다. [터미널 실행 안내](../docs/uv-runtime.md)를
참고하세요. 아래는 기존 DB 발행·이력·예약 갱신용 선택형 서비스의 실행 방법입니다.

저장소 루트에서 실행합니다. Docker Compose 2.20.3 이상이 필요합니다.

새 환경에서는 최초 한 번 `.env.example`을 이 폴더의 `.env`로 복사하고 두 비밀번호를 변경합니다. `.env` 권한은 600으로 설정합니다. `.env`는 Git과 Docker 빌드 컨텍스트에서 제외됩니다. 기존 데이터 볼륨을 사용하는 경우 기존 자격증명을 유지합니다.

```sh
docker compose up -d --build
docker compose ps -a
```

루트 `compose.yaml`은 이 폴더의 `compose.yaml`을 불러오는 진입점입니다. 별도 `-f`, `--env-file`, bootstrap 명령 없이 Nginx, PostgreSQL, Redis, Prefect API/services, 등록 작업, 워커, 디스패처를 시작합니다. 등록 작업은 성공 후 `Exited (0)`으로 표시됩니다. 초기 등록은 작업과 스케줄을 비활성으로 만들고 실제 수집은 기본적으로 꺼져 있습니다. 기존 활성화 상태는 재시작해도 유지합니다.

```sh
docker compose exec -T updater-worker python -m src.updater.cli status --json
docker compose logs --tail 100 updater-bootstrap updater-worker
docker compose down
```

접속 주소는 `http://127.0.0.1:4200`이며 `PREFECT_AUTH_STRING`의 사용자명과 비밀번호로 로그인합니다. 사내 접속과 용량 설정은 [게이트웨이 운영 안내](../docs/gateway-operations.md), 수집 활성화와 백업은 [업데이터 운영 안내](../docs/price-updater-operations.md)를 따릅니다. 컨테이너 중지는 볼륨을 지우지 않습니다.

### 최초 수집과 엑셀 내보내기

전체 수집 전에 일부만 테스트하려면 `sample`을 사용합니다. 기본 100건이며 `--limit`은 1~1,000건입니다. 별도 엑셀에 `partial_test`, `coverage_complete=false`를 표시하고 운영 DB에는 발행하지 않습니다. 기존 출력 파일은 덮어쓰지 않으므로 반복할 때 새 파일명을 지정합니다. 최초 운영 발행이 없어도 실행할 수 있습니다.

```sh
docker compose exec -T updater-worker python -m src.updater.cli sample --limit 100 --out /data/catalog-test.xlsx &&
docker compose cp updater-worker:/data/catalog-test.xlsx ./catalog-test.xlsx
```

`docker compose up`은 서비스를 시작하고 작업을 등록합니다. 초기 품목 데이터는 포함하지 않습니다. `status --json`이 `runs: []`, `freshness.status: unpublished`를 반환하면 아직 수집·발행된 데이터가 없는 상태입니다. 이때 `export`는 `no_publication`으로 종료하고 파일을 생성하지 않으므로 `docker compose cp`도 실행할 수 없습니다.

최초 수집 전에 `docker/.env`의 `NARA_SOURCE_ENABLED=true`를 설정합니다. 기본 전체 수집은 깊은 페이지를 미리 검사한 뒤 10페이지 단위로 최대 4개 작업을 병렬 실행합니다. `NARA_PRICE_DOMAIN_JSON`은 선택 사항이며, 별도 가격 분할을 지정할 때만 사용합니다. 수동 수집만 사용할 때는 `NARA_DISPATCH_ENABLED=false`를 유지합니다.

`docker/`에서 시작한 기존 스택은 같은 위치에서 다음 명령을 실행합니다. 환경변수 변경은 `restart`가 아닌 컨테이너 재생성으로 적용합니다.

```sh
docker compose up -d --build
docker compose exec -T updater-worker python -m src.updater.cli refresh --mode full
docker compose exec -T updater-worker python -m src.updater.cli status --json
```

`refresh`는 비동기로 작업을 제출하므로 명령 성공만으로 발행이 완료된 것은 아닙니다. `status --json`의 `collection_progress`에서 수집 건수·페이지 수를 확인합니다. 전체 건수·고유 품목·페이지 수가 맞고 관측 구간이 12시간 이내일 때만 발행합니다. 원본 건수 변동, 반복·누락 페이지는 실패로 처리하고 기존 게시본을 유지합니다. 최초 수집에 실패하면 `unpublished` 상태가 유지됩니다. 발행 완료 뒤 내보내고, 파일 복사는 내보내기가 성공했을 때만 실행합니다.

```sh
docker compose exec -T updater-worker python -m src.updater.cli export --out /data/catalog.xlsx &&
docker compose cp updater-worker:/data/catalog.xlsx ./catalog.xlsx
```

명령을 실행하는 위치에 따라 기본 Compose 프로젝트 이름이 달라집니다. 루트에서 시작한 스택은 기본값 `nara`, `docker/`에서 시작한 스택은 `docker`이며 데이터 볼륨도 분리됩니다. 이미 시작한 위치와 프로젝트 이름을 유지합니다. `docker/`에서 시작한 스택을 루트에서 조작할 때는 `docker compose -p docker -f docker/compose.yaml ...`을 사용합니다.

이 폴더에는 서비스 정의, Dockerfile, Nginx 설정, Prefect 설정, 업데이터 의존성 파일과 테스트용 Compose 정의가 있습니다. 애플리케이션 소스는 `core/`, `src/`, 테스트 코드는 `test/`에 유지합니다.

격리된 fixture QA는 저장소 루트에서 `docker compose --env-file /protected/qa.env -p nara-updater-qa -f docker/compose.yaml -f docker/compose.fixture.yaml up -d --build`로 실행합니다. QA 오버레이는 include 진입점 대신 실제 서비스 정의와 병합합니다. 운영 스택에 fixture 오버레이를 적용하지 않습니다.
