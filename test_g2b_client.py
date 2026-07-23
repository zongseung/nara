"""
ponytail 자체검증: 실제 응답에서 캡처한 원본 레코드로 _normalize()를 검증한다.
필드 매핑(특히 MAS여부/계약구분/계약번호/단가)이 깨지면 여기서 바로 실패한다. 네트워크 불필요.
  실행:  python test_g2b_client.py
"""
import g2b_client as g2b

# shop.g2b.go.kr newShopUntySrchApi.do 응답 rsltList[0] 실측 발췌 (안내판 검색 첫 결과)
SAMPLE = {
    "itemIdnfNo": "22757231",
    "itemIndfNmView": "안내판걸이구 김스애드 kims-bs01 (부품)200×250×1.2mm",
    "dtlsPrnm": "안내판걸이구",
    "dtlsPrnmNo": "5512190801",
    "itemClsfNo": "55121908",
    "ctrtUprc": 17990,
    "ctrtUntVal": "개",
    "masCtrtYn": "Y",
    "shopCtrtTyNm": "다수공급자계약",
    "ctrtNo": "001880931_1",
    "ctrtChgOrd": "04",
    "lastCtrtYn": "Y",
    "ctrtBgngYmd": "20180425",
    "ctrtEndYmd": "20270424",
    "ctentUntyGrpNm": "주식회사 김스애드",
    "mnftrEtpsNm": "주식회사김스애드",
    "bzmnRegNo": "2048610496",
    "spplRgnNm": "전지역",
    "pdctAtrbCdDtlNm": "200$250$1.2$스테인리스강$결속부보강판",
    "entFormSeNm": "중소기업",
    "totCnt": 12455,
}


def test_normalize():
    r = g2b._normalize(SAMPLE)
    assert r["계약단가"] == 17990 and isinstance(r["계약단가"], int), r["계약단가"]
    assert r["MAS여부"] == "Y"
    assert r["계약구분"] == "다수공급자계약"      # MAS vs 3자단가 구분의 근거
    assert r["계약번호"] == "001880931_1"          # 계약 건 구분
    assert r["최종계약여부"] == "Y"
    assert r["세부품명"] == "안내판걸이구"
    assert r["세부품명번호"] == "5512190801"
    assert r["제조업체"] == "주식회사김스애드"


def test_price_coercion():
    assert g2b._normalize({"ctrtUprc": "17990.0"})["계약단가"] == 17990
    assert g2b._normalize({"ctrtUprc": None})["계약단가"] == 0
    assert g2b._normalize({})["계약단가"] == 0


if __name__ == "__main__":
    test_normalize()
    test_price_coercion()
    print("ok")
