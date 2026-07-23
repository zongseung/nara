# nara — 나라장터 종합쇼핑몰 가격비교 자동화

조달청 나라장터 종합쇼핑몰(shop.g2b.go.kr)에서 상품·단가·규격·이미지를 수집하고,
**우리 품목과 유사한 경쟁사 품목을 자동 매칭해 "가격 비교표(엑셀)"를 생성**하는 파이프라인.

수천 개 상품을 필터 걸고 하나하나 눈으로 비교하던 수작업을 스크립트/엔드포인트 한 번으로 대체한다.

---

## 배경

- 종합쇼핑몰에는 동일 품목이 **다수공급자계약(MAS)** 과 **제3자단가계약**으로 여러 업체에 등록되어 있다.
- 경쟁사 대비 우리 단가를 비교하려면 "같은 규격·재질·용도"의 품목을 골라 표를 만들어야 하는데, 품목이 수천~수만 개라 수작업이 곧 노가다가 된다.
- 이 프로젝트는 그 과정을 **데이터 수집 → 유사 매칭 → 비교표 생성**으로 자동화한다.

## 어떻게 되는가 (핵심 발견)

종합쇼핑몰 UI(WebSquare) 뒤에서 검색은 **평문 JSON API 한 방**으로 동작한다:

```
POST https://shop.g2b.go.kr/gm/gms/gmsd/newShopUntySrchApi.do
→ 200, 순수 JSON (암호화·로그인 없음)
```

응답 `rsltList[]`에 필요한 필드가 날것으로 들어있다:

| 용도 | JSON 필드 |
|---|---|
| 단가 | `ctrtUprc` |
| MAS vs 제3자단가 | `masCtrtYn` / `shopCtrtTyNm` |
| 계약 건 구분 | `ctrtNo` · `ctrtChgOrd` · `lastCtrtYn` |
| 세부품명/분류 | `dtlsPrnm` · `dtlsPrnmNo` · `itemClsfNo` |
| 규격·재질·용도 | `pdctAtrbCdDtlNm` (가로$세로$두께$재질 … 용도:…) |
| 업체·식별번호 | `mnftrEtpsNm` · `bzmnRegNo` · `itemIdnfNo` |
| **이미지** | `imgSrc` (원본 JPG URL — 캡처 아님, 직접 다운로드) |

추가로 확인된 특성:
- 페이지 크기는 서버가 **100으로 상한 고정** (더 요청해도 100만 옴).
- 빈 키워드 = 전체 몰 **약 104만 건**. deep-offset은 뒤 페이지가 느려짐(page 10,000 ≈ 8초).
- 가격 구간(`startPrce`/`endPrce`)·업체명(`etpsNm`) 필터 지원.
- 동시요청 정상(실측: 이미지 20장/동시 8 ≈ 0.2초, 100%).

## 아키텍처

```
우리 품목 (사내 DB or 나라장터 업체명 검색)
        │
        ▼
  g2b_client  ── 종합쇼핑몰 JSON API 크롤 (키워드/가격/업체명/페이징)
        │
        ▼
  matcher     ── 재질·규격·용도·가격 유사도 점수 → 경쟁사 후보 top-N (자기 업체 제외)
        │
        ├──▶ build_report ── 비교표 엑셀 생성 (이미지 임베드)
        │
        └──▶ app.py /match ── FastAPI 엔드포인트: 매칭 + 이미지 async 수집(우리 스토리지) + DB upsert
```

## 구성 파일

| 파일 | 역할 |
|---|---|
| **`format_convert.py`** | **품목등록결과(.xls) → 협상품목리스트 양식(.xlsx) 변환 + 마크다운 미리보기** |
| **`compare_file.py`** | **제안업체 .xls → 각 품목 동일단가 경쟁사 매칭 → 업체별 가격비교표** |
| `g2b_client.py` | 종합쇼핑몰 크롤 코어 — 키워드/가격구간/업체명 검색, 페이징, 필드 정리 |
| `matcher.py` | 유사도 매칭 — 재질(0.35)·규격(0.25)·용도(0.25)·가격(0.15) 가중합 + 동일단가 필터 |
| `build_report.py` | 비교표 엑셀 생성 (다품목 블록 + 이미지 임베드, 업체별 시트) |
| `batch_match.py` | 업체 전 품목 배치 매칭 → 비교표 일괄 생성 |
| `crawl_all.py` | 전체 카탈로그(104만) 덤프 → SQLite (가격 샤딩 + 동시요청 + 재개) |
| `app.py` | FastAPI `/match` 엔드포인트 (MySQL/SQLite 공통) |
| `demo_match.py` | 로컬 SQLite로 `/match` 라이브 데모 (MySQL 불필요) |
| `server.py` | MCP 서버 (인터랙티브 키워드→엑셀) |
| `schema.sql` | MySQL 스키마 (our_product / competitor_match / product_image) |
| `DESIGN.md` | 아키텍처·데이터모델·API 계약 설계 문서 |
| `test_*.py` | 파서·가격샤딩 자체검증 (네트워크 불필요) |

