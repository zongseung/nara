# 비교 웹 실행 안내

촬영용 임시 설정: `core/comparison.py`의 `CHECK_SUPPORTS = False`로 지지대 개수 검사만 생략합니다. 웹의 지지대 확인 버튼은 숨기고 생략 사실을 표시합니다. 기존 작업 파일은 자동 재매칭하지 않으므로 새 수집을 시작하세요. 운영 복원은 이 값을 `True`로 바꾸고 웹 안내·버튼을 복원한 뒤 API·워커를 재시작하거나 Docker 이미지를 재빌드합니다.

Figma의 `0:1` 페이지에 있는 Furrow 4개 화면을 바탕으로 구현했습니다. 조건 입력, 수집 진행, 경쟁사 검토, 비교표 미리보기와 다운로드를 제공합니다. `src/web/static`은 빌드가 필요 없는 HTML/CSS/JavaScript이며 FastAPI가 같은 주소에서 제공합니다.

## Docker로 웹 전체 실행

`docker/compose.web.yaml`은 Nginx, 웹 API, 웹 워커, 응답 캐시와 Prefect 제어 서버를 함께 실행합니다. 애플리케이션용 DB를 추가하지 않으며 작업 JSON·Excel은 `web-data` 볼륨에 저장합니다.

```sh
cd docker
# 최초 한 번만 복사한 뒤 인증값을 설정합니다. 기존 .env는 덮어쓰지 않습니다.
cp -n .env.example .env
docker compose -p nara-web-deploy -f compose.web.yaml up -d --build
docker compose -p nara-web-deploy -f compose.web.yaml ps
```

