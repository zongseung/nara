# nara — 나라장터 종합쇼핑몰 가격비교 자동화

제안업체 품목등록결과(`.xls`)를 받아 → ① **협상품목리스트(제출 양식)** 를 만들고
→ ② 각 품목마다 나라장터에서 **동일단가 경쟁사**를 찾아 **가격비교표(엑셀)** 를 생성한다.

수천 개 상품을 필터 걸고 하나하나 눈으로 비교하던 수작업을 CLI 두 줄로 대체한다.

---

## 배경

- 종합쇼핑몰에는 동일 품목이 **다수공급자계약(MAS)** 과 **제3자단가계약**으로 여러 업체에 등록되어 있다.
- 협상 시 우리 단가를 방어하려면 "같은 단가 · 같은 규격/재질/용도"의 경쟁사 품목을 골라 비교표를 만들어야 하는데, 품목이 수천~수만 개라 수작업이 곧 노가다가 된다.
- 이 프로젝트는 그 과정을 **양식 변환 → 동일단가 경쟁사 매칭 → 비교표 생성**으로 자동화한다.

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
| 세부품명/분류 | `dtlsPrnm` · `dtlsPrnmNo` · `itemClsfNo` |
| 규격·재질·용도 | `pdctAtrbCdDtlNm` |
| 업체·식별번호 | `mnftrEtpsNm` · `itemIdnfNo` |
| **이미지** | `imgSrc` (원본 JPG URL — 캡처 아님, 직접 다운로드) |

추가 특성: 페이지 크기 서버 상한 100 · 가격구간(`startPrce`/`endPrce`)·업체명(`etpsNm`) 필터 지원 · 동시요청 정상(실측 이미지 20장/동시 8 ≈ 0.2초).

## 아키텍처

```
제안업체 품목등록결과(.xls)
   │
   ├─ format_convert ── 헤더 자동인식 ──▶ 협상품목리스트(제출 양식).xlsx
   │
   └─ compare_file
        │  g2b_client   ── 종합쇼핑몰 JSON API (키워드/가격/페이징)
        │  matcher      ── 동일단가 필터 + 재질·규격·용도 유사도 → top-N (자기 업체 제외)
        │  build_report ── 업체별 시트 비교표 + 경쟁사 이미지 임베드
        ▼
      가격비교표.xlsx
```

## 구성 파일

| 파일 | 역할 |
|---|---|
| **`format_convert.py`** | 품목등록결과(.xls) → 협상품목리스트 양식(.xlsx) 변환 + 마크다운 미리보기 |
| **`compare_file.py`** | 제안업체 .xls → 각 품목 동일단가 경쟁사 매칭 → 업체별 가격비교표 |
| `g2b_client.py` | 종합쇼핑몰 크롤 코어 — 키워드/가격구간/업체명 검색, 페이징, 필드 정리 |
| `matcher.py` | 유사도 매칭(재질 0.35·규격 0.25·용도 0.25·가격 0.15) + 동일단가 필터 |
| `build_report.py` | 가격비교표 엑셀 (업체별 시트, 다품목 블록, 이미지 임베드) |
| `crawl_all.py` · `export_parquet.py` | 전체 카탈로그(104만) 덤프 → SQLite → Parquet (선택) |
| `test_*.py` | 파서·가격샤딩 자체검증 (네트워크 불필요) |

## 빠른 시작 — 실제 업무 흐름

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
- 업체별 시트를 **마크다운 표로 미리보기** → 확인 후 `협상품목리스트.xlsx` 저장
- 헤더명 자동인식(시트마다 컬럼 위치 달라도 OK) · 식별번호 `.0`/앞 탭 자동정리
- 옵션: `--rows 5`(미리보기 행수) · `--no-xlsx`(미리보기만) · `--days 40 --qty 5`(고정값)

### 2) 가격비교표 만들기 (동일단가 경쟁사)
```bash
python compare_file.py "품목등록결과.xls" --out 가격비교표.xlsx
```
- 각 품목 → 나라장터에서 **같은 단가** 경쟁사를 찾아 유사도(규격·재질·용도) 순 top3 → 업체별 시트 + 경쟁사 이미지 임베드
- 나라장터 **실시간 조회**라 품목 수만큼 검색(98건이면 몇 분, 진행상황 출력)
- 옵션: `--top 3`(경쟁사 수) · `--limit 2`(업체당 품목 상한, 빠른 테스트)

> 입력 `.xls`가 다른 폴더에 있으면 경로 지정: `python compare_file.py "../품목등록결과.xls"`

## 매칭 로직

```
후보 풀  = 동일단가(compare_file 기본) + 키워드(품명), 자기 업체 제외
유사도   = 0.35·재질동일 + 0.25·규격근접 + 0.25·용도유사(difflib) + 0.15·가격근접
```
규칙 기반·투명(설명 가능)하며 `matcher.WEIGHTS`로 조정. 완전 자동 확정이 아니라 **top-N 후보를 추려 사람이 검수하는 반자동**을 전제로 한다.

## 기타 도구 (선택)

```bash
python crawl_all.py            # 종합쇼핑몰 전체(104만) → shop_all.db (약 1시간, 재개 가능)
python export_parquet.py      # shop_all.db → shop_all.parquet (DuckDB/pandas 쿼리)
python test_g2b_client.py     # 파서 자체검증
python test_crawl.py          # 가격 샤딩 자체검증
```

## 한계

- **내부 비공식 API** — 차세대 나라장터 개편 시 엔드포인트/필드가 바뀔 수 있다. 깨지면 `g2b_client.py` 한 파일 수정, 또는 공식 오픈API(data.go.kr `15129471`)로 교체.
- 규격/재질/용도 매칭은 휴리스틱 — 표기가 특이한 품목·고가(비교군 희박) 품목은 후보 품질이 낮을 수 있어 사람 검수 필요.
- `compare_file`은 실행마다 나라장터 실시간 조회(품목 수만큼 호출). 반복·대량이면 받아둔 `shop_all.parquet`을 쓰도록 소스만 교체하면 호출 0.

## 주의

공개된 종합쇼핑몰 데이터를 조회하는 용도이며, 대량 수집 시 서버에 과부하가 가지 않도록 동시요청 수·지연을 적절히 제한한다.
