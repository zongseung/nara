"""
나라장터 단가 비교 — 동적 검색 웹앱 (FastAPI).

키워드 / 계약구분(MAS·제3자단가) / 동일단가로 즉석 검색 → 경쟁사 전량 표시 → 엑셀 다운로드.
매 검색이 다름(동적), 결과는 3개 캡 없이 전부.

    .venv/bin/uvicorn webapp:app --host 0.0.0.0 --port 8000
"""
import io
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel

import g2b_client as g2b
import matcher

NARA = "https://shop.g2b.go.kr"
app = FastAPI(title="나라장터 단가 비교")


class Query(BaseModel):
    keyword: str
    contract: str = ""     # "", "MAS", "제3자단가"
    price: int = 0         # 0=무관, >0=동일단가
    limit: int = 300


def _search(q: Query):
    lo = hi = q.price if q.price else ""
    rows, page = [], 1
    while len(rows) < q.limit:
        batch, _ = g2b.fetch_page(page, keyword=q.keyword, start_prce=lo, end_prce=hi)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    out, seen = [], set()
    for x in rows:
        ct = x.get("shopCtrtTyNm") or ""
        if q.contract == "MAS" and x.get("masCtrtYn") != "Y":
            continue
        if q.contract == "제3자단가" and "제3자" not in ct:
            continue
        e = matcher.extract(x)
        d = e["규격"]
        out.append({
            "업체": e["업체"] or "", "세부품명": e["세부품명"] or "",
            "규격": (f"{d[0]}×{d[1]}mm" if d else ""), "재질": e["재질"] or "",
            "용도": e["용도"] or "", "단가": e["가격"] or 0, "계약구분": ct,
            "모델": e["모델"] or "", "식별번호": e["식별번호"] or "",
            "img": (NARA + e["imgSrc"]) if e.get("imgSrc") else "",
        })
        if len(out) >= q.limit:
            break
    return out


@app.post("/api/search")
async def api_search(q: Query):
    rows = await run_in_threadpool(_search, q)
    return {"count": len(rows), "rows": rows}