[http://127.0.0.1:8080](http://127.0.0.1:8080)에서 접속합니다. 외부 포트는 Nginx 하나만 열립니다. API·Redis·PostgreSQL·Prefect는 Docker 내부에서 통신합니다. 초기 등록이 성공한 뒤 API와 워커가 시작하며, 워커는 한 번에 한 작업만 실행합니다. 원천 수집은 화면에서 작업을 제출해야 시작합니다.

별도 worktree에서 원래 체크아웃의 환경 파일을 사용할 때는 `docker compose --env-file /Users/ijongseung/nara/docker/.env -p nara-web-deploy -f compose.web.yaml ...`처럼 지정합니다. 이전 호스트 실행의 `storage/web` 작업은 새 볼륨으로 자동 이동하지 않습니다.

```sh
docker compose -p nara-web-deploy -f compose.web.yaml logs --tail 100 -f web-api web-worker web-gateway
# 코드 변경 반영: 작업이 끝난 뒤 실행합니다.
docker compose -p nara-web-deploy -f compose.web.yaml up -d --build
# 정지: 볼륨은 보존합니다. down -v는 작업 파일과 제어 DB를 삭제합니다.
docker compose -p nara-web-deploy -f compose.web.yaml down
```

백업 대상은 `web-data`와 Prefect PostgreSQL입니다. 파일 백업은 수집·내보내기가 끝나고 API·워커를 멈춘 상태에서 수행합니다. 캐시는 백업하지 않습니다. 프로젝트 이름을 바꾸면 별도의 볼륨을 사용하므로 운영 중에는 `-p` 값을 유지합니다.

기본 바인딩은 localhost입니다. 사내 인증 프록시·TLS를 준비한 뒤 `NARA_WEB_BIND`, `NARA_WEB_PORT`, `NARA_WEB_HOSTS`를 실제 접근 주소에 맞춥니다. 앱 자체의 계정·권한 관리는 포함하지 않습니다.

### 캐시 범위

- `web-cache`는 Prefect 큐용 Redis와 분리합니다. 메모리 상한 128MB, `allkeys-lru`, 디스크 영속화 없음입니다.
- `GET /jobs/{id}/items`의 직렬화 응답만 최대 60초 캐시합니다. 응답 하나가 2MB를 넘으면 저장하지 않습니다.
- 작업 ID·실행 ID·개정 번호·갱신 시각을 키에 넣어 수집 진행이나 검토 변경 후에는 이전 응답을 재사용하지 않습니다. `X-Cache: HIT/MISS`로 확인할 수 있습니다.
- Redis 장애 시 파일을 직접 읽습니다. 연결 대기 상한 0.2초, 실패 후 5초간 재접속을 미룹니다.
- 원천 가격·작업 상태·이벤트·Excel은 캐시하지 않습니다. Nginx 프록시 캐시도 사용하지 않으며 브라우저에는 `Cache-Control: no-store`를 반환합니다.

메모리 정책은 [Redis eviction 문서](https://redis.io/docs/latest/develop/reference/eviction/)를 따릅니다. 가격 갱신은 캐시 만료와 관계없이 새 수집 작업으로 수행합니다.

웹 이미지의 의존성을 변경했다면 루트에서 잠금 파일을 갱신합니다.

```sh
uv lock
uv export --locked --extra web --extra orchestration --no-dev --no-emit-project \
  --format requirements-txt --output-file docker/requirements-web.lock --quiet
```

## uv로 API·워커 실행

아래 명령은 이 기능이 있는 체크아웃의 루트에서 실행합니다. Python 3.12와 uv, 기존 Prefect 서버가 필요합니다.

```sh
uv sync --locked --extra web --extra orchestration
```

Prefect 서버가 없다면 기존 Docker 설정에서 제어 서버만 시작합니다. `docker/.env.example`을 `docker/.env`로 복사하고 문서에 따라 인증값을 설정합니다. 이미 4200 포트의 Prefect가 실행 중이면 이 단계는 생략합니다.

```sh
docker compose --env-file docker/.env -f docker/compose.yaml up -d --build \
  postgres redis prefect-server prefect-services gateway
```

웹 API, 등록 명령, 워커는 같은 체크아웃과 같은 데이터 디렉터리를 사용해야 합니다. 아래 환경변수는 세 터미널 모두에 적용합니다. 별도 worktree에는 무시된 `.env`가 복사되지 않으므로 `--env-file`에 원래 체크아웃의 절대 경로를 지정해도 됩니다.

```sh
export PREFECT_API_URL=http://127.0.0.1:4200/api
export NARA_WEB_DATA_DIR="$PWD/storage/web"
export NARA_WEB_POOL=nara-web
```

1. 배포 등록: 최초 실행, 코드 경로·풀·데이터 경로를 변경한 경우 실행합니다.

```sh
uv run --locked --extra web --extra orchestration --env-file docker/.env \
  python -m src.web register
```

2. 워커: 별도 터미널에서 계속 실행합니다.

```sh
uv run --locked --extra web --extra orchestration --env-file docker/.env \
  python -m src.web worker
```

3. 웹: 또 다른 터미널에서 실행합니다.

```sh
uv run --locked --extra web --extra orchestration --env-file docker/.env \
  python -m src.web serve --port 8000
```

[http://127.0.0.1:8000](http://127.0.0.1:8000)에 접속합니다. 업체명·검색어와 품목 수를 입력하거나 기존 품목등록결과 `.xls`를 선택합니다. 기준 품목 수의 기본값은 전체(`limit=0`)입니다. 파일 입력은 최대 8MB이고, 업체 조회와 파일 입력 모두 작업당 최대 10,000개를 처리합니다. `limit=0`도 이 안전 상한을 넘을 수 없습니다.

방향 수 일치는 기본적으로 꺼진 선택 조건입니다. 체크하면 알려진 방향 수가 같아야 합니다. API는 `same_direction: true`, CLI는 `--same-direction`으로 설정합니다. 용도 문구는 순위에만 반영합니다. 규격 각 변 ±10%·가격·명확한 품목 분류/형상 차이 검사는 유지하며 지지대는 촬영용으로 생략합니다.

조건 충족 후보를 먼저 선정하고 빈칸은 확인 필요 → 대안 후보 순으로 보충합니다. 표시면·설치·발광·반사 정보의 미기재/충돌은 확인 필요, 명확한 불일치는 대안 후보입니다. 후보 JSON의 `비교구분`·`확인사항`은 웹과 Excel에 보존합니다. 가격 오름차순 배치는 구분과 별개입니다. 기존 작업은 자동 재매칭하지 않으므로 새 수집을 시작해야 합니다.

`PREFECT_AUTH_STRING`은 실행 진입점에서 Prefect 클라이언트의 `PREFECT_API_AUTH_STRING`으로 전달합니다. 이 값을 브라우저나 작업 JSON에는 포함하지 않습니다. 코드 변경 뒤에는 API와 워커를 재시작합니다.

기존 Docker updater 워커 이미지에는 이 체크아웃의 코드와 호스트 경로가 없습니다. 이 안내에서는 별도 `nara-web` 프로세스 풀을 사용합니다. 배포 동시 실행 한도 1, 워커 `--limit 1`, 파일 잠금으로 동일 데이터 디렉터리에서 웹 작업을 직렬 실행합니다. 원천 조회의 기존 1초 간격은 유지하며 별도로 실행한 CLI 프로세스까지 전역으로 조절하지는 않습니다.

수집 시작 직후에는 접수 상태를 먼저 표시합니다. 앞선 작업이 실행 중이면 대기 안내에 해당 작업의 완료 개수와 이동 버튼을 표시합니다. 접수된 요청은 다시 누르지 않아도 순서가 되면 실행됩니다. 전체 수집은 상세 조회 때문에 시간이 걸리므로 촬영용 일부 확인은 기준 품목 수를 3으로 지정할 수 있습니다. 정적 화면 파일은 `Cache-Control: no-cache`로 재검증하며, 이전 배포 코드가 이미 브라우저 캐시에 남았다면 한 번 강력 새로고침합니다.

## 저장과 검토

애플리케이션용 DB는 만들지 않습니다. Prefect의 PostgreSQL·Redis는 기존 작업 제어 인프라이며 품목 DB가 아닙니다.

```text
storage/web/<작업 UUID>/
  job.json                  요청, 상태, 개정 번호, Prefect 실행 ID
  input.xls                 업로드 입력이 있는 경우
  items/<번호>.json         원천 품목, 선정 후보, 수동 제외, 확인 근거
  events.jsonl              품목별 진행과 검토 기록
  comparison-<개정>-<실행>.xlsx
  items-before-<실행>/       수집 재시도 전 품목 보존본
```

JSON은 임시 파일과 원자적 교체로 저장하고, 상태 변경에는 파일 잠금을 사용합니다. API와 워커는 로컬 파일시스템을 공유하는 한 호스트에서 운영합니다. 여러 호스트나 파일 잠금을 지원하지 않는 네트워크 파일시스템은 이 구성의 대상이 아닙니다. 작업 파일은 자동 삭제하지 않으므로 보존기간과 디스크 정리는 운영 시 정해야 합니다.

- 수집만 끝나면 JSON을 저장하고 검토 단계에서 멈춥니다. Excel은 사용자가 생성 버튼을 눌러야 만듭니다.
- 후보를 제외·복원할 수 있습니다. 변경하면 개정 번호를 올리고 이전 Excel 다운로드를 무효화합니다. 생성은 저장된 선정 결과를 사용하며 다시 매칭하지 않습니다. 사진은 기존 렌더러가 원천 URL에서 읽습니다.
- 후보 카드의 **다른 후보 찾기**에서 포함할 재질을 선택적으로 입력하면 해당 후보만 Prefect로 재검색합니다. 재질은 기존 정규화로 별칭을 해석하며, 입력한 계열·등급이 모두 확인된 제품만 허용합니다. 혼합 재질도 가능하고 가격·규격 등 기존 조건은 완화하지 않습니다. 자유 문장 해석은 지원하지 않습니다.
- 재검색은 원천 검색을 새로 호출합니다. 다른 품목과 유지할 후보는 저장된 값을 보존하고, 대체 후 가격 오름차순으로 정렬합니다. 해당 품목의 거절 식별번호를 기록해 반복 추천을 막고 수동 제외 상태도 보존합니다. 검색 범위는 기존 최대 500건과 상세 조회 한도를 따르므로 전체 시장에 대체 제품이 없다는 뜻은 아닙니다.
- 대체 후보가 없거나 조회 실패·중지가 발생하면 기존 후보를 보존합니다. 진행 기록에서 사유를 확인하고 실패 시 재시도할 수 있습니다. 재검색 접수 시 이전 Excel은 무효화되며 검토 후 다시 생성합니다. 기준 품목의 검색·탈락 집계는 최초 비교 결과를 유지합니다.
- 지지대 확인 API는 확인값과 원천 `_raw`를 별도로 보존하고 원천 값과 충돌하는 입력을 거절합니다. 현재 촬영용 설정에서는 지지대 검사를 생략하므로 웹 입력 버튼은 숨겨져 있습니다.
- 검토 화면에서 기준 품목의 `후보진단`으로 검색 총수·검사 후보 수·조회 범위 제한·첫 탈락 사유별 건수·상세 조회 수를 보여줍니다. 개별 탈락 후보 원문은 보존하지 않습니다. 집계는 최종 검색 1회의 결과이며 조건별 중복 집계가 아닙니다.
- 중지는 진행 중인 원천 요청이 끝난 뒤 반영합니다. 작업 실패·Prefect 실행 종료는 화면에 표시하며 재시도할 수 있습니다. Prefect 제출 응답이 끊긴 경우에도 같은 실행 ID로 중복 제출을 방지합니다.
- 업로드 파일의 가격은 기존 `희망가(부가세포함)`, 업체 조회 가격은 원천 계약단가입니다. 가격을 다시 조회하려면 새 작업을 생성합니다.
- 협상품목리스트 변환은 기존 `format_convert.py` CLI를 사용합니다. 이 웹의 내보내기는 가격비교표입니다.

## API

변경 요청은 JSON을 사용합니다. `revision`은 `GET /jobs/{id}`에서 읽은 현재 값을 보내야 합니다. 다른 창에서 수정되었거나 작업 중이면 409를 반환합니다.

| 요청 | 동작 |
|---|---|
| `POST /jobs` | 업체·검색어·한도·가격 모드·방향 수 일치 선택으로 작업 제출, 202 |
| `POST /jobs/upload?limit=3&top=3` | `.xls` 원본 바이트 업로드, 202 |
| `GET /jobs` | 최근 50개 작업 |
| `GET /jobs/{id}` | 상태, 개정 번호, 실패·보류 메시지 |
| `GET /jobs/{id}/items` | 검토할 기준 품목과 후보; 내부 원천 응답 제외 |
| `GET /jobs/{id}/events` | 최근 100개 진행·검토 이벤트 |
| `POST /jobs/{id}/items/{n}/supports` | `revision`, `count`, `note`로 확인값 저장 후 재비교 |
| `POST /jobs/{id}/items/{n}/selection` | `revision`, `excluded` 후보 인덱스 목록 저장 |
| `POST /jobs/{id}/items/{n}/research` | `revision`, `replace_item_id`, 선택 `conditions.material`로 후보 한 개 재검색, 202 |
| `POST /jobs/{id}/workbook` | `revision`의 검토 결과로 Excel 생성, 202 |
| `GET /jobs/{id}/workbook` | 현재 검토 결과의 Excel 다운로드 |
| `POST /jobs/{id}/retry` | 제출 재시도 또는 실패·중지 작업 재실행 |
| `POST /jobs/{id}/cancel` | 중지 요청 |
| `GET /health` | API와 파일 저장 방식 확인 |

```sh
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"company":"지피코리아","keyword":"안내판","limit":1,"top":3}'
```

현재 실행기는 localhost에만 바인딩합니다. 계정·권한 관리 기능은 포함하지 않았습니다. 사내 공동 사용으로 확장할 때는 회사 인증 프록시와 TLS 뒤에 배치하고 `NARA_WEB_HOSTS`를 실제 호스트명으로 제한해야 합니다. 다른 출처의 브라우저 변경 요청은 거절합니다.

## 검증

```sh
uv run --locked --extra web --extra orchestration python -m unittest \
  test.test_web_jobs test.test_web_cache test.test_web_compose \
  test.test_comparison_options test.test_legacy_matching test.test_company_comparison test.test_architecture
node --check src/web/static/app.js
node --check src/web/static/views.js
```

웹 테스트는 임시 파일 저장소와 검증용 원천을 사용해 비동기 제출, 검토 후 내보내기, 지지대 확인, 가격 정밀도, 중복 실행, 실패·중지·재시도와 이전 파일 무효화를 확인합니다. 실제 사이트 접근 여부는 별도로 확인해야 합니다.
