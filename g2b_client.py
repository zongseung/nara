"""
나라장터 종합쇼핑몰(shop.g2b.go.kr) 상품검색 비공식 클라이언트.

공식 UI가 호출하는 내부 JSON 엔드포인트(newShopUntySrchApi.do)를 그대로 사용한다.
로그인 불필요, 응답은 순수 JSON. 페이지 크기는 서버가 100으로 상한을 고정한다
(500/1000을 요청해도 100건만 반환됨 — 실측 확인).

ponytail: 내부 비공식 API. 차세대 나라장터 개편 시 엔드포인트/필드가 바뀔 수 있다.
          깨지면 공식 오픈API(data.go.kr 15129471 종합쇼핑몰 품목정보)로 교체.
"""
import html
import time
import requests

BASE = "https://shop.g2b.go.kr"
SEARCH_URL = BASE + "/gm/gms/gmsd/newShopUntySrchApi.do"
PAGE_SIZE = 100  # 서버가 100으로 캡함. 더 요청해도 100만 온다.

# UI가 보내는 정적 헤더 (CSRF 토큰·암호화 없음). 그대로 재현.
_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json",
    "submissionid": "mf_wfm_container_tabShopSubHeader_contents_tabShopLstFormCon_body_sbmSrch",
    "Menu-Info": '{"menuNo":"12167","menuCangVal":"GMSD003_01","bsneClsfCd":"%EC%97%85130034","scrnNo":"07677"}',
    "User-Agent": "Mozilla/5.0",
}

# 원본 rsltList JSON 필드 → 정리된 컬럼명
_FIELD_MAP = {
    "itemIdnfNo": "물품식별번호",
    "itemIndfNmView": "품목명",
    "dtlsPrnm": "세부품명",
    "dtlsPrnmNo": "세부품명번호",
    "itemClsfNo": "물품분류번호",
    "ctrtUprc": "계약단가",           # 아래에서 숫자로 덮어씀
    "ctrtUntVal": "계약단위",
    "masCtrtYn": "MAS여부",           # Y=다수공급자계약
    "shopCtrtTyNm": "계약구분",        # '다수공급자계약' / '제3자단가계약' 등 (ground truth)
    "ctrtNo": "계약번호",
    "ctrtChgOrd": "계약변경차수",
    "lastCtrtYn": "최종계약여부",
    "ctrtBgngYmd": "계약시작일",
    "ctrtEndYmd": "계약종료일",
    "ctentUntyGrpNm": "계약업체",
    "mnftrEtpsNm": "제조업체",
    "bzmnRegNo": "사업자등록번호",
    "spplRgnNm": "공급지역",
    "pdctAtrbCdDtlNm": "규격상세",     # 가로$세로$두께$재질$... 형태
    "oncMaxDlreqAmt": "1회최대납품요구액",
    "entFormSeNm": "기업규모",
    "imgSrc": "이미지",
}


def _normalize(row):
    """원본 rsltList 항목 → 정리된 dict. 계약단가는 정수로."""
    out = {clean: (html.unescape(v) if isinstance(v := row.get(raw), str) else v)
           for raw, clean in _FIELD_MAP.items()}
    try:
        out["계약단가"] = int(float(row.get("ctrtUprc") or 0))
    except (TypeError, ValueError):
        out["계약단가"] = None
    return out


def _payload(keyword, page, page_size=PAGE_SIZE, start_prce="", end_prce="", etps_nm=""):
    return {"searchVO": {
        "tabDiv": "", "target": "계300001,계300002,계309999",
        "searchKeyword": keyword, "etpsNm": etps_nm, "prchsMthdSeCd": "본010001",
        "sortCndt": "SRCH_UPRC_ASC", "recordCountPerPage": str(page_size),
        "currentPage": page, "srchSeCd": "검030006", "rdoIndex": 1,
        "dgtlSrvcMallYn": "N",
        # 가격 구간 필터 (전체 카탈로그 샤딩용). 빈 문자열이면 무제한.
        "startPrce": "" if start_prce == "" else str(start_prce),
        "endPrce": "" if end_prce == "" else str(end_prce),
    }}


def fetch_page(page, *, keyword="", page_size=PAGE_SIZE, start_prce="", end_prce="",
               etps_nm="", session=None, timeout=60):
    """한 페이지의 원본 rsltList와 총건수를 반환한다. → (raw_rows, totCnt)"""
    s = session or requests.Session()
    r = s.post(SEARCH_URL, json=_payload(keyword, page, page_size, start_prce, end_prce, etps_nm),
               headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    rows = r.json().get("rsltList") or []
    tot = int(rows[0].get("totCnt") or 0) if rows else 0
    return rows, tot


def company_catalog(company, *, max_items=None, session=None, delay=0.4):
    """업체명(etpsNm)으로 해당 업체 전 품목 원본 rows 수집. (우리 카탈로그를 나라장터에서 직접)"""
    s = session or requests.Session()
    rows, page, total = [], 1, None
    while True:
        batch, tot = fetch_page(page, etps_nm=company, session=s)
        if not batch:
            break
        if total is None:
            total = tot
        rows.extend(batch)
        if max_items and len(rows) >= max_items:
            return rows[:max_items]
        if len(rows) >= total or len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(delay)
    return rows


def search(keyword, *, max_items=None, only_last_contract=True,
           contract_type=None, delay=0.6, session=None):
    """
    키워드로 종합쇼핑몰 전 품목을 페이징 수집한다.

    max_items          : 수집 상한(필터 전 원본 기준). None이면 전건(~2초/100건).
    only_last_contract : 최종계약건(lastCtrtYn=Y)만 남긴다. '계약 건이 달라선 안 됨' 대응.
    contract_type      : 'MAS' | '3자단가' | None(전체). MAS여부 기준으로 필터.
    반환               : 정리된 dict 리스트.
    """
    s = session or requests.Session()
    rows, page, total = [], 1, None
    while True:
        batch, tot = fetch_page(page, keyword=keyword, session=s)
        if not batch:
            break
        if total is None:
            total = tot
        rows.extend(_normalize(x) for x in batch)
        if max_items and len(rows) >= max_items:
            rows = rows[:max_items]
            break
        if len(rows) >= total or len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(delay)  # ponytail: 고정 지연. 막히면 늘려라 (실측상 미차단).

    if only_last_contract:
        rows = [x for x in rows if x.get("최종계약여부") == "Y"]
    if contract_type == "MAS":
        rows = [x for x in rows if x.get("MAS여부") == "Y"]
    elif contract_type in ("3자단가", "제3자단가", "3자"):
        rows = [x for x in rows if x.get("MAS여부") != "Y"]
    return rows


def to_xlsx(rows, path):
    """정리된 행을 엑셀로 저장. 첫 행의 키를 헤더로."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "종합쇼핑몰"
    if rows:
        cols = list(rows[0].keys())
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c) for c in cols])
    wb.save(path)
    return path