@app.post("/api/report")
async def api_report(q: Query):
    rows = await run_in_threadpool(_search, q)
    wb = Workbook(); ws = wb.active; ws.title = "동일단가경쟁사"
    cols = ["업체", "세부품명", "규격", "재질", "용도", "단가", "계약구분", "모델", "식별번호"]
    ws.append(cols)
    for r in rows:
        ws.append([r[c] for c in cols])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    fn = f"비교_{q.keyword or 'result'}.xlsx".replace(" ", "_")
    cd = "attachment; filename=result.xlsx; filename*=UTF-8''" + quote(fn)  # 한글 파일명 퍼센트 인코딩
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": cd})


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>나라장터 단가 비교</title>
<style>
  :root{--accent:#4263eb;--ink:#1e293b;--muted:#64748b;--border:#e6e9f0;--bg:#f4f6fb;--card:#fff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Malgun Gothic','Apple SD Gothic Neo',sans-serif}
  header{background:linear-gradient(120deg,#2b3a67,#4263eb);color:#fff;padding:22px 28px}
  header h1{margin:0;font-size:20px;font-weight:700;letter-spacing:-.3px}
  header p{margin:4px 0 0;font-size:13px;opacity:.85}
  .wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;
    box-shadow:0 1px 3px rgba(16,24,40,.04);padding:18px}
  .search{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:-32px;position:relative}
  .search input,.search select{height:46px;border:1px solid var(--border);border-radius:10px;
    padding:0 14px;font-size:15px;background:#fff;outline:none}
  .search input:focus,.search select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(66,99,235,.12)}
  #kw{flex:1;min-width:220px;font-weight:600}
  #price{width:190px}
  .btn{height:46px;padding:0 22px;border:0;border-radius:10px;background:var(--accent);color:#fff;
    font-size:15px;font-weight:700;cursor:pointer;transition:.15s}
  .btn:hover{background:#324ad0}
  .btn.ghost{background:#fff;color:var(--accent);border:1px solid var(--accent)}
  .btn.ghost:hover{background:#eef1fe}
  .bar{display:flex;align-items:center;justify-content:space-between;margin:22px 2px 12px}
  .count{font-size:14px;color:var(--muted)}
  .count b{color:var(--ink);font-size:16px}
  table{width:100%;border-collapse:collapse;background:#fff;border-radius:14px;overflow:hidden;
    border:1px solid var(--border);font-size:14px}
  th{background:#f8fafc;color:var(--muted);font-weight:600;text-align:left;padding:12px 14px;
    border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:11px 14px;border-bottom:1px solid #f1f4f9;vertical-align:middle}
  tr:hover td{background:#fafbff}
  td.price{font-weight:700;color:#111}
  .thumb{width:46px;height:46px;object-fit:cover;border-radius:8px;background:#f1f4f9;border:1px solid var(--border)}
  .tag{display:inline-block;font-size:12px;font-weight:600;padding:3px 9px;border-radius:20px}
  .tag.mas{background:#e7f0ff;color:#2352c9}.tag.third{background:#eafaf1;color:#12855a}
  .empty,.loading{text-align:center;color:var(--muted);padding:60px 0;font-size:15px}
  .spin{width:26px;height:26px;border:3px solid #dfe4ee;border-top-color:var(--accent);
    border-radius:50%;animation:s .8s linear infinite;margin:0 auto 12px}
  @keyframes s{to{transform:rotate(360deg)}}
</style></head>
<body>
<header><h1>나라장터 단가 비교</h1><p>종합쇼핑몰 동일단가 경쟁사 즉석 검색 · 엑셀 내보내기</p></header>
<div class="wrap">
  <div class="search card">
    <input id="kw" placeholder="품명·키워드 (예: 안내판)" onkeydown="if(event.key==='Enter')go()">
    <select id="ct">
      <option value="">계약구분 전체</option>
      <option value="MAS">다수공급자계약(MAS)</option>
      <option value="제3자단가">제3자단가계약</option>
    </select>
    <input id="price" type="number" placeholder="동일단가(원) — 비우면 전체">
    <button class="btn" onclick="go()">검색</button>
  </div>
  <div class="bar">
    <span class="count" id="count">키워드를 입력하고 검색하세요</span>
    <button class="btn ghost" id="dlbtn" onclick="dl()" style="display:none">엑셀로 저장</button>
  </div>
  <div id="out"><div class="empty" id="emptybox"></div></div>
</div>
<script>
function q(){return{keyword:kw.value.trim(),contract:ct.value,price:parseInt(price.value)||0}}
async function go(){
  if(!kw.value.trim())return;
  out.innerHTML='<div class="loading"><div class="spin"></div>검색 중…</div>';
  dlbtn.style.display='none';
  const r=await fetch('/api/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(q())});
  const d=await r.json();
  render(d);
}
function render(d){
  const p=parseInt(price.value)||0;
  count.innerHTML='검색결과 <b>'+d.count.toLocaleString()+'</b>곳'+(p?' · 동일단가 '+p.toLocaleString()+'원':'');
  if(!d.count){out.innerHTML='<div class="empty">조건에 맞는 경쟁사가 없습니다</div>';return}
  dlbtn.style.display='';
  let h='<table><thead><tr><th></th><th>업체</th><th>세부품명</th><th>규격</th><th>재질</th><th>용도</th><th>단가</th><th>계약구분</th></tr></thead><tbody>';
  for(const x of d.rows){
    const tag=x.계약구분.includes('제3자')?'<span class="tag third">3자단가</span>':(x.계약구분.includes('다수')?'<span class="tag mas">MAS</span>':x.계약구분);
    const img=x.img?'<img class="thumb" src="'+x.img+'" loading="lazy" onerror="this.style.visibility=\\'hidden\\'">':'<div class="thumb"></div>';
    h+='<tr><td>'+img+'</td><td>'+esc(x.업체)+'</td><td>'+esc(x.세부품명)+'</td><td>'+esc(x.규격)+'</td><td>'+esc(x.재질)+'</td><td>'+esc(x.용도)+'</td><td class="price">'+(x.단가||0).toLocaleString()+'</td><td>'+tag+'</td></tr>';
  }
  out.innerHTML=h+'</tbody></table>';
}
function esc(s){return(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function dl(){
  const r=await fetch('/api/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(q())});
  const b=await r.blob();const u=URL.createObjectURL(b);const a=document.createElement('a');
  a.href=u;a.download='비교_'+(kw.value.trim()||'result')+'.xlsx';a.click();URL.revokeObjectURL(u);
}
</script>
</body></html>"""
