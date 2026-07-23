"""
종합쇼핑몰 전체 카탈로그(~104만 건) 덤프 → SQLite.

문제: 그냥 1→10,381페이지로 훑으면 deep-offset 때문에 뒤 페이지가 8초+로 느려짐(실측).
해법: 가격 구간으로 샤딩해 각 구간을 항상 얕은 offset으로만 훑는다.
      구간 건수를 보고 SAFE(9000)를 넘으면 반으로 쪼갠다(적응형).
      페이지들은 ThreadPoolExecutor로 동시 요청(실측: 동시 8개 모두 200, 8p 3.7초).
      SQLite에 저장 → PK로 중복 자동제거 + 가격윈도우 단위로 재개(resume).

사용:
    python crawl_all.py                      # shop_all.db 에 전체 덤프
    python crawl_all.py --db out.db --workers 8 --safe 9000
    (중단해도 다시 실행하면 완료된 윈도우는 건너뛰고 이어서 함)

내보내기(완료 후):
    sqlite3 -header -csv shop_all.db "SELECT data FROM items" > out.csv   # data는 JSON 1열
    또는 python -c "import export_csv" 식으로 별도 처리 (README 참고)

ponytail: 단일 가격 하나에 SAFE 넘게 몰리면(예: 정확히 100만원 상품 수만개) 그 윈도우만
          deep-page로 훑음 — 가격으론 더 못 쪼갬. 동시요청이라 견딜 만함. 카탈로그는
          실행 중 바뀔 수 있음(1회 스냅샷).
"""
import argparse
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import g2b_client as g2b

SAFE = 9000                    # 윈도우당 상한(≈90페이지). 넘으면 반으로 분할.
PRICE_MAX = 300_000_000_000    # 관측 최고가 2000억 위. 닫힌 구간으로 전 범위 커버.
ID_KEYS = ("ctrtItemMngNo", "dqId", "itemIdnfNo")  # 행 고유키 우선순위

_local = threading.local()


def _session():
    s = getattr(_local, "s", None)
    if s is None:
        s = _local.s = requests.Session()
    return s


def _row_id(x):
    for k in ID_KEYS:
        v = x.get(k)
        if v:
            return str(v)
    return json.dumps(x, sort_keys=True, ensure_ascii=False)  # 최후의 수단


def count(session, lo, hi):
    """가격구간 [lo, hi]의 총건수."""
    _, tot = g2b.fetch_page(1, page_size=1, start_prce=lo, end_prce=hi, session=session)
    return tot


def plan_windows(session, lo, hi, out):
    """[lo,hi]를 건수 SAFE 이하 리프 구간들로 적응 분할해 out에 (lo,hi,tot)로 append."""
    tot = count(session, lo, hi)
    if tot == 0:
        return
    if tot <= SAFE or lo >= hi:
        out.append((lo, hi, tot))
        return
    mid = (lo + hi) // 2
    plan_windows(session, lo, mid, out)
    plan_windows(session, mid + 1, hi, out)


def _fetch(lo, hi, page):
    raw, _ = g2b.fetch_page(page, page_size=100, start_prce=lo, end_prce=hi, session=_session())
    return [(_row_id(x), json.dumps(g2b._normalize(x), ensure_ascii=False)) for x in raw]


def main():
    global SAFE
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="shop_all.db")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--safe", type=int, default=SAFE)
    args = ap.parse_args()
    SAFE = args.safe

    con = sqlite3.connect(args.db)
    con.execute("CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, data TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS done(win TEXT PRIMARY KEY)")
    con.commit()
    done = {r[0] for r in con.execute("SELECT win FROM done")}

    print("가격 윈도우 계획 중...")
    leaves = []
    plan_windows(requests.Session(), 0, PRICE_MAX, leaves)
    est = sum(t for _, _, t in leaves)
    print(f"윈도우 {len(leaves)}개, 예상 ~{est:,}건 (완료 {len(done)}개 건너뜀)")

    pool = ThreadPoolExecutor(max_workers=args.workers)
    t0 = time.time()
    for lo, hi, tot in leaves:
        win = f"{lo}-{hi}"
        if win in done:
            continue
        pages = (tot + 99) // 100
        futs = [pool.submit(_fetch, lo, hi, p) for p in range(1, pages + 1)]
        n = 0
        for f in as_completed(futs):
            rows = f.result()
            if rows:
                con.executemany("INSERT OR IGNORE INTO items(id, data) VALUES(?, ?)", rows)
                n += len(rows)
        con.execute("INSERT OR IGNORE INTO done(win) VALUES(?)", (win,))
        con.commit()
        uniq = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        print(f"[{int(time.time() - t0)}s] {win}: +{n} | 누적 unique {uniq:,}")

    total = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"완료. unique items = {total:,}  →  {args.db}")


if __name__ == "__main__":
    main()
