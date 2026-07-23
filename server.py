"""
g2b-shop MCP 서버.

"안내판 중에 우리보다 비싼거 찾아줘" → 도구가 종합쇼핑몰 API를 페이징 수집 →
정리된 행 반환 / 엑셀 저장.

도구:
  - search_shop(keyword, contract_type, max_items)          정리된 상품 리스트(JSON)
  - export_shop_xlsx(keyword, out_path, ..., our_price)      엑셀로 저장(+우리단가 비교)

MCP 등록 (.mcp.json 또는 claude_desktop_config.json):
  "g2b-shop": { "command": "python", "args": ["/절대경로/g2b-shop-mcp/server.py"] }
"""
from mcp.server.fastmcp import FastMCP
import g2b_client as g2b

mcp = FastMCP("g2b-shop")


@mcp.tool()
def search_shop(keyword: str, contract_type: str = "", max_items: int = 0) -> list[dict]:
    """
    종합쇼핑몰에서 keyword로 상품을 검색해 정리된 행을 반환한다.
    contract_type: 'MAS' | '3자단가' | ''(전체).
    max_items    : 수집 상한. 0이면 전건(수천 건이면 수 분 소요).
    """
    return g2b.search(keyword,
                      max_items=max_items or None,
                      contract_type=contract_type or None)


@mcp.tool()
def export_shop_xlsx(keyword: str, out_path: str, contract_type: str = "",
                     max_items: int = 0, our_price: int = 0,
                     only_higher_than_ours: bool = False) -> str:
    """
    검색 결과를 엑셀 파일로 저장한다.
    our_price>0 이면 '우리단가'/'차액'/'우리보다비쌈' 컬럼을 붙인다.
    only_higher_than_ours=True 이면 우리보다 비싼 건만 저장한다.
    """
    rows = g2b.search(keyword, max_items=max_items or None,
                      contract_type=contract_type or None)
    if our_price:
        for r in rows:
            p = r.get("계약단가")
            r["우리단가"] = our_price
            r["차액"] = (p - our_price) if p is not None else None
            r["우리보다비쌈"] = "Y" if (p is not None and p > our_price) else "N"
        if only_higher_than_ours:
            rows = [r for r in rows if r.get("우리보다비쌈") == "Y"]
    g2b.to_xlsx(rows, out_path)
    return f"{len(rows)}건 저장 → {out_path}"


if __name__ == "__main__":
    mcp.run()
