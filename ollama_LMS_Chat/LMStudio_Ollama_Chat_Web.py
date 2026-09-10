# -*- coding: utf-8 -*-
import json, threading, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import requests

HOST='127.0.0.1'
PORT=8765

HTML = r'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>LM Studio / Ollama Chat</title>
<style>
body{font-family:Arial,"Malgun Gothic",sans-serif;background:#f4f6f8;margin:0}
.wrap{max-width:1100px;margin:24px auto;padding:0 16px}
.card{background:white;border:1px solid #ddd;border-radius:12px;padding:16px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:120px 1fr 100px 1fr;gap:10px;align-items:center}
input,select,textarea,button{font-size:15px}
input,select,textarea{width:100%;box-sizing:border-box;padding:9px;border:1px solid #bbb;border-radius:8px}
button{padding:10px 14px;border:0;border-radius:8px;background:#1769e0;color:white;cursor:pointer}
.chat{height:380px;overflow:auto;background:#fafafa;border:1px solid #ddd;border-radius:8px;padding:12px;white-space:pre-wrap}
.user{font-weight:bold;color:#1459b8}.ai{font-weight:bold;color:#16834b}
.row{display:flex;gap:8px;margin-top:10px}
</style>
</head>
<body>
<div class="wrap">
<div class="card">
<h2>LM Studio / Ollama Chat</h2>
<div class="grid">
<label>서버 종류</label>
<select id="backend"><option>LM Studio</option><option>Ollama</option></select>
<label>서버 주소</label><input id="server" value="http://127.0.0.1:1234">
<label>API Key</label><input id="apikey" type="password">
<label>상태</label><span id="status">연결 대기</span>
<label>모델</label><select id="model"></select>
<label>Temperature</label><input id="temp" type="number" value="0.7" step="0.1" min="0" max="2">
</div>
<div class="row">
<button onclick="loadModels()">모델 조회 및 연결</button>
<button onclick="clearChat()" style="background:#555">대화 초기화</button>
</div>
</div>

<div class="card">
<label><b>시스템 프롬프트</b></label>
<input id="system" value="당신은 친절하고 정확한 AI 도우미입니다.">
</div>

<div class="card"><div class="chat" id="chat"></div></div>

<div class="card">
<textarea id="prompt" rows="5" placeholder="질문을 입력하세요. Ctrl+Enter로 전송할 수 있습니다."></textarea>
<div class="row"><button id="sendBtn" onclick="sendMessage()">전송</button></div>
</div>
</div>

<script>
let history=[];
const backend=document.getElementById('backend');

backend.addEventListener('change',()=>{
  document.getElementById('server').value =
    backend.value==='LM Studio' ? 'http://127.0.0.1:1234' : 'http://127.0.0.1:11434';
  document.getElementById('model').innerHTML='';
  history=[];
  document.getElementById('status').textContent='연결 대기';
});

document.getElementById('prompt').addEventListener('keydown',e=>{
  if(e.ctrlKey && e.key==='Enter'){e.preventDefault();sendMessage();}
});

function cfg(){
  return {
    backend:backend.value,
    server:document.getElementById('server').value.trim(),
    api_key:document.getElementById('apikey').value.trim(),
    model:document.getElementById('model').value,
    temperature:parseFloat(document.getElementById('temp').value||'0.7'),
    system:document.getElementById('system').value.trim()
  };
}

function addMessage(who,text,cls){
  const chat=document.getElementById('chat');
  const wrap=document.createElement('div');
  wrap.style.marginBottom='14px';
  const label=document.createElement('div');
  label.className=cls;
  label.textContent='['+who+']';
  const body=document.createElement('div');
  body.textContent=text;
  wrap.appendChild(label);wrap.appendChild(body);
  chat.appendChild(wrap);chat.scrollTop=chat.scrollHeight;
  return body;
}

async function loadModels(){
  const st=document.getElementById('status');st.textContent='연결 중...';
  try{
    const r=await fetch('/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg())});
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||'연결 실패');
    const sel=document.getElementById('model');sel.innerHTML='';
    d.models.forEach(m=>{const o=document.createElement('option');o.value=m;o.textContent=m;sel.appendChild(o);});
    st.textContent='연결됨 / 모델 '+d.models.length+'개';
  }catch(e){st.textContent='오류';alert(e.message);}
}

async function sendMessage(){
  const p=document.getElementById('prompt');
  const text=p.value.trim();
  if(!text)return;
  if(!document.getElementById('model').value){alert("먼저 모델 조회 및 연결을 실행하세요.");return;}
  p.value='';
  addMessage('사용자',text,'user');
  const ai=addMessage('LLM','','ai');
  history.push({role:'user',content:text});
  document.getElementById('status').textContent='응답 생성 중...';
  document.getElementById('sendBtn').disabled=true;

  const payload=cfg();payload.history=history;
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(!r.ok)throw new Error(await r.text());
    const reader=r.body.getReader();const dec=new TextDecoder('utf-8');
    let answer='';
    while(true){
      const {value,done}=await reader.read();
      if(done)break;
      answer+=dec.decode(value,{stream:true});
      ai.textContent=answer;
      document.getElementById('chat').scrollTop=document.getElementById('chat').scrollHeight;
    }
    answer+=dec.decode();
    ai.textContent=answer;
    history.push({role:'assistant',content:answer});
    document.getElementById('status').textContent='연결됨';
  }catch(e){ai.textContent='[오류] '+e.message;document.getElementById('status').textContent='오류';}
  finally{document.getElementById('sendBtn').disabled=false;}
}

function clearChat(){
  history=[];
  document.getElementById('chat').innerHTML='';
  document.getElementById('status').textContent='대화 초기화';
}
</script>
</body></html>'''

def lm_headers(key):
    h={'Content-Type':'application/json'}
    if key:
        h['Authorization']='Bearer '+key
    return h

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_data(self,status,data,ctype='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type',ctype)
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path=='/' or self.path.startswith('/?'):
            self.send_data(200,HTML.encode('utf-8'),'text/html; charset=utf-8')
        else:
            self.send_data(404,b'Not Found','text/plain; charset=utf-8')

    def read_json(self):
        n=int(self.headers.get('Content-Length','0'))
        return json.loads(self.rfile.read(n).decode('utf-8'))

    def do_POST(self):
        try:
            data=self.read_json()
            if self.path=='/models':
                self.models(data)
            elif self.path=='/chat':
                self.chat(data)
            else:
                self.send_data(404,b'{"error":"Not Found"}')
        except Exception as e:
            self.send_data(500,json.dumps({'error':str(e)},ensure_ascii=False).encode('utf-8'))

    def models(self,data):
        backend=data.get('backend')
        server=data.get('server','').rstrip('/')
        key=data.get('api_key','')
        if backend=='LM Studio':
            r=requests.get(server+'/v1/models',headers=lm_headers(key),timeout=10)
            r.raise_for_status()
            r.encoding='utf-8'
            models=[x.get('id') for x in r.json().get('data',[]) if x.get('id')]
        else:
            r=requests.get(server+'/api/tags',timeout=10)
            r.raise_for_status()
            r.encoding='utf-8'
            models=[x.get('name') for x in r.json().get('models',[]) if x.get('name')]
        self.send_data(200,json.dumps({'models':models},ensure_ascii=False).encode('utf-8'))

    def chat(self,data):
        backend=data.get('backend')
        server=data.get('server','').rstrip('/')
        key=data.get('api_key','')
        model=data.get('model')
        temp=data.get('temperature',0.7)
        system=data.get('system','')
        history=data.get('history',[])

        messages=[]
        if system:
            messages.append({'role':'system','content':system})
        messages.extend(history)

        if backend=='LM Studio':
            body={'model':model,'messages':messages,'temperature':temp,'stream':True}
            r=requests.post(server+'/v1/chat/completions',headers=lm_headers(key),json=body,stream=True,timeout=(10,600))
            r.raise_for_status()
            r.encoding='utf-8'

            self.send_response(200)
            self.send_header('Content-Type','text/plain; charset=utf-8')
            self.send_header('Cache-Control','no-cache')
            self.end_headers()

            try:
                for raw_line in r.iter_lines(decode_unicode=False):
                    if not raw_line:
                        continue
                    line=raw_line.decode('utf-8',errors='replace').strip()
                    if not line.startswith('data:'):
                        continue
                    p=line[5:].strip()
                    if p=='[DONE]':
                        break
                    try:
                        delta=json.loads(p).get('choices',[{}])[0].get('delta',{})
                        tok=delta.get('content') or delta.get('reasoning_content') or ''
                        if tok:
                            self.wfile.write(tok.encode('utf-8'))
                            self.wfile.flush()
                    except Exception:
                        pass
            finally:
                r.close()
        else:
            body={'model':model,'messages':messages,'stream':True,'options':{'temperature':temp}}
            r=requests.post(server+'/api/chat',json=body,stream=True,timeout=(10,600))
            r.raise_for_status()
            r.encoding='utf-8'

            self.send_response(200)
            self.send_header('Content-Type','text/plain; charset=utf-8')
            self.send_header('Cache-Control','no-cache')
            self.end_headers()

            try:
                for raw_line in r.iter_lines(decode_unicode=False):
                    if not raw_line:
                        continue
                    line=raw_line.decode('utf-8',errors='replace').strip()
                    try:
                        o=json.loads(line)
                        tok=(o.get('message') or {}).get('content','')
                        if tok:
                            self.wfile.write(tok.encode('utf-8'))
                            self.wfile.flush()
                        if o.get('done'):
                            break
                    except Exception:
                        pass
            finally:
                r.close()

def open_browser():
    webbrowser.open(f'http://{HOST}:{PORT}/')

if __name__=='__main__':
    print('LM Studio / Ollama Chat')
    print(f'브라우저 주소: http://{HOST}:{PORT}')
    print('종료: Ctrl+C')
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    threading.Timer(0.8,open_browser).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
