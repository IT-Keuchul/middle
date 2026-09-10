# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31.0",
#     "psutil>=5.9.0",
#     "huggingface-hub>=0.27.0",
# ]
# ///

import os, sys, re, json, queue, shutil, tempfile, threading, subprocess, time
import importlib.util
from pathlib import Path

# --- 단일 파일 실행을 위한 필수 패키지 자동 설치 안전장치 ---
REQUIRED_PACKAGES = {
    "requests": "requests>=2.31.0",
    "psutil": "psutil>=5.9.0",
    "huggingface_hub": "huggingface-hub>=0.27.0",
}
_missing = [pkg for mod, pkg in REQUIRED_PACKAGES.items() if importlib.util.find_spec(mod) is None]
if _missing:
    print(f"[*] 필수 패키지 자동 설치 중: {', '.join(_missing)}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *_missing])

# --- PyInstaller 또는 부모 프로세스에 의한 Tcl/Tk 환경변수 오염 방지 ---
for _var in ("TCL_LIBRARY", "TK_LIBRARY"):
    _val = os.environ.get(_var)
    if _val and ("_MEI" in _val or not os.path.exists(_val)):
        del os.environ[_var]

_base_tcl = Path(sys.base_prefix) / "tcl"
if (_base_tcl / "tcl8.6").exists():
    os.environ["TCL_LIBRARY"] = str(_base_tcl / "tcl8.6")
if (_base_tcl / "tk8.6").exists():
    os.environ["TK_LIBRARY"] = str(_base_tcl / "tk8.6")

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import requests, psutil
from huggingface_hub import HfApi, hf_hub_download


LM_INSTALL = "irm https://lmstudio.ai/install.ps1 | iex"
OLLAMA_INSTALL = "irm https://ollama.com/install.ps1 | iex"
LM_API = "http://127.0.0.1:1234"
OLLAMA_API = "http://127.0.0.1:11434"
DOWNLOAD_ROOT = Path.home() / "LocalLLMModels"


def hidden_run(args, timeout=None):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=flags
    )


def exists(cmd):
    return shutil.which(cmd) is not None


def get_pe_version(file_path):
    if not file_path or os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        size = ctypes.windll.version.GetFileVersionInfoSizeW(file_path, None)
        if size == 0:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(file_path, 0, size, buf):
            return None
        lpffi = ctypes.c_void_p()
        u_len = ctypes.c_uint()
        if ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(lpffi), ctypes.byref(u_len)):
            class VS_FIXEDFILEINFO(ctypes.Structure):
                _fields_ = [
                    ("dwSignature", wintypes.DWORD),
                    ("dwStrucVersion", wintypes.DWORD),
                    ("dwFileVersionMS", wintypes.DWORD),
                    ("dwFileVersionLS", wintypes.DWORD),
                    ("dwProductVersionMS", wintypes.DWORD),
                    ("dwProductVersionLS", wintypes.DWORD),
                ]
            ffi = VS_FIXEDFILEINFO.from_address(lpffi.value)
            major = ffi.dwFileVersionMS >> 16
            minor = ffi.dwFileVersionMS & 0xFFFF
            patch = ffi.dwFileVersionLS >> 16
            return f"{major}.{minor}.{patch}"
    except Exception:
        return None
    return None



def nvidia_info():
    if not exists("nvidia-smi"):
        return []
    try:
        r = hidden_run([
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits"
        ], 8)
        rows = []
        for line in r.stdout.splitlines():
            p = [x.strip() for x in line.split(",", 1)]
            if len(p) == 2:
                rows.append((p[0], float(p[1]) / 1024))
        return rows
    except Exception:
        return []


def hardware():
    vm = psutil.virtual_memory()
    disk = shutil.disk_usage(str(Path.home()))
    gpus = nvidia_info()
    vram = max([x[1] for x in gpus], default=0)

    if vram >= 45: max_b = 70
    elif vram >= 22: max_b = 32
    elif vram >= 14: max_b = 14
    elif vram >= 7: max_b = 8
    elif vram >= 4: max_b = 4
    else:
        ram = vm.total / 1024**3
        max_b = 14 if ram >= 64 else 8 if ram >= 32 else 4 if ram >= 16 else 3

    return {
        "ram": vm.total / 1024**3,
        "free_ram": vm.available / 1024**3,
        "disk": disk.free / 1024**3,
        "gpus": gpus,
        "vram": vram,
        "max_b": max_b,
    }


