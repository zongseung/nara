"""Local Docker-only controllable source fixture, with no live-source requests."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time
from pathlib import Path

STATE=Path('/fixture/state.json')


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        query=json.loads(self.rfile.read(int(self.headers['Content-Length'])))['searchVO']
        state=json.loads(STATE.read_text())
        time.sleep(state.get('delay_seconds',0))
        if query['currentPage']==2 and state.get('page2_failures',0)>0:
            state['page2_failures']-=1
            STATE.write_text(json.dumps(state))
            self.send_response(500)
            self.end_headers()
            return
        if state.get('failure'):
            self.send_response(500)
            self.end_headers()
            return
        row=dict(ctrtItemMngNo='fixture-A',itemIdnfNo='goods-A',itemIdnfNm='=1+1',itemIndfNmView='=1+1',
                 ctrtUprc=state['price'],ctrtUntVal='개',ctentUntyGrpNm=state.get('seller','Seller'),mnftrEtpsNm='Maker',
                 pdctAtrbCdDtlNm=state.get('spec','10mm'),itemClsfNo='123',rprsCtrtItemMngNo='',ctrtNo='C',
                 ctrtBgngYmd='20260101',ctrtEndYmd='20261231',totCnt=1)
        count=state.get('count',1)
        start=(int(query['currentPage'])-1)*int(query['recordCountPerPage'])
        rows=[dict(row,ctrtItemMngNo=row['ctrtItemMngNo'] if count==1 else 'fixture-'+str(i),totCnt=count) for i in range(start,min(count,start+int(query['recordCountPerPage'])))]
        body=json.dumps(dict(rsltList=rows,totalSize=count)).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args):
        return


if __name__=='__main__':
    HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
