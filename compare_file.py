"""
제안업체 품목등록결과(.xls) → 각 품목 나라장터 '동일단가' 경쟁사 매칭 → 가격비교표(업체별 시트).

format_convert.read_items 로 .xls를 읽고 → matcher(동일단가) 로 경쟁사를 찾아 →
build_report.build_multi 로 업체별 시트 비교표(경쟁사 이미지 임베드)를 만든다.

    python compare_file.py "입력.xls"
    python compare_file.py "입력.xls" --out 가격비교표.xlsx --top 3 --limit 5
"""
import argparse

import build_report
import format_convert


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="품목등록결과 .xls 경로")
    ap.add_argument("--out", default="가격비교표.xlsx")
    ap.add_argument("--top", type=int, default=3, help="업체당 경쟁사 후보 수")
    ap.add_argument("--limit", type=int, default=0, help="시트당 품목 수 상한(0=전체)")
    a = ap.parse_args()

    companies = format_convert.read_items(a.input)
    if a.limit:
        companies = {k: v[:a.limit] for k, v in companies.items()}
    total = sum(len(v) for v in companies.values())
    print(f"{len(companies)}개 업체 / {total}개 품목 → 동일단가 경쟁사 매칭 "
          f"(입력 {len(companies)}개사 = 계열사 상호 제외)")

    # 입력 파일의 모든 업체(계열사)를 서로 경쟁 풀에서 제외
    build_report.build_multi(companies, a.out, top_n=a.top, exact_price=True,
                             exclude_companies=list(companies))
    print("저장:", a.out)


if __name__ == "__main__":
    main()
