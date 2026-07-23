"""
로컬 SQLite로 /match 엔드포인트 라이브 데모 (MySQL 불필요).

app.py의 실제 /match 핸들러를 FastAPI TestClient(ASGI)로 그대로 호출한다.
  1) SQLite 테이블 생성 + 지피코리아 시드 적재
  2) 실제 app 임포트(같은 sqlite 파일에 aiosqlite로 연결)
  3) POST /match → 응답 JSON 출력
  4) DB에 저장된 매칭/이미지 행 + 스토리지 파일 확인

    .venv/bin/python demo_match.py
"""
import os
import sqlite3

os.environ["DB_URL"] = "sqlite+aiosqlite:///./demo.db"
os.environ["STORAGE_DIR"] = "./storage/images"
os.environ["PUBLIC_BASE"] = "/static/images"

DDL = """
CREATE TABLE our_product(
  product_id TEXT PRIMARY KEY, item_idnf_no TEXT, name TEXT NOT NULL, dtls_prnm_no TEXT,
  spec_w INTEGER, spec_h INTEGER, spec_t REAL, material TEXT, usage_desc TEXT, model TEXT, our_price INTEGER);
CREATE TABLE competitor_match(
  match_id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, run_id TEXT, rank_no INTEGER, score REAL,
  comp_company TEXT, comp_item_idnf TEXT, comp_model TEXT, comp_spec_w INTEGER, comp_spec_h INTEGER,
  comp_material TEXT, comp_usage TEXT, comp_price INTEGER, contract_type TEXT, src_img_url TEXT);
CREATE TABLE product_image(
  image_id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, storage_key TEXT, our_url TEXT, bytes INTEGER);
"""

SEED = [
    ("GK-BF02", "26187495", "안내판", None, 500, 450, None, "스테인리스",
     "시각장애인용점자안내", "GK-BF02", 1_300_000),
    ("GK-BF05", "26224093", "안내판", None, 500, 450, None, "스테인리스",
     "시각장애인용점자/공원안내", "GK-BF05", 5_200_000),
]


def bootstrap():
    if os.path.exists("demo.db"):
        os.remove("demo.db")
    con = sqlite3.connect("demo.db")
    con.executescript(DDL)
    con.executemany(
        "INSERT INTO our_product(product_id,item_idnf_no,name,dtls_prnm_no,spec_w,spec_h,"
        "spec_t,material,usage_desc,model,our_price) VALUES(?,?,?,?,?,?,?,?,?,?,?)", SEED)
    con.commit()
    con.close()
    print("SQLite 부트스트랩: 테이블 3개 + 지피코리아 2건 시드")


def main():
    bootstrap()
    from fastapi.testclient import TestClient
    import app  # 실제 엔드포인트 모듈 (env 세팅 후 임포트)

    client = TestClient(app.app)
    print("\nPOST /match  { product_id: 'GK-BF02', keyword: '점자 안내판' }")
    r = client.post("/match", json={"product_id": "GK-BF02", "top_n": 3, "keyword": "점자 안내판"})
    print("HTTP", r.status_code)
    data = r.json()
    print(f"run_id: {data['run_id']}")
    for c in data["candidates"]:
        print(f"  #{c['rank']} {c['score']:.2f} | {c['company']} | {c['price']:,} | "
              f"{c['spec']} | {c['material']} | {c['model']} | {c['contract_type']}")
        print(f"      image_url: {c['image_url']}")

    print("\n--- DB 저장 확인 (competitor_match / product_image) ---")
    con = sqlite3.connect("demo.db")
    for row in con.execute("SELECT rank_no,comp_company,comp_price,contract_type FROM competitor_match ORDER BY rank_no"):
        print("  competitor_match:", row)
    for row in con.execute("SELECT storage_key,our_url,bytes FROM product_image"):
        print("  product_image:", row)
    con.close()

    print("\n--- 스토리지 B 파일 확인 ---")
    d = os.environ["STORAGE_DIR"]
    files = os.listdir(d) if os.path.isdir(d) else []
    print(f"  {d}: {len(files)}개 파일", files[:3], "..." if len(files) > 3 else "")


if __name__ == "__main__":
    main()
