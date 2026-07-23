"""
crawl_all.py가 만든 shop_all.db(SQLite) → Parquet 내보내기.

items(id, data) 테이블의 JSON을 컬럼으로 펼쳐 zstd Parquet로 저장한다.
DuckDB/pandas/Polars에서 바로 쿼리 가능. 100만 행을 배치로 스트리밍(메모리 상한).

    .venv/bin/python export_parquet.py --db shop_all.db --out shop_all.parquet
"""
import argparse
import json
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq

import g2b_client as g2b

# 정리된 컬럼 (계약단가만 정수, 나머지 문자열)
_STR_COLS = [c for c in g2b._FIELD_MAP.values() if c != "계약단가"]
SCHEMA = pa.schema([(c, pa.string()) for c in _STR_COLS] + [("계약단가", pa.int64())])
BATCH = 50_000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="shop_all.db")
    ap.add_argument("--out", default="shop_all.parquet")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    total = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"{total:,}건 → {a.out}")

    writer = pq.ParquetWriter(a.out, SCHEMA, compression="zstd")
    buf, done = [], 0
    for (data,) in con.execute("SELECT data FROM items"):
        buf.append(json.loads(data))
        if len(buf) >= BATCH:
            writer.write_table(pa.Table.from_pylist(buf, schema=SCHEMA))
            done += len(buf); buf = []
            print(f"  {done:,} / {total:,}")
    if buf:
        writer.write_table(pa.Table.from_pylist(buf, schema=SCHEMA))
        done += len(buf)
    writer.close()
    con.close()
    print(f"완료: {done:,}행, {len(SCHEMA.names)}열 → {a.out}")
    print(f'DuckDB 예)  .venv/bin/python -c "import duckdb;'
          f"print(duckdb.sql(\\\"SELECT 계약구분,COUNT(*) c FROM '{a.out}' GROUP BY 1 ORDER BY c DESC\\\"))\"")


if __name__ == "__main__":
    main()
