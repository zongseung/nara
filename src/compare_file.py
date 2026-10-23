"""
업체 조회 또는 품목등록결과(.xls) → 구성·규격을 대조한 경쟁사 매칭 → 가격비교표(업체별 시트).

format_convert.read_items 로 .xls를 읽거나 업체 품목을 조회하고 → matcher로 경쟁사를 찾아 →
build_report.build_multi 로 업체별 시트 비교표(경쟁사 이미지 임베드)를 만든다.

    python compare_file.py "입력.xls"
    python compare_file.py "입력.xls" --out 가격비교표.xlsx --top 3 --limit 5
"""
import argparse
import logging
import sqlite3

from core.catalog_sources import SearchQuery, SourceError
from core.legacy_matching import _norm_company, extract
from src import build_report, format_convert

logger = logging.getLogger(__name__)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", help="품목등록결과 .xls 경로 (--company와 택일)")
    ap.add_argument("--company", action="append", default=[], help="실시간 조회할 업체명. 여러 업체는 옵션 반복")
    ap.add_argument("--keyword", default="", help="업체 품목 조회를 제한할 키워드")
    ap.add_argument("--out", default="가격비교표.xlsx", help="출력 .xlsx 경로 (상위 폴더 자동 생성, 기존 파일 덮어쓰기 금지)")
    ap.add_argument("--top", type=int, default=3, help="업체당 경쟁사 후보 수")
    ap.add_argument("--exact-price", action="store_true", help="같은 단가에서 검색 (규격 ±10%%·지지대 개수 일치 등 필수 검사 유지)")
    ap.add_argument("--limit", type=int, default=0, help="시트당 품목 수 상한(0=전체)")
    ap.add_argument("--fallback-band", type=float, default=None,
                    help="--exact-price 무매칭 시 ±비율 가격대 재검색 (기본 0.3)")
    ap.add_argument("--workers", type=int, default=8, help="동시 매칭 스레드 수")
    ap.add_argument("--catalog-db")
    ap.add_argument("--catalog-run", default="last-reconciled")
    ap.add_argument("--allow-incomplete", action="store_true")
    ap.add_argument("--mode", choices=("suggest", "shadow", "auto"), default="suggest")
    for name in ("purpose", "category", "catalog-manifest", "source-root", "policy-dir", "release", "ledger", "index-dir"):
        ap.add_argument("--" + name)
    a = ap.parse_args()
    diagnostic = a.catalog_db or a.catalog_run != "last-reconciled" or a.allow_incomplete
    if bool(a.input) == bool(a.company):
        ap.error("input 또는 --company 중 하나를 지정하세요")
    if a.company:
        normalized = [_norm_company(name) for name in a.company]
        if not all(normalized) or len(set(normalized)) != len(normalized):
            ap.error("--company는 비어 있거나 중복된 업체명을 사용할 수 없습니다")
        if a.mode != 'suggest' or diagnostic or a.catalog_manifest:
            ap.error("--company requires live suggest mode without catalog options")
        if a.limit > 10000:
            ap.error("--company --limit은 최대 10000입니다")
    elif a.keyword:
        ap.error("--keyword requires --company")
    if a.limit < 0 or a.workers < 1 or a.top < 1:
        ap.error("--limit은 0 이상, --workers와 --top은 1 이상이어야 합니다")
    if a.fallback_band is not None and not 0 <= a.fallback_band <= 1:
        ap.error("--fallback-band는 0 이상 1 이하여야 합니다")
    if a.mode == "suggest" and a.fallback_band and not a.exact_price and not a.catalog_db:
        ap.error("--fallback-band는 --exact-price와 함께 사용하세요")
    if diagnostic and (a.mode != "suggest" or a.catalog_manifest):
        ap.error("diagnostic catalog options are suggest-only and cannot mix with a manifest")
    if a.mode != "suggest":
        if a.fallback_band not in (None, 0):
            ap.error("shadow/auto require --fallback-band 0")
        required = ("purpose", "category", "catalog_manifest", "source_root", "policy_dir", "release", "ledger", "index_dir")
        missing = [name for name in required if not getattr(a, name)]
        if missing:
            ap.error("shadow/auto require: " + ", ".join(missing))
        from src.matching.runtime import run_workbook
        from src.updater.store import StoreError
        try:
            run_workbook(a)
        except StoreError as exc:
            ap.exit(2, f'error: {exc}\n')
        return
    if a.catalog_manifest:
        ap.error("--catalog-manifest requires shadow or auto")
    if a.fallback_band is None:
        a.fallback_band = .3 if a.exact_price or a.catalog_db else 0
    if not a.catalog_db and (a.catalog_run != "last-reconciled" or a.allow_incomplete):
        ap.error("--catalog-run/--allow-incomplete require --catalog-db")
    try:
        a.out = str(build_report.prepare_output(a.out))
    except (ValueError, OSError) as exc:
        ap.error(f"출력 경로 오류: {exc}")
    provider = None
    if a.catalog_db:
        from src.catalog_provider import SQLiteProvider
        provider = SQLiteProvider(a.catalog_db, a.catalog_run, allow_incomplete=a.allow_incomplete)

    if a.company:
        from src.live_collection import collect_live
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        companies = {}
        for requested in a.company:
            logger.info('업체 품목 조회중 | %s', requested)
            result = collect_live(SearchQuery(company=requested,keyword=a.keyword),
                                  limit=a.limit or 10000,workers=min(a.workers,4))
            if not a.limit and len(result.items) < result.matched_total:
                raise SourceError('company_limit_exceeded',f'{requested}: use --limit 10000 for a partial report')
            items = [extract(dict(item.raw)) for item in result.items]
            items = [item for item in items if _norm_company(item['업체']) == _norm_company(requested)]
            if not items:
                raise SourceError('company_not_found',requested)
            company = items[0]['업체']
            companies[company] = items
            logger.info('업체 품목 조회 완료 | %s | 비교 대상 %d건 (검색 결과 %d건)',
                         company,len(items),result.matched_total)
            if len(result.items) < result.matched_total:
                logger.info('부분 비교표 | --limit %d에 따라 수집 범위를 제한했습니다',a.limit)
    else:
        companies = format_convert.read_items(a.input)
        if a.limit:
            companies = {k: v[:a.limit] for k, v in companies.items()}
    total = sum(len(v) for v in companies.values())
    exact_price = a.exact_price or provider is not None
    policy = "동일단가" if exact_price else "재질·구성 비교, 작은 규격·높은 가격 우선"
    print(f"{len(companies)}개 업체 / {total}개 품목 → {policy} 경쟁사 매칭 "
          f"(계열사 상호 제외, 동시 {a.workers}, 무매칭 시 ±{a.fallback_band:.0%} 근사)", flush=True)

    # 입력 파일의 모든 업체(계열사)를 서로 경쟁 풀에서 제외
    build_report.build_multi(companies, a.out, top_n=a.top, exact_price=exact_price,
                             exclude_companies=list(companies),
                             fallback_band=a.fallback_band, workers=a.workers, provider=provider,
                             comparison=True)
    print("저장:", a.out)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, SourceError, sqlite3.Error) as exc:
        raise SystemExit(f"error: {exc}") from None
