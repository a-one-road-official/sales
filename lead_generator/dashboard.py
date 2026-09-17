"""Local operator UI. Bind to loopback; use an authenticated tunnel remotely."""
import argparse
import base64
import hmac
import json
import os
from http.server import HTTPServer,BaseHTTPRequestHandler
from .store import Store

HTML='''<!doctype html><html lang="ja"><meta charset="utf-8"><title>リスト収集の操作</title>
<style>body{font:18px system-ui;max-width:900px;margin:40px auto;padding:20px;background:#f3f5f7}button{padding:14px 30px;margin:12px;font-size:20px}pre{white-space:pre-wrap;background:white;padding:24px}</style>
<h1>リスト収集の操作</h1><p>START / STOP は処理要求です。実際の稼働状態と最終更新時刻を下で確認してください。</p>
<p>候補数と、条件を確認して両リストに追加できた件数を別々に表示します。</p>
<button onclick="command('START')">START</button><button onclick="command('STOP')">STOP</button>
<p id="message"></p><pre id="status">読み込み中</pre>
<script>async function refresh(){let r=await fetch('/status');document.getElementById('status').textContent=JSON.stringify(await r.json(),null,2)}
async function command(c){let r=await fetch('/command',{method:'POST',headers:{'Content-Type':'application/json','X-Leadgen-Control':'1'},body:JSON.stringify({command:c})});document.getElementById('message').textContent=r.ok?c+' を記録しました':'操作に失敗しました';refresh()}refresh();setInterval(refresh,5000)</script></html>'''


def main():
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--port',type=int,default=8765)
    a=p.parse_args();token=os.environ.get('LEADGEN_UI_PASSWORD','')
    if len(token)<20:raise SystemExit('Set LEADGEN_UI_PASSWORD to a unique password of at least 20 characters')
    expected='Basic '+base64.b64encode(('operator:'+token).encode()).decode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def allowed(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''),expected):
                self.send_response(401);self.send_header('WWW-Authenticate','Basic realm="Lead generator"');self.end_headers();return False
            return True
        def reply(self,body,kind='application/json',status=200):
            self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body.encode())
        def do_GET(self):
            if not self.allowed():return
            if self.path=='/':self.reply(HTML,'text/html')
            elif self.path=='/status':
                store=Store(a.db)
                try:self.reply(json.dumps(store.status(),ensure_ascii=False))
                finally:store.db.close()
            else:self.reply('{}',status=404)
        def do_POST(self):
            if not self.allowed():return
            if self.path!='/command' or self.headers.get('X-Leadgen-Control')!='1':return self.reply('{}',status=403)
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<1000:raise ValueError()
                command=json.loads(self.rfile.read(size))['command']
                if command not in ('START','STOP'):raise ValueError()
            except (ValueError,KeyError):return self.reply('{}',status=400)
            store=Store(a.db)
            try:
                store.set('command',command);store.event('CONTROL',{'command':command,'actor':'operator_ui'});self.reply('{"saved":true}')
            finally:store.db.close()
    HTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
if __name__=='__main__':main()
