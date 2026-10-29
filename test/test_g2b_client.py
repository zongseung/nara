"""
ponytail 자체검증: 실제 응답에서 캡처한 원본 레코드를 parse_search_page → extract로 검증한다.
필드 매핑(특히 계약구분/단가/세부품명)이 깨지면 여기서 바로 실패한다. 네트워크 불필요.
  실행:  python -m unittest test.test_g2b_client
"""
import unittest
from decimal import Decimal

from core.catalog_sources import get_profile
from core.legacy_matching import extract
from src.g2b_client import parse_search_page

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


class G2BClientTests(unittest.TestCase):
    def test_captured_row_maps_to_comparison_fields(self):
        page = parse_search_page({"totalSize": 12455, "rsltList": [SAMPLE]}, get_profile())
        item = extract(page.rows[0].raw)
        self.assertEqual(page.rows[0].contract_price_decimal, Decimal(17990))
        self.assertEqual((item["가격"], item["계약구분"], item["세부품명"], item["세부품명번호"],
                          item["업체"], item["식별번호"]),
                         (Decimal(17990), "다수공급자계약", "안내판걸이구", "5512190801",
                          "주식회사김스애드", "22757231"))


if __name__ == "__main__":
    unittest.main()
