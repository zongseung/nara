"""
지피코리아 전 품목 배치 매칭 + 비교표 일괄 생성.

우리 품목 엑셀이 없어도 됨 — 지피코리아도 나라장터 등록업체라 업체명(etpsNm) 검색으로
전 품목(129건)을 직접 끌어와 '우리 품목'으로 삼는다. 각 품목마다 유사 경쟁사 후보를
뽑고(자기 업체 제외) 한 장의 비교표 엑셀로 출력.

    .venv/bin/python batch_match.py                 # 전체(129) → 지피코리아_비교표.xlsx
    .venv/bin/python batch_match.py --limit 10      # 앞 10개만(빠른 확인)
"""
import argparse

import requests
import g2b_client as g2b
import matcher
import build_report

COMPANY = "주식회사 지피코리아"
SHORT = "지피코리아"


def to_our(raw):
    """나라장터 원본 row → build_report가 쓰는 '우리 품목' dict."""
    e = matcher.extract(raw)
    return {"품명": e["품명"] or "품목", "규격": e["규격"], "재질": e["재질"],
            "용도": e["용도"], "가격": e["가격"], "모델": e["모델"], "식별번호": e["식별번호"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="지피코리아_비교표.xlsx")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)   # 0 = 전체
    args = ap.parse_args()

    s = requests.Session()
    print(f"{SHORT} 카탈로그 수집 중...")
    raws = g2b.company_catalog(COMPANY, session=s, max_items=args.limit or None)
    ours = [to_our(r) for r in raws]
    print(f"{len(ours)}개 품목 → 배치 매칭 + 비교표 생성 (자기 업체 제외)")

    build_report.build(ours, args.out, top_n=args.top,
                       our_company=COMPANY, exclude_company=SHORT)
    print("저장:", args.out)


if __name__ == "__main__":
    main()