## 빠른 시작 — 실제 업무 흐름

제안업체 품목등록결과(`.xls`) → ① 협상품목리스트(제출 양식) + ② 가격비교표(동일단가 경쟁사).

### 0) 최초 1회 세팅
```bash
cd nara
python3 -m venv .venv
source .venv/bin/activate          # 프롬프트에 (.venv) 뜨면 성공 (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
```
> 이후엔 새 터미널마다 `cd nara && source .venv/bin/activate` 만.

### 1) 협상품목리스트 만들기 (제출 양식)
```bash
python format_convert.py "품목등록결과.xls" --out 협상품목리스트.xlsx
```
- 실행하면 업체별 시트를 **마크다운 표로 미리보기** → 확인 후 `협상품목리스트.xlsx` 저장
- 헤더명 자동인식(시트마다 컬럼 위치 달라도 OK) · 식별번호 `.0`/앞 탭 자동정리
- 옵션: `--rows 5`(미리보기 행수) · `--no-xlsx`(미리보기만, 저장 안 함) · `--days 40 --qty 5`(고정값)

### 2) 가격비교표 만들기 (동일단가 경쟁사)
```bash
python compare_file.py "품목등록결과.xls" --out 가격비교표.xlsx
```
- 각 품목 → 나라장터에서 **같은 단가** 경쟁사를 찾아 유사도(규격·재질·용도) 순 top3 → 업체별 시트 + 경쟁사 이미지 임베드
- 나라장터 **실시간 조회**라 품목 수만큼 검색(98건이면 몇 분, 진행상황 출력)
- 옵션: `--top 3`(경쟁사 수) · `--limit 2`(업체당 품목 상한, 빠른 테스트)

> 입력 `.xls`가 다른 폴더에 있으면 경로로 지정: `python compare_file.py "../품목등록결과.xls"`
> (예: 파일이 `~/Downloads`, 코드가 `~/Downloads/nara` 면 `../` 붙임)

## 기타 도구

| 명령 | 용도 |
|---|---|
| `uvicorn webapp:app --host 0.0.0.0 --port 8000` | 브라우저 동적 검색(키워드·계약구분·동일단가) + 엑셀 다운로드 |
| `python batch_match.py --company "업체명"` | 나라장터 등록 업체명으로 전 품목 비교표 |
| `python crawl_all.py` → `python export_parquet.py` | 종합쇼핑몰 전체(104만) 덤프 → SQLite → Parquet |
| `python demo_match.py` | `/match` API 라이브 데모(SQLite, MySQL 불필요) |
| `uvicorn app:app` (+ `schema.sql`, `DB_URL` MySQL) | `/match` API 운영 |
| `server.py` (`.mcp.json` 등록) | 키워드 검색 MCP 서버 |

### 검증
```bash
.venv/bin/python test_g2b_client.py    # 파서
.venv/bin/python test_crawl.py         # 가격 샤딩
```

## 데이터 모델 (MySQL)

- `our_product` — **PK=사내 품번**. 물품식별번호는 nullable 속성. 규격/재질/용도/가격/모델 등 매칭 구동 컬럼.
- `competitor_match` — 우리품목 1:N 경쟁사item. `run_id`로 재실행 이력. 점수·순위·경쟁사 스펙·단가·계약구분.
- `product_image` — 이미지는 우리 스토리지에 저장(스토리지 B), 우리 URL로 서빙.

자세한 스키마·API 계약은 [`DESIGN.md`](DESIGN.md) 참고.

## 매칭 로직

우리 품목 1건 vs 후보:
```
유사도 = 0.35·재질동일 + 0.25·규격근접 + 0.25·용도유사(difflib) + 0.15·가격근접
후보 풀 = 같은 가격대(±price_band) + 키워드(품명), 자기 업체 제외
```
규칙 기반·투명(설명 가능)하며, `matcher.WEIGHTS`로 조정한다. 완전 자동 확정이 아니라 **top-N 후보를 추려 사람이 검수하는 반자동**을 전제로 한다.

## 한계

- **내부 비공식 API** — 차세대 나라장터 개편 시 엔드포인트/필드가 바뀔 수 있다. 깨지면 공식 오픈API(data.go.kr `15129471` 종합쇼핑몰 품목정보)로 교체.
- 규격/재질/용도 파싱은 `pdctAtrbCdDtlNm` 문자열 휴리스틱 — 표기가 특이한 품목은 놓칠 수 있다.
- 고가 품목은 나라장터에 비교군이 희박해 후보 품질이 낮을 수 있다.
- 전체 카탈로그 덤프는 1회 스냅샷(실행 중 신규등록/단가변경 미반영).

## 주의

공개된 종합쇼핑몰 데이터를 조회하는 용도이며, 대량 수집 시 대상 서버에 과부하가 가지 않도록 동시요청 수·지연을 적절히 제한한다.