def parse_b(name):
    m = re.findall(r'(?<!\d)(\d+(?:\.\d+)?)\s*[bB](?![A-Za-z])', name)
    return max(map(float, m)) if m else None


def qscore(name):
    n = name.lower()
    for key, s in [
        ("q4_k_m", 100), ("q5_k_m", 95), ("q4_k_s", 90),
        ("q5_k_s", 85), ("q6_k", 80), ("q3_k_m", 75),
        ("q8_0", 70), ("q2_k", 50), ("f16", 30), ("bf16", 28)
    ]:
        if key in n:
            return s
    return 40


class Manager:
    def __init__(self, root):
        self.root = root
        root.title("Local LLM Manager")
        root.geometry("1200x820")
        root.minsize(1000, 700)

        self.q = queue.Queue()
        self.hw = hardware()
        self.hf_data = []
        self.models = {"LM Studio": [], "Ollama": []}
        self.hist = {"LM Studio": [], "Ollama": []}

        self.install_backend = tk.StringVar(value="LM Studio")
        self.chat_backend = tk.StringVar(value="LM Studio")
        self.hf_query = tk.StringVar()
        self.hf_limit = tk.IntVar(value=12)
        self.download_dir = tk.StringVar(value=str(DOWNLOAD_ROOT))
        self.chat_model = tk.StringVar()
        self.temp = tk.DoubleVar(value=0.7)
        self.status = tk.StringVar(value="준비")

        self.build()
        root.after(100, self.events)
        self.refresh_status()
        self.refresh_models()

    def build(self):
        head = ttk.Frame(self.root)
        head.pack(fill="x", padx=10, pady=8)
        ttk.Label(head, text="Local LLM Manager", font=("", 16, "bold")).pack(side="left")
        ttk.Label(head, textvariable=self.status).pack(side="right")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=5)

        self.t1, self.t2, self.t3, self.t4, self.t5 = [ttk.Frame(nb) for _ in range(5)]
        for t, name in zip(
            [self.t1,self.t2,self.t3,self.t4,self.t5],
            ["1. 설치/업데이트","2. Hugging Face 모델","3. 설치된 모델","4. LLM 대화","로그"]
        ):
            nb.add(t, text=name)

        self.build_install()
        self.build_hf()
        self.build_models()
        self.build_chat()
        self.logbox = tk.Text(self.t5, wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=10, pady=10)

    def build_install(self):
        f = ttk.LabelFrame(self.t1, text="이 PC 사양")
        f.pack(fill="x", padx=10, pady=10)
        gpu = ", ".join(f"{n} ({v:.1f}GB)" for n,v in self.hw["gpus"]) or "NVIDIA GPU 미감지"
        txt = (
            f'RAM {self.hw["ram"]:.1f}GB / 사용 가능 {self.hw["free_ram"]:.1f}GB\n'
            f'GPU: {gpu}\n'
            f'홈 드라이브 여유 {self.hw["disk"]:.1f}GB / 추천 최대 규모 약 {self.hw["max_b"]}B(Q4 기준)'
        )
        ttk.Label(f, text=txt, justify="left").pack(anchor="w", padx=10, pady=10)

        g = ttk.LabelFrame(self.t1, text="런타임")
        g.pack(fill="x", padx=10, pady=6)

        self.runtime = ttk.Treeview(g, columns=("name","state","ver","api"), show="headings", height=3)
        for c,t,w in [
            ("name","런타임",220),("state","상태",120),("ver","버전",270),("api","기본 API",260)
        ]:
            self.runtime.heading(c,text=t); self.runtime.column(c,width=w,anchor="w")
        self.runtime.pack(fill="x", padx=8, pady=8)

        b = ttk.Frame(g); b.pack(fill="x", padx=8, pady=(0,8))
        ttk.Button(b,text="LM Studio/llmster 설치·업데이트",
                   command=lambda:self.install("LM Studio")).pack(side="left",padx=3)
        ttk.Button(b,text="Ollama 설치·업데이트",
                   command=lambda:self.install("Ollama")).pack(side="left",padx=3)
        ttk.Button(b,text="상태 새로고침",command=self.refresh_status).pack(side="left",padx=3)

        s = ttk.LabelFrame(self.t1, text="서버")
        s.pack(fill="x", padx=10, pady=6)
        ttk.Button(s,text="LM Studio 서버 시작",command=self.lm_start).pack(side="left",padx=8,pady=8)
        ttk.Button(s,text="LM Studio 서버 중지",command=self.lm_stop).pack(side="left",padx=4,pady=8)
        ttk.Button(s,text="Ollama API 확인",command=self.ollama_check).pack(side="left",padx=4,pady=8)

        ttk.Label(
            self.t1,
            text="※ 위 LM Studio PowerShell 명령은 공식 문서상 Desktop GUI가 아니라 llmster + lms CLI 설치 방식입니다."
        ).pack(anchor="w", padx=14, pady=8)

    def build_hf(self):
        f = ttk.LabelFrame(self.t2,text="Hugging Face GGUF 모델 검색")
        f.pack(fill="x", padx=10, pady=10)
        ttk.Label(f,text="검색어").grid(row=0,column=0,padx=5,pady=7)
        ttk.Entry(f,textvariable=self.hf_query,width=35).grid(row=0,column=1,padx=5,sticky="ew")
        ttk.Label(f,text="개수").grid(row=0,column=2,padx=5)
        ttk.Spinbox(f,from_=5,to=30,textvariable=self.hf_limit,width=6).grid(row=0,column=3)
        ttk.Button(f,text="PC 맞춤 추천",command=lambda:self.hf_search(True)).grid(row=0,column=4,padx=5)
        ttk.Button(f,text="검색",command=lambda:self.hf_search(False)).grid(row=0,column=5,padx=5)
        f.columnconfigure(1,weight=1)

        p = ttk.Frame(self.t2); p.pack(fill="x",padx=10)
        ttk.Label(p,text="다운로드 폴더").pack(side="left")
        ttk.Entry(p,textvariable=self.download_dir).pack(side="left",fill="x",expand=True,padx=5)
        ttk.Button(p,text="선택",command=self.pick_dir).pack(side="left")

        self.hftree = ttk.Treeview(
            self.t2,columns=("fit","name","b","down","like","files"),show="headings",height=17)
        for c,t,w in [
            ("fit","적합도",80),("name","모델",520),("b","규모",80),
            ("down","다운로드",100),("like","Likes",70),("files","GGUF",70)
        ]:
            self.hftree.heading(c,text=t); self.hftree.column(c,width=w,anchor="w")
        self.hftree.pack(fill="both",expand=True,padx=10,pady=8)

        b=ttk.Frame(self.t2); b.pack(fill="x",padx=10,pady=5)
        ttk.Label(b,text="등록 대상").pack(side="left")
        ttk.Combobox(b,textvariable=self.install_backend,values=["LM Studio","Ollama"],
                     state="readonly",width=14).pack(side="left",padx=5)
        ttk.Button(b,text="GGUF 후보 보기",command=self.show_files).pack(side="left",padx=4)
        ttk.Button(b,text="권장 GGUF 다운로드 + 등록",command=self.download).pack(side="left",padx=4)

    def build_models(self):
        top=ttk.Frame(self.t3); top.pack(fill="x",padx=10,pady=10)
        ttk.Button(top,text="새로고침",command=self.refresh_models).pack(side="left")
        ttk.Button(top,text="LM Studio 선택 모델 로드",command=self.load_lm).pack(side="left",padx=5)
        ttk.Button(top,text="LM Studio 전체 언로드",command=self.unload_lm).pack(side="left",padx=5)

        pane=ttk.Panedwindow(self.t3,orient="horizontal"); pane.pack(fill="both",expand=True,padx=10,pady=5)
        a=ttk.LabelFrame(pane,text="LM Studio"); b=ttk.LabelFrame(pane,text="Ollama")
        pane.add(a,weight=1); pane.add(b,weight=1)
        self.lmlist=tk.Listbox(a); self.lmlist.pack(fill="both",expand=True,padx=6,pady=6)
        self.olist=tk.Listbox(b); self.olist.pack(fill="both",expand=True,padx=6,pady=6)

    def build_chat(self):
        top=ttk.Frame(self.t4); top.pack(fill="x",padx=10,pady=10)
        ttk.Label(top,text="런타임").pack(side="left")
        cb=ttk.Combobox(top,textvariable=self.chat_backend,values=["LM Studio","Ollama"],
                        state="readonly",width=13)
        cb.pack(side="left",padx=5); cb.bind("<<ComboboxSelected>>",lambda e:self.chat_models())
        ttk.Label(top,text="모델").pack(side="left",padx=(10,0))
        self.modelcb=ttk.Combobox(top,textvariable=self.chat_model,state="readonly",width=48)
        self.modelcb.pack(side="left",fill="x",expand=True,padx=5)
        ttk.Label(top,text="Temperature").pack(side="left")
        ttk.Scale(top,from_=0,to=1.5,variable=self.temp,orient="horizontal",length=120).pack(side="left",padx=4)

        self.chatbox=tk.Text(self.t4,wrap="word",state="disabled")
        self.chatbox.pack(fill="both",expand=True,padx=10,pady=5)
        bot=ttk.Frame(self.t4); bot.pack(fill="x",padx=10,pady=10)
        self.input=tk.Text(bot,height=5,wrap="word"); self.input.pack(side="left",fill="both",expand=True)
        self.input.bind("<Control-Return>",lambda e:self.send())
        x=ttk.Frame(bot); x.pack(side="left",padx=(8,0))
        ttk.Button(x,text="전송\nCtrl+Enter",command=self.send).pack(fill="x")
        ttk.Button(x,text="대화 초기화",command=self.clear_chat).pack(fill="x",pady=5)

    def emit(self,kind,data):
        self.q.put((kind,data))

    def events(self):
        try:
            while True:
                k,d=self.q.get_nowait()
                if k=="status": self.status.set(d)
                elif k=="log":
                    self.logbox.insert("end",str(d).rstrip()+"\n"); self.logbox.see("end")
                elif k=="runtime":
                    for x in self.runtime.get_children(): self.runtime.delete(x)
                    for row in d: self.runtime.insert("", "end", values=row)
                elif k=="hf":
                    self.hf_data=d
                    for x in self.hftree.get_children(): self.hftree.delete(x)
                    for i,r in enumerate(d):
                        self.hftree.insert("", "end", iid=str(i),
                            values=(r["fit"],r["name"],r["btxt"],f'{r["downloads"]:,}',
                                    f'{r["likes"]:,}',len(r["files"])))
                elif k=="models":
                    self.models=d
                    self.lmlist.delete(0,"end"); self.olist.delete(0,"end")
                    for x in d["LM Studio"]: self.lmlist.insert("end",x)
                    for x in d["Ollama"]: self.olist.insert("end",x)
                    self.chat_models()
                elif k=="token": self.append("",d)
                elif k=="done": self.append("","\n"); self.status.set("준비")
                elif k=="error": messagebox.showerror("오류",d); self.status.set("오류")
                elif k=="info": messagebox.showinfo("안내",d)
        except queue.Empty:
            pass
        self.root.after(100,self.events)

    # 설치/상태
    def install(self,name):
        cmd=LM_INSTALL if name=="LM Studio" else OLLAMA_INSTALL
        if not messagebox.askyesno("설치/업데이트",f"{name} 공식 설치 스크립트를 실행합니다.\n이미 설치되어 있으면 업데이트를 시도합니다.\n\n{cmd}"):
            return
        def work():
            self.emit("status",f"{name} 설치/업데이트 중")
            try:
                flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
                p=subprocess.Popen(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-Command",cmd],
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                    encoding="utf-8",errors="replace",creationflags=flags)
                for line in iter(p.stdout.readline,""):
                    if line:self.emit("log",line)
                rc=p.wait()
                if rc==0:self.emit("info",f"{name} 설치/업데이트 명령 완료.\n필요하면 프로그램을 다시 실행하세요.")
                else:self.emit("error",f"{name} 설치 실패: {rc}")
            except Exception as e:self.emit("error",str(e))
            self.emit("status","준비"); self.refresh_status()
        threading.Thread(target=work,daemon=True).start()

    def refresh_status(self):
        def work():
            rows = []
            if exists("lms"):
                lms_path = shutil.which("lms")
                fv = get_pe_version(lms_path)
                r = hidden_run(["lms", "-v"], 8)
                cli_v = (r.stdout or "").strip() if r.returncode == 0 else ""
                if fv and cli_v:
                    v_str = f"{fv} ({cli_v})"
                elif fv:
                    v_str = f"v{fv}"
                elif cli_v:
                    v_str = cli_v
                else:
                    v_str = "설치됨"
                rows.append(("LM Studio/llmster", "설치됨", v_str, LM_API))
            else:
                rows.append(("LM Studio/llmster", "미설치", "-", LM_API))

            if exists("ollama"):
                r = hidden_run(["ollama", "--version"], 8)
                v_str = (r.stdout or "").strip() if r.returncode == 0 else "설치됨"
                rows.append(("Ollama", "설치됨", v_str, OLLAMA_API))
            else:
                rows.append(("Ollama", "미설치", "-", OLLAMA_API))
            self.emit("runtime", rows)
        threading.Thread(target=work, daemon=True).start()

    def lm_start(self):
        def work():
            if not exists("lms"): self.emit("error","먼저 LM Studio/llmster를 설치하세요."); return
            r=hidden_run(["lms","server","start"],30)
            self.emit("log",(r.stdout or "")+(r.stderr or ""))
            self.emit("info","LM Studio 서버 시작 명령을 실행했습니다." if r.returncode==0 else "서버 시작 실패")
        threading.Thread(target=work,daemon=True).start()

    def lm_stop(self):
        threading.Thread(target=lambda:self.emit("log",
            (hidden_run(["lms","server","stop"],20).stdout if exists("lms") else "")),daemon=True).start()

    def ollama_check(self):
        def work():
            try:
                r=requests.get(OLLAMA_API+"/api/tags",timeout=4); r.raise_for_status()
                self.emit("info","Ollama API 정상: "+OLLAMA_API)
            except Exception as e:self.emit("error","Ollama API 접속 실패\n"+str(e))
        threading.Thread(target=work,daemon=True).start()

    # HF
    def pick_dir(self):
        p=filedialog.askdirectory(initialdir=self.download_dir.get())
        if p:self.download_dir.set(p)

    def hf_search(self,recommend):
        def work():
            self.emit("status","Hugging Face 검색 중")
            try:
                api=HfApi()
                lim=max(5,min(int(self.hf_limit.get()),30))
                kw=self.hf_query.get().strip()
                args=dict(filter="gguf",pipeline_tag="text-generation",sort="downloads",
                          limit=min(lim*5,80),expand=["downloads","likes","siblings","gguf","tags"])
                if kw:args["search"]=kw
                rows=[]
                for m in api.list_models(**args):
                    mid=getattr(m,"id","")
                    files=[getattr(s,"rfilename","") for s in (getattr(m,"siblings",[]) or [])
                           if getattr(s,"rfilename","").lower().endswith(".gguf")]
                    if not files:continue
                    b=parse_b(mid)
                    if b is None: fit,score="확인",45
                    elif b<=self.hw["max_b"]*.55: fit,score="여유",100
                    elif b<=self.hw["max_b"]: fit,score="권장",90
                    elif b<=self.hw["max_b"]*1.35: fit,score="경계",55
                    else: fit,score="큼",20
                    down=getattr(m,"downloads",0) or 0; likes=getattr(m,"likes",0) or 0
                    rows.append(dict(fit=fit,name=mid,b=b,btxt=(f"{b:g}B" if b else "?"),
                                     downloads=down,likes=likes,files=files,
                                     rank=score*1000000+min(down,999999)))
                rows.sort(key=lambda x:x["rank" if recommend else "downloads"],reverse=True)
                self.emit("hf",rows[:lim])
            except Exception as e:self.emit("error","Hugging Face 검색 실패\n"+str(e))
            self.emit("status","준비")
        threading.Thread(target=work,daemon=True).start()

    def selected_hf(self):
        s=self.hftree.selection()
        return self.hf_data[int(s[0])] if s else None

    def repo_files_meta(self,model):
        info=HfApi().model_info(model,files_metadata=True)
        out=[]
        for s in info.siblings or []:
            n=s.rfilename
            if not n.lower().endswith(".gguf"):continue
            size=getattr(s,"size",None)
            if size is None and getattr(s,"lfs",None): size=getattr(s.lfs,"size",None)
            out.append((qscore(n),n,size))
        return sorted(out,reverse=True)

    def show_files(self):
        row=self.selected_hf()
        if not row: messagebox.showwarning("선택","모델을 선택하세요."); return
        def work():
            try:
                files=self.repo_files_meta(row["name"])[:12]
                txt=[]
                for i,(_,n,s) in enumerate(files,1):
                    txt.append(f"{i}. {n} ({s/1024**3:.1f} GB)" if s else f"{i}. {n}")
                self.emit("info",row["name"]+"\n\n"+"\n".join(txt))
            except Exception as e:self.emit("error",str(e))
        threading.Thread(target=work,daemon=True).start()

    def download(self):
        row=self.selected_hf()
        if not row: messagebox.showwarning("선택","모델을 선택하세요."); return
        target=self.install_backend.get()
        if not messagebox.askyesno("다운로드",f"{row['name']}\n권장 GGUF를 다운로드하고 {target}에 등록할까요?"):return

        def work():
            try:
                self.emit("status","GGUF 정보 확인 중")
                files=self.repo_files_meta(row["name"])
                # split shard 자동 등록은 복잡하므로 단일 GGUF 우선
                single=[x for x in files if not re.search(r'-\d{5}-of-\d{5}\.gguf$',x[1].lower())]
                use=(single or files)[0]
                _,filename,size=use
                folder=Path(self.download_dir.get())/row["name"].replace("/","__")
                folder.mkdir(parents=True,exist_ok=True)
                self.emit("status",f"다운로드 중: {filename}")
                path=hf_hub_download(repo_id=row["name"],filename=filename,local_dir=str(folder))
                self.emit("log","다운로드 완료: "+path)

                if target=="LM Studio":
                    if not exists("lms"): raise RuntimeError("lms가 없습니다.")
                    r=hidden_run(["lms","import",path],180)
                    self.emit("log",(r.stdout or "")+(r.stderr or ""))
                    if r.returncode!=0: raise RuntimeError("lms import 실패")
                else:
                    if not exists("ollama"): raise RuntimeError("ollama가 없습니다.")
                    name=re.sub(r'[^a-z0-9._-]+','-',row["name"].split("/")[-1].lower()).strip("-") or "hf-model"
                    with tempfile.TemporaryDirectory() as td:
                        mf=Path(td)/"Modelfile"
                        mf.write_text(f'FROM "{Path(path).resolve()}"\n',encoding="utf-8")
                        self.emit("status","Ollama 모델 생성 중")
                        r=hidden_run(["ollama","create",name,"-f",str(mf)],600)
                        self.emit("log",(r.stdout or "")+(r.stderr or ""))
                        if r.returncode!=0: raise RuntimeError("ollama create 실패")
                self.emit("info",f"{target} 등록 완료\n{path}")
                self.refresh_models()
            except Exception as e:self.emit("error","다운로드/등록 실패\n"+str(e))
            self.emit("status","준비")
        threading.Thread(target=work,daemon=True).start()

    # Models
    def refresh_models(self):
        def work():
            d={"LM Studio":[],"Ollama":[]}
            if exists("lms"):
                try:
                    r=hidden_run(["lms","ls","--json"],20)
                    obj=json.loads(r.stdout)
                    def walk(o):
                        a=[]
                        if isinstance(o,dict):
                            for k,v in o.items():
                                if k in ("modelKey","key") and isinstance(v,str):a.append(v)
                                else:a+=walk(v)
                        elif isinstance(o,list):
                            for x in o:a+=walk(x)
                        return a
                    d["LM Studio"]=list(dict.fromkeys(walk(obj)))
                except Exception:
                    r=hidden_run(["lms","ls"],20)
                    d["LM Studio"]=[x.strip() for x in r.stdout.splitlines() if "/" in x and x.strip()]
            try:
                r=requests.get(OLLAMA_API+"/api/tags",timeout=4)
                if r.ok:d["Ollama"]=[m["name"] for m in r.json().get("models",[]) if m.get("name")]
            except Exception:pass
            self.emit("models",d)
        threading.Thread(target=work,daemon=True).start()

    def load_lm(self):
        s=self.lmlist.curselection()
        if not s:messagebox.showwarning("선택","LM Studio 모델을 선택하세요.");return
        model=self.lmlist.get(s[0])
        def work():
            try:
                self.emit("status","모델 로드 중")
                r=hidden_run(["lms","load",model,"--gpu","auto"],600)
                self.emit("log",(r.stdout or "")+(r.stderr or ""))
                if r.returncode!=0:raise RuntimeError("모델 로드 실패")
                hidden_run(["lms","server","start"],30)
                time.sleep(1); self.refresh_models(); self.emit("info","모델 로드 완료")
            except Exception as e:self.emit("error",str(e))
            self.emit("status","준비")
        threading.Thread(target=work,daemon=True).start()

    def unload_lm(self):
        def work():
            if exists("lms"):
                r=hidden_run(["lms","unload","--all"],60)
                self.emit("log",(r.stdout or "")+(r.stderr or ""))
                self.refresh_models()
        threading.Thread(target=work,daemon=True).start()

    # Chat
    def chat_models(self):
        vals=self.models.get(self.chat_backend.get(),[])
        if self.chat_backend.get()=="LM Studio":
            try:
                r=requests.get(LM_API+"/v1/models",timeout=1)
                if r.ok:
                    x=[m["id"] for m in r.json().get("data",[]) if m.get("id")]
                    if x:vals=x
            except Exception:pass
        self.modelcb["values"]=vals
        self.chat_model.set(vals[0] if vals else "")

    def append(self,speaker,text):
        self.chatbox.config(state="normal")
        if speaker:self.chatbox.insert("end",f"\n[{speaker}]\n")
        self.chatbox.insert("end",text);self.chatbox.see("end");self.chatbox.config(state="disabled")

    def clear_chat(self):
        self.hist[self.chat_backend.get()]=[]
        self.chatbox.config(state="normal");self.chatbox.delete("1.0","end");self.chatbox.config(state="disabled")

    def send(self):
        backend=self.chat_backend.get(); model=self.chat_model.get().strip()
        msg=self.input.get("1.0","end").strip()
        if not model:messagebox.showwarning("모델","모델을 선택하세요.");return
        if not msg:return
        self.input.delete("1.0","end");self.append("사용자",msg);self.append("LLM","")
        self.hist[backend].append({"role":"user","content":msg})
        threading.Thread(target=self.chat_worker,args=(backend,model),daemon=True).start()

    def chat_worker(self,backend,model):
        try:
            self.emit("status","응답 생성 중")
            if backend=="LM Studio":
                try: requests.get(LM_API+"/v1/models",timeout=2).raise_for_status()
                except Exception:
                    if exists("lms"): hidden_run(["lms","server","start"],30);time.sleep(1)
                body={"model":model,"messages":self.hist[backend],"temperature":float(self.temp.get()),"stream":True}
                full=[]
                with requests.post(LM_API+"/v1/chat/completions",json=body,stream=True,timeout=(10,600)) as r:
                    r.raise_for_status()
                    r.encoding = "utf-8"
                    for raw_line in r.iter_lines(decode_unicode=False):
                        if not raw_line:continue
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):continue
                        p = line[5:].strip()
                        if p == "[DONE]":break
                        try:
                            delta = json.loads(p)["choices"][0].get("delta", {})
                            tok = delta.get("content") or delta.get("reasoning_content") or ""
                            if tok:
                                full.append(tok)
                                self.emit("token", tok)
                        except Exception:pass
            else:
                body={"model":model,"messages":self.hist[backend],"stream":True,
                      "options":{"temperature":float(self.temp.get())}}
                full=[]
                with requests.post(OLLAMA_API+"/api/chat",json=body,stream=True,timeout=(10,600)) as r:
                    r.raise_for_status()
                    r.encoding = "utf-8"
                    for raw_line in r.iter_lines(decode_unicode=False):
                        if not raw_line:continue
                        line = raw_line.decode("utf-8", errors="replace")
                        o = json.loads(line)
                        tok = (o.get("message") or {}).get("content", "")
                        if tok:
                            full.append(tok)
                            self.emit("token", tok)
                        if o.get("done"):break
            text="".join(full)
            if text:self.hist[backend].append({"role":"assistant","content":text})
            self.emit("done",None)
        except Exception as e:self.emit("error","채팅 오류\n"+str(e))


if __name__=="__main__":
    DOWNLOAD_ROOT.mkdir(parents=True,exist_ok=True)
    root=tk.Tk()
    Manager(root)
    root.mainloop()
