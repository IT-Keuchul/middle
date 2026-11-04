# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31.0",
#     "psutil>=5.9.0",
#     "huggingface-hub>=0.27.0",
# ]
# ///

import os
import sys
import re
import json
import queue
import shutil
import tempfile
import threading
import subprocess
import time
import importlib.util
from pathlib import Path

# --- 단일 파일 / 일반 계정 실행을 위한 필수 패키지 자동 설치 안전장치 ---
if not getattr(sys, "frozen", False):
    REQUIRED_PACKAGES = {
        "requests": "requests>=2.31.0",
        "psutil": "psutil>=5.9.0",
        "huggingface_hub": "huggingface-hub>=0.27.0",
    }
    _missing = [pkg for mod, pkg in REQUIRED_PACKAGES.items() if importlib.util.find_spec(mod) is None]
    if _missing:
        print(f"[*] 필수 패키지 확인 중: {', '.join(_missing)}")
        try:
            # 가상환경이 아닌 전역 파이썬일 때 UAC/일반 계정 권한 오류 방지용 --user 플래그 분기
            is_venv = (sys.prefix != getattr(sys, "base_prefix", sys.prefix))
            pip_cmd = [sys.executable, "-m", "pip", "install"]
            if not is_venv:
                pip_cmd.append("--user")
            pip_cmd.extend(_missing)
            subprocess.check_call(pip_cmd)
        except Exception as _e:
            print(f"[!] 패키지 자동 설치 건너뜀 (권한 또는 네트워크): {_e}")

# --- Tcl/Tk 환경변수 오염 방지 및 경로 복구 (PyInstaller / 일반 환경 완벽 호환) ---
def _setup_tcl_tk():
    is_frozen = getattr(sys, "frozen", False)
    mei_pass = getattr(sys, "_MEIPASS", None)

    # 1. 이전 프로세스나 잘못된 환경변수 오염 정리
    for var in ("TCL_LIBRARY", "TK_LIBRARY"):
        val = os.environ.get(var)
        if val:
            if not os.path.exists(val):
                del os.environ[var]
            elif not is_frozen and "_MEI" in val:
                del os.environ[var]
            elif is_frozen and mei_pass and "_MEI" in val and not val.startswith(mei_pass):
                del os.environ[var]

    # 2. 유효한 Tcl/Tk 라이브러리 디렉토리 자동 탐색 (환경변수 누락 시에만)
    search_dirs = []
    if is_frozen and mei_pass:
        search_dirs.append(Path(mei_pass))
    search_dirs.extend([
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(getattr(sys, "exec_prefix", sys.prefix)),
    ])

    for root_dir in search_dirs:
        if "TCL_LIBRARY" not in os.environ:
            for cand in [
                root_dir / "tcl" / "tcl8.6",
                root_dir / "lib" / "tcl8.6",
                root_dir / "tcl8.6",
                root_dir / "Library" / "lib" / "tcl8.6",
            ]:
                if (cand / "init.tcl").exists():
                    os.environ["TCL_LIBRARY"] = str(cand)
                    break

        if "TK_LIBRARY" not in os.environ:
            for cand in [
                root_dir / "tcl" / "tk8.6",
                root_dir / "lib" / "tk8.6",
                root_dir / "tk8.6",
                root_dir / "Library" / "lib" / "tk8.6",
            ]:
                if (cand / "tk.tcl").exists() or cand.exists():
                    os.environ["TK_LIBRARY"] = str(cand)
                    break

        if "TCL_LIBRARY" in os.environ and "TK_LIBRARY" in os.environ:
            break

_setup_tcl_tk()

# Windows 고해상도(DPI) 배율 흐림 방지
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import requests
import psutil
from huggingface_hub import HfApi, hf_hub_download

LM_INSTALL = "irm https://lmstudio.ai/install.ps1 | iex"
OLLAMA_INSTALL = "irm https://ollama.com/install.ps1 | iex"
LM_API = "http://127.0.0.1:1234"
OLLAMA_API = "http://127.0.0.1:11434"
DOWNLOAD_ROOT = Path.home() / "LocalLLMModels"

DEFAULT_SYSTEM_PROMPT = (
    "당신은 친절한 AI 어시스턴트입니다. "
    "내부 생각 과정(Chain of Thought), 영어 독백, 추론 과정, 혼잣말을 일절 출력하지 마세요. "
    "오직 사용자의 말에 필요한 대답만을 자연스럽고 유창한 한국어로 직접 답변하세요."
)


class ThinkTagFilter:
    """스트리밍 토큰에서 <think>...</think> 블록을 완벽하게 걸러내는 필터"""
    def __init__(self):
        self.in_think = False
        self.buffer = ""

    def process(self, chunk: str) -> str:
        self.buffer += chunk
        output = []
        while self.buffer:
            if self.in_think:
                if "</think>" in self.buffer:
                    _, self.buffer = self.buffer.split("</think>", 1)
                    self.in_think = False
                else:
                    matched = False
                    for i in range(len("</think>") - 1, 0, -1):
                        if self.buffer.endswith("</think>"[:i]):
                            self.buffer = self.buffer[-i:]
                            matched = True
                            break
                    if not matched:
                        self.buffer = ""
                    break
            else:
                if "<think>" in self.buffer:
                    before, self.buffer = self.buffer.split("<think>", 1)
                    output.append(before)
                    self.in_think = True
                else:
                    matched = False
                    for i in range(len("<think>") - 1, 0, -1):
                        if self.buffer.endswith("<think>"[:i]):
                            output.append(self.buffer[:-i])
                            self.buffer = self.buffer[-i:]
                            matched = True
                            break
                    if not matched:
                        output.append(self.buffer)
                        self.buffer = ""
                    break
        return "".join(output)

    def flush(self) -> str:
        if self.in_think:
            return ""
        res = self.buffer
        self.buffer = ""
        return res



def refresh_system_path():
    """레지스트리 및 기본 설치 위치에서 최신 PATH를 가져와 os.environ['PATH'] 갱신 (설치 직후 즉시 인식)"""
    if os.name != "nt":
        return
    paths = []
    # 일반 계정 및 기본 설치 경로 후보군
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    user_profile = os.environ.get("USERPROFILE", "")
    default_candidates = [
        Path(local_appdata) / "Programs" / "Ollama",
        Path(user_profile) / ".lmstudio" / "bin",
        Path(local_appdata) / "LM-Studio" / "resources" / "bin",
        Path(user_profile) / "AppData" / "Local" / "Programs" / "Ollama",
    ]
    for c in default_candidates:
        if c.exists() and str(c) not in paths:
            paths.append(str(c))

    # 레지스트리 읽기 (User PATH는 일반 사용자 권한으로 언제나 안전하게 접근 가능)
    try:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                user_path, _ = winreg.QueryValueEx(key, "Path")
                if user_path:
                    paths.extend(user_path.split(os.pathsep))
        except Exception:
            pass

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
                sys_path, _ = winreg.QueryValueEx(key, "Path")
                if sys_path:
                    paths.extend(sys_path.split(os.pathsep))
        except Exception:
            pass
    except Exception:
        pass

    current_paths = os.environ.get("PATH", "").split(os.pathsep)
    combined = []
    seen = set()
    for p in paths + current_paths:
        p_clean = os.path.expandvars(p.strip().strip('"'))
        if p_clean and p_clean.lower() not in seen:
            seen.add(p_clean.lower())
            combined.append(p_clean)
    os.environ["PATH"] = os.pathsep.join(combined)


def hidden_run(args, timeout=None):
    """프로세스를 안전하게 숨김 실행하며 예외를 흡수하여 충돌을 방지"""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=flags
        )
    except FileNotFoundError:
        class DummyProcess:
            returncode = 127
            stdout = ""
            stderr = f"실행 파일을 찾을 수 없습니다: {args[0] if args else ''}"
        return DummyProcess()
    except subprocess.TimeoutExpired:
        class DummyProcess:
            returncode = 124
            stdout = ""
            stderr = f"명령 실행 시간 초과 ({timeout}초)"
        return DummyProcess()
    except Exception as e:
        class DummyProcess:
            returncode = -1
            stdout = ""
            stderr = f"실행 실패: {e}"
        return DummyProcess()


def exists(cmd):
    """실행 가능 여부 확인 (동적 PATH 갱신 포함)"""
    if shutil.which(cmd) is not None:
        return True
    refresh_system_path()
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
        for line in (r.stdout or "").splitlines():
            p = [x.strip() for x in line.split(",", 1)]
            if len(p) == 2:
                try:
                    rows.append((p[0], float(p[1]) / 1024))
                except ValueError:
                    pass
        return rows
    except Exception:
        return []


def hardware():
    try:
        vm = psutil.virtual_memory()
        ram_total = vm.total / 1024**3
        free_ram = vm.available / 1024**3
    except Exception:
        ram_total = 8.0
        free_ram = 4.0

    try:
        disk = shutil.disk_usage(str(Path.home()))
        disk_free = disk.free / 1024**3
    except Exception:
        disk_free = 10.0

    gpus = nvidia_info()
    vram = max([x[1] for x in gpus], default=0)

    if vram >= 45: max_b = 70
    elif vram >= 22: max_b = 32
    elif vram >= 14: max_b = 14
    elif vram >= 7: max_b = 8
    elif vram >= 4: max_b = 4
    else:
        max_b = 14 if ram_total >= 64 else 8 if ram_total >= 32 else 4 if ram_total >= 16 else 3

    return {
        "ram": ram_total,
        "free_ram": free_ram,
        "disk": disk_free,
        "gpus": gpus,
        "vram": vram,
        "max_b": max_b,
    }


def parse_b(name):
    m = re.findall(r'(?<!\d)(\d+(?:\.\d+)?)\s*[bB](?![A-Za-z])', name)
    try:
        return max(map(float, m)) if m else None
    except Exception:
        return None


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
        self.is_closing = False
        self.after_id = None

        root.title("Local LLM Manager")
        root.geometry("1200x820")
        root.minsize(1000, 700)

        # Tkinter 예외 핸들러 등록 (창 닫힘 중 TclError 등으로 인한 비정상 크래시 완충)
        def handle_tk_exception(exc, val, tb):
            if self.is_closing or "application has been destroyed" in str(val) or "can't invoke" in str(val):
                return
            import traceback
            traceback.print_exception(exc, val, tb)
        self.root.report_callback_exception = handle_tk_exception

        # 창 닫기 프로토콜 연결
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

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
        self.after_id = root.after(100, self.events)
        self.refresh_status()
        self.refresh_models()

    def on_close(self):
        """창 종료 시 안전하게 정리하여 TCL 에러 및 메모리 누수 방지"""
        self.is_closing = True
        if self.after_id:
            try:
                self.root.after_cancel(self.after_id)
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def build(self):
        head = ttk.Frame(self.root)
        head.pack(fill="x", padx=10, pady=8)
        ttk.Label(head, text="Local LLM Manager", font=("", 16, "bold")).pack(side="left")
        ttk.Label(head, textvariable=self.status).pack(side="right")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=5)

        self.t1, self.t2, self.t3, self.t4, self.t5 = [ttk.Frame(nb) for _ in range(5)]
        for t, name in zip(
            [self.t1, self.t2, self.t3, self.t4, self.t5],
            ["1. 설치/업데이트", "2. Hugging Face 모델", "3. 설치된 모델", "4. LLM 대화", "로그"]
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
        gpu = ", ".join(f"{n} ({v:.1f}GB)" for n, v in self.hw["gpus"]) or "NVIDIA GPU 미감지"
        txt = (
            f'RAM {self.hw["ram"]:.1f}GB / 사용 가능 {self.hw["free_ram"]:.1f}GB\n'
            f'GPU: {gpu}\n'
            f'홈 드라이브 여유 {self.hw["disk"]:.1f}GB / 추천 최대 규모 약 {self.hw["max_b"]}B(Q4 기준)'
        )
        ttk.Label(f, text=txt, justify="left").pack(anchor="w", padx=10, pady=10)

        g = ttk.LabelFrame(self.t1, text="런타임")
        g.pack(fill="x", padx=10, pady=6)

        self.runtime = ttk.Treeview(g, columns=("name", "state", "ver", "api"), show="headings", height=3)
        for c, t, w in [
            ("name", "런타임", 220), ("state", "상태", 120), ("ver", "버전", 270), ("api", "기본 API", 260)
        ]:
            self.runtime.heading(c, text=t)
            self.runtime.column(c, width=w, anchor="w")
        self.runtime.pack(fill="x", padx=8, pady=8)

        b = ttk.Frame(g)
        b.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(b, text="LM Studio/llmster 설치·업데이트",
                   command=lambda: self.install("LM Studio")).pack(side="left", padx=3)
        ttk.Button(b, text="Ollama 설치·업데이트",
                   command=lambda: self.install("Ollama")).pack(side="left", padx=3)
        ttk.Button(b, text="상태 새로고침", command=self.refresh_status).pack(side="left", padx=3)

        s = ttk.LabelFrame(self.t1, text="서버")
        s.pack(fill="x", padx=10, pady=6)
        ttk.Button(s, text="LM Studio 서버 시작", command=self.lm_start).pack(side="left", padx=8, pady=8)
        ttk.Button(s, text="LM Studio 서버 중지", command=self.lm_stop).pack(side="left", padx=4, pady=8)
        ttk.Button(s, text="Ollama API 확인", command=self.ollama_check).pack(side="left", padx=4, pady=8)

        ttk.Label(
            self.t1,
            text="※ 위 LM Studio PowerShell 명령은 Desktop GUI가 아니라 llmster + lms CLI 설치 방식입니다.\n"
                 "※ 일반 사용자 계정에서도 별도 UAC 관리자 권한 없이 본인 계정에 설치 및 실행이 가능합니다."
        ).pack(anchor="w", padx=14, pady=8)

    def build_hf(self):
        f = ttk.LabelFrame(self.t2, text="Hugging Face GGUF 모델 검색")
        f.pack(fill="x", padx=10, pady=10)
        ttk.Label(f, text="검색어").grid(row=0, column=0, padx=5, pady=7)
        ttk.Entry(f, textvariable=self.hf_query, width=35).grid(row=0, column=1, padx=5, sticky="ew")
        ttk.Label(f, text="개수").grid(row=0, column=2, padx=5)
        ttk.Spinbox(f, from_=5, to=30, textvariable=self.hf_limit, width=6).grid(row=0, column=3)
        ttk.Button(f, text="PC 맞춤 추천", command=lambda: self.hf_search(True)).grid(row=0, column=4, padx=5)
        ttk.Button(f, text="검색", command=lambda: self.hf_search(False)).grid(row=0, column=5, padx=5)
        f.columnconfigure(1, weight=1)

        p = ttk.Frame(self.t2)
        p.pack(fill="x", padx=10)
        ttk.Label(p, text="다운로드 폴더").pack(side="left")
        ttk.Entry(p, textvariable=self.download_dir).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(p, text="선택", command=self.pick_dir).pack(side="left")

        self.hftree = ttk.Treeview(
            self.t2, columns=("fit", "name", "b", "down", "like", "files"), show="headings", height=17
        )
        for c, t, w in [
            ("fit", "적합도", 80), ("name", "모델", 520), ("b", "규모", 80),
            ("down", "다운로드", 100), ("like", "Likes", 70), ("files", "GGUF", 70)
        ]:
            self.hftree.heading(c, text=t)
            self.hftree.column(c, width=w, anchor="w")
        self.hftree.pack(fill="both", expand=True, padx=10, pady=8)

        b = ttk.Frame(self.t2)
        b.pack(fill="x", padx=10, pady=5)
        ttk.Label(b, text="등록 대상").pack(side="left")
        ttk.Combobox(b, textvariable=self.install_backend, values=["LM Studio", "Ollama"],
                     state="readonly", width=14).pack(side="left", padx=5)
        ttk.Button(b, text="GGUF 후보 보기", command=self.show_files).pack(side="left", padx=4)
        ttk.Button(b, text="권장 GGUF 다운로드 + 등록", command=self.download).pack(side="left", padx=4)

    def build_models(self):
        top = ttk.Frame(self.t3)
        top.pack(fill="x", padx=10, pady=10)
        ttk.Button(top, text="새로고침", command=self.refresh_models).pack(side="left")
        ttk.Button(top, text="LM Studio 선택 모델 로드", command=self.load_lm).pack(side="left", padx=5)
        ttk.Button(top, text="LM Studio 전체 언로드", command=self.unload_lm).pack(side="left", padx=5)

        pane = ttk.Panedwindow(self.t3, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10, pady=5)
        a = ttk.LabelFrame(pane, text="LM Studio")
        b = ttk.LabelFrame(pane, text="Ollama")
        pane.add(a, weight=1)
        pane.add(b, weight=1)
        self.lmlist = tk.Listbox(a)
        self.lmlist.pack(fill="both", expand=True, padx=6, pady=6)
        self.olist = tk.Listbox(b)
        self.olist.pack(fill="both", expand=True, padx=6, pady=6)

    def build_chat(self):
        top = ttk.Frame(self.t4)
        top.pack(fill="x", padx=10, pady=10)
        ttk.Label(top, text="런타임").pack(side="left")
        cb = ttk.Combobox(top, textvariable=self.chat_backend, values=["LM Studio", "Ollama"],
                          state="readonly", width=13)
        cb.pack(side="left", padx=5)
        cb.bind("<<ComboboxSelected>>", lambda e: self.chat_models())
        ttk.Label(top, text="모델").pack(side="left", padx=(10, 0))
        self.modelcb = ttk.Combobox(top, textvariable=self.chat_model, state="readonly", width=48)
        self.modelcb.pack(side="left", fill="x", expand=True, padx=5)
        ttk.Label(top, text="Temperature").pack(side="left")
        ttk.Scale(top, from_=0, to=1.5, variable=self.temp, orient="horizontal", length=120).pack(side="left", padx=4)

        self.chatbox = tk.Text(self.t4, wrap="word", state="disabled")
        self.chatbox.pack(fill="both", expand=True, padx=10, pady=5)
        bot = ttk.Frame(self.t4)
        bot.pack(fill="x", padx=10, pady=10)
        self.input = tk.Text(bot, height=5, wrap="word")
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Control-Return>", lambda e: self.send())
        x = ttk.Frame(bot)
        x.pack(side="left", padx=(8, 0))
        ttk.Button(x, text="전송\nCtrl+Enter", command=self.send).pack(fill="x")
        ttk.Button(x, text="대화 초기화", command=self.clear_chat).pack(fill="x", pady=5)

    def emit(self, kind, data):
        if not self.is_closing:
            self.q.put((kind, data))

    def events(self):
        """UI 스레드 안전 이벤트 루프 (TclError 원천 차단)"""
        if self.is_closing:
            return
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return

        try:
            while not self.q.empty():
                try:
                    k, d = self.q.get_nowait()
                except queue.Empty:
                    break

                if self.is_closing:
                    return

                if k == "status":
                    self.status.set(d)
                elif k == "log":
                    try:
                        if self.logbox.winfo_exists():
                            self.logbox.insert("end", str(d).rstrip() + "\n")
                            self.logbox.see("end")
                    except Exception:
                        pass
                elif k == "runtime":
                    try:
                        if self.runtime.winfo_exists():
                            for x in self.runtime.get_children():
                                self.runtime.delete(x)
                            for row in d:
                                self.runtime.insert("", "end", values=row)
                    except Exception:
                        pass
                elif k == "hf":
                    try:
                        if self.hftree.winfo_exists():
                            self.hf_data = d
                            for x in self.hftree.get_children():
                                self.hftree.delete(x)
                            for i, r in enumerate(d):
                                self.hftree.insert("", "end", iid=str(i),
                                    values=(r["fit"], r["name"], r["btxt"],
                                            f'{r["downloads"]:,}', f'{r["likes"]:,}', len(r["files"])))
                    except Exception:
                        pass
                elif k == "models":
                    try:
                        self.models = d
                        if self.lmlist.winfo_exists():
                            self.lmlist.delete(0, "end")
                            for x in d.get("LM Studio", []):
                                self.lmlist.insert("end", x)
                        if self.olist.winfo_exists():
                            self.olist.delete(0, "end")
                            for x in d.get("Ollama", []):
                                self.olist.insert("end", x)
                        self.chat_models()
                    except Exception:
                        pass
                elif k == "token":
                    self.append("", d)
                elif k == "done":
                    self.append("", "\n")
                    self.status.set("준비")
                elif k == "error":
                    if not self.is_closing:
                        try:
                            messagebox.showerror("오류", str(d), parent=self.root)
                        except Exception:
                            pass
                    self.status.set("오류")
                elif k == "info":
                    if not self.is_closing:
                        try:
                            messagebox.showinfo("안내", str(d), parent=self.root)
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            if not self.is_closing:
                try:
                    if self.root.winfo_exists():
                        self.after_id = self.root.after(100, self.events)
                except Exception:
                    pass

    # 설치/상태
    def install(self, name):
        cmd = LM_INSTALL if name == "LM Studio" else OLLAMA_INSTALL
        if not messagebox.askyesno(
            "설치/업데이트",
            f"{name} 공식 설치 스크립트를 실행합니다.\n"
            f"일반 계정 환경에서도 사용자 프로필에 안전하게 설치됩니다.\n"
            f"이미 설치되어 있으면 최신 버전으로 업데이트합니다.\n\n"
            f"명령어:\n{cmd}",
            parent=self.root
        ):
            return

        def work():
            self.emit("status", f"{name} 설치/업데이트 중")
            try:
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                p = subprocess.Popen(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", creationflags=flags
                )
                if p.stdout:
                    for line in iter(p.stdout.readline, ""):
                        if line:
                            self.emit("log", line)
                rc = p.wait()

                # 설치 후 새 경로를 즉시 인식하도록 PATH 갱신
                refresh_system_path()

                if rc == 0:
                    self.emit("info", f"{name} 설치/업데이트가 완료되었습니다.\n런타임 상태를 새로고침합니다.")
                else:
                    self.emit("error", f"{name} 설치 스크립트 종료 코드: {rc}\n로그 탭을 확인하세요.")
            except Exception as e:
                self.emit("error", f"설치 실행 중 오류: {e}")
            self.emit("status", "준비")
            self.refresh_status()

        threading.Thread(target=work, daemon=True).start()

    def refresh_status(self):
        def work():
            refresh_system_path()
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
            if not exists("lms"):
                self.emit("error", "먼저 LM Studio/llmster를 설치하세요.")
                return
            r = hidden_run(["lms", "server", "start"], 30)
            self.emit("log", (r.stdout or "") + (r.stderr or ""))
            if r.returncode == 0:
                self.emit("info", "LM Studio 서버가 정상 시작되었습니다.")
            else:
                self.emit("error", f"LM Studio 서버 시작 실패:\n{r.stderr or r.stdout}")
        threading.Thread(target=work, daemon=True).start()

    def lm_stop(self):
        def work():
            if exists("lms"):
                r = hidden_run(["lms", "server", "stop"], 20)
                self.emit("log", (r.stdout or "") + (r.stderr or ""))
                self.emit("info", "LM Studio 서버를 중지했습니다.")
        threading.Thread(target=work, daemon=True).start()

    def ollama_check(self):
        def work():
            try:
                r = requests.get(OLLAMA_API + "/api/tags", timeout=4)
                r.raise_for_status()
                self.emit("info", f"Ollama API 정상 연결: {OLLAMA_API}")
            except Exception as e:
                self.emit("error", f"Ollama API 접속 실패:\n{e}\n\nOllama 서비스가 실행 중인지 확인하세요.")
        threading.Thread(target=work, daemon=True).start()

    # HF
    def pick_dir(self):
        p = filedialog.askdirectory(initialdir=self.download_dir.get(), parent=self.root)
        if p:
            # 쓰기 권한 테스트 (일반 계정 권한 확인)
            try:
                test_dir = Path(p)
                test_dir.mkdir(parents=True, exist_ok=True)
                test_file = test_dir / f".write_test_{int(time.time())}.tmp"
                test_file.touch()
                test_file.unlink()
                self.download_dir.set(p)
            except Exception as e:
                messagebox.showerror(
                    "권한 오류",
                    f"해당 폴더에 쓰기 권한이 없습니다:\n{p}\n\n일반 계정 권한으로 쓰기가 가능한 폴더(예: 사용자 폴더)를 선택해주세요.\n{e}",
                    parent=self.root
                )

    def hf_search(self, recommend):
        def work():
            self.emit("status", "Hugging Face 검색 중")
            try:
                api = HfApi()
                try:
                    lim = max(5, min(int(self.hf_limit.get()), 30))
                except Exception:
                    lim = 12

                kw = self.hf_query.get().strip()
                args = dict(
                    filter="gguf",
                    pipeline_tag="text-generation",
                    sort="downloads",
                    limit=min(lim * 5, 80),
                    expand=["downloads", "likes", "siblings", "gguf", "tags"]
                )
                if kw:
                    args["search"] = kw

                rows = []
                for m in api.list_models(**args):
                    mid = getattr(m, "id", "")
                    files = [
                        getattr(s, "rfilename", "") for s in (getattr(m, "siblings", []) or [])
                        if getattr(s, "rfilename", "").lower().endswith(".gguf")
                    ]
                    if not files:
                        continue
                    b = parse_b(mid)
                    if b is None:
                        fit, score = "확인", 45
                    elif b <= self.hw["max_b"] * 0.55:
                        fit, score = "여유", 100
                    elif b <= self.hw["max_b"]:
                        fit, score = "권장", 90
                    elif b <= self.hw["max_b"] * 1.35:
                        fit, score = "경계", 55
                    else:
                        fit, score = "큼", 20

                    down = getattr(m, "downloads", 0) or 0
                    likes = getattr(m, "likes", 0) or 0
                    rows.append(dict(
                        fit=fit, name=mid, b=b, btxt=(f"{b:g}B" if b else "?"),
                        downloads=down, likes=likes, files=files,
                        rank=score * 1000000 + min(down, 999999)
                    ))
                rows.sort(key=lambda x: x["rank" if recommend else "downloads"], reverse=True)
                self.emit("hf", rows[:lim])
            except Exception as e:
                self.emit("error", f"Hugging Face 검색 실패:\n{e}")
            self.emit("status", "준비")

        threading.Thread(target=work, daemon=True).start()

    def selected_hf(self):
        try:
            s = self.hftree.selection()
            if not s:
                return None
            idx = int(s[0])
            if 0 <= idx < len(self.hf_data):
                return self.hf_data[idx]
        except Exception:
            pass
        return None

    def repo_files_meta(self, model):
        info = HfApi().model_info(model, files_metadata=True)
        out = []
        for s in info.siblings or []:
            n = s.rfilename
            if not n.lower().endswith(".gguf"):
                continue
            size = getattr(s, "size", None)
            if size is None and getattr(s, "lfs", None):
                size = getattr(s.lfs, "size", None)
            out.append((qscore(n), n, size))
        return sorted(out, reverse=True)

    def show_files(self):
        row = self.selected_hf()
        if not row:
            messagebox.showwarning("선택", "목록에서 모델을 먼저 선택하세요.", parent=self.root)
            return

        def work():
            try:
                files = self.repo_files_meta(row["name"])[:12]
                txt = []
                for i, (_, n, s) in enumerate(files, 1):
                    txt.append(f"{i}. {n} ({s/1024**3:.1f} GB)" if s else f"{i}. {n}")
                self.emit("info", row["name"] + "\n\n" + "\n".join(txt))
            except Exception as e:
                self.emit("error", f"GGUF 파일 목록 조회 실패:\n{e}")
        threading.Thread(target=work, daemon=True).start()

    def download(self):
        row = self.selected_hf()
        if not row:
            messagebox.showwarning("선택", "목록에서 모델을 먼저 선택하세요.", parent=self.root)
            return
        target = self.install_backend.get()
        if not messagebox.askyesno(
            "다운로드 및 등록",
            f"모델: {row['name']}\n\n권장 GGUF를 다운로드하고 {target}에 자동 등록할까요?",
            parent=self.root
        ):
            return

        def work():
            try:
                self.emit("status", "GGUF 정보 확인 중")
                files = self.repo_files_meta(row["name"])
                if not files:
                    raise RuntimeError("다운로드 가능한 GGUF 파일이 없습니다.")

                single = [x for x in files if not re.search(r'-\d{5}-of-\d{5}\.gguf$', x[1].lower())]
                use = (single or files)[0]
                _, filename, size = use
                folder = Path(self.download_dir.get()) / row["name"].replace("/", "__")
                folder.mkdir(parents=True, exist_ok=True)

                self.emit("status", f"다운로드 중: {filename}")
                self.emit("log", f"다운로드 시작: {row['name']} -> {filename}")

                # local_dir_use_symlinks=False를 지정하여 Windows 일반 계정 Symlink(WinError 1314) 권한 오류 원천 방지
                path = hf_hub_download(
                    repo_id=row["name"],
                    filename=filename,
                    local_dir=str(folder),
                    local_dir_use_symlinks=False
                )
                self.emit("log", f"다운로드 완료: {path}")

                if target == "LM Studio":
                    if not exists("lms"):
                        raise RuntimeError("LM Studio CLI(lms)가 감지되지 않았습니다. 먼저 설치하세요.")
                    self.emit("status", "LM Studio에 모델 등록 중")
                    r = hidden_run(["lms", "import", path], 180)
                    self.emit("log", (r.stdout or "") + (r.stderr or ""))
                    if r.returncode != 0:
                        raise RuntimeError(f"lms import 실패: {r.stderr or r.stdout}")
                else:
                    if not exists("ollama"):
                        raise RuntimeError("Ollama가 감지되지 않았습니다. 먼저 설치하세요.")
                    name = re.sub(r'[^a-z0-9._-]+', '-', row["name"].split("/")[-1].lower()).strip("-") or "hf-model"
                    with tempfile.TemporaryDirectory() as td:
                        mf = Path(td) / "Modelfile"
                        # Windows 역슬래시 escape 파싱 오류 방지를 위해 as_posix() 슬래시 경로 사용
                        clean_model_path = Path(path).resolve().as_posix()
                        mf.write_text(f'FROM "{clean_model_path}"\n', encoding="utf-8")
                        self.emit("status", "Ollama 모델 생성 중")
                        r = hidden_run(["ollama", "create", name, "-f", str(mf)], 600)
                        self.emit("log", (r.stdout or "") + (r.stderr or ""))
                        if r.returncode != 0:
                            raise RuntimeError(f"ollama create 실패: {r.stderr or r.stdout}")

                self.emit("info", f"{target}에 모델이 성공적으로 등록되었습니다!\n\n파일: {path}")
                self.refresh_models()
            except Exception as e:
                self.emit("error", f"다운로드/등록 실패:\n{e}")
            self.emit("status", "준비")

        threading.Thread(target=work, daemon=True).start()

    # Models
    def refresh_models(self):
        def work():
            d = {"LM Studio": [], "Ollama": []}
            if exists("lms"):
                try:
                    r = hidden_run(["lms", "ls", "--json"], 20)
                    if r.returncode == 0 and r.stdout:
                        obj = json.loads(r.stdout)
                        def walk(o):
                            a = []
                            if isinstance(o, dict):
                                for k, v in o.items():
                                    if k in ("modelKey", "key") and isinstance(v, str):
                                        a.append(v)
                                    else:
                                        a += walk(v)
                            elif isinstance(o, list):
                                for x in o:
                                    a += walk(x)
                            return a
                        d["LM Studio"] = list(dict.fromkeys(walk(obj)))
                    else:
                        r = hidden_run(["lms", "ls"], 20)
                        d["LM Studio"] = [x.strip() for x in (r.stdout or "").splitlines() if "/" in x and x.strip()]
                except Exception:
                    r = hidden_run(["lms", "ls"], 20)
                    d["LM Studio"] = [x.strip() for x in (r.stdout or "").splitlines() if "/" in x and x.strip()]

            try:
                r = requests.get(OLLAMA_API + "/api/tags", timeout=4)
                if r.ok:
                    d["Ollama"] = [m["name"] for m in r.json().get("models", []) if m.get("name")]
            except Exception:
                pass

            self.emit("models", d)

        threading.Thread(target=work, daemon=True).start()

    def load_lm(self):
        try:
            s = self.lmlist.curselection()
            if not s:
                messagebox.showwarning("선택", "LM Studio 모델을 선택하세요.", parent=self.root)
                return
            model = self.lmlist.get(s[0])
        except Exception:
            return

        def work():
            try:
                self.emit("status", "모델 로드 중")
                r = hidden_run(["lms", "load", model, "--gpu", "auto"], 600)
                self.emit("log", (r.stdout or "") + (r.stderr or ""))
                if r.returncode != 0:
                    raise RuntimeError(f"모델 로드 실패:\n{r.stderr or r.stdout}")
                hidden_run(["lms", "server", "start"], 30)
                time.sleep(1)
                self.refresh_models()
                self.emit("info", f"모델 '{model}' 로드 완료")
            except Exception as e:
                self.emit("error", str(e))
            self.emit("status", "준비")

        threading.Thread(target=work, daemon=True).start()

    def unload_lm(self):
        def work():
            if exists("lms"):
                r = hidden_run(["lms", "unload", "--all"], 60)
                self.emit("log", (r.stdout or "") + (r.stderr or ""))
                self.refresh_models()
                self.emit("info", "LM Studio 모델을 모두 언로드했습니다.")
        threading.Thread(target=work, daemon=True).start()

    # Chat
    def chat_models(self):
        try:
            cur_backend = self.chat_backend.get()
            vals = list(self.models.get(cur_backend, []))
            if cur_backend == "LM Studio":
                try:
                    r = requests.get(LM_API + "/v1/models", timeout=1.5)
                    if r.ok:
                        x = [m["id"] for m in r.json().get("data", []) if m.get("id")]
                        if x:
                            vals = x
                except Exception:
                    pass
            self.modelcb["values"] = vals
            if vals:
                if self.chat_model.get() not in vals:
                    self.chat_model.set(vals[0])
            else:
                self.chat_model.set("")
        except Exception:
            pass

    def append(self, speaker, text):
        if self.is_closing:
            return
        try:
            if not self.chatbox.winfo_exists():
                return
            self.chatbox.config(state="normal")
            if speaker:
                self.chatbox.insert("end", f"\n[{speaker}]\n")
            self.chatbox.insert("end", text)
            self.chatbox.see("end")
            self.chatbox.config(state="disabled")
        except Exception:
            pass

    def clear_chat(self):
        try:
            self.hist[self.chat_backend.get()] = []
            if self.chatbox.winfo_exists():
                self.chatbox.config(state="normal")
                self.chatbox.delete("1.0", "end")
                self.chatbox.config(state="disabled")
        except Exception:
            pass

    def send(self):
        backend = self.chat_backend.get()
        model = self.chat_model.get().strip()
        msg = self.input.get("1.0", "end").strip()
        if not model:
            messagebox.showwarning("모델", "대화할 모델을 선택하세요.", parent=self.root)
            return
        if not msg:
            return
        self.input.delete("1.0", "end")
        self.append("사용자", msg)
        self.append("LLM", "")
        self.hist[backend].append({"role": "user", "content": msg})
        threading.Thread(target=self.chat_worker, args=(backend, model), daemon=True).start()

    def chat_worker(self, backend, model):
        try:
            self.emit("status", "응답 생성 중")
            filter_obj = ThinkTagFilter()
            started = False

            # 시스템 프롬프트(한국어로 필요한 대화만 하도록 유도) 구성
            hist_messages = self.hist.get(backend, [])
            messages = []
            if not any(m.get("role") == "system" for m in hist_messages):
                messages.append({"role": "system", "content": DEFAULT_SYSTEM_PROMPT})
            messages.extend(hist_messages)

            if backend == "LM Studio":
                try:
                    requests.get(LM_API + "/v1/models", timeout=2).raise_for_status()
                except Exception:
                    if exists("lms"):
                        hidden_run(["lms", "server", "start"], 30)
                        time.sleep(1)

                body = {
                    "model": model,
                    "messages": messages,
                    "temperature": float(self.temp.get()),
                    "stream": True
                }
                full = []
                with requests.post(LM_API + "/v1/chat/completions", json=body, stream=True, timeout=(10, 600)) as r:
                    if r.status_code != 200:
                        try:
                            err_detail = r.json().get("error", {}).get("message", r.text)
                        except Exception:
                            err_detail = r.text
                        raise RuntimeError(f"LM Studio 에러 (HTTP {r.status_code}): {err_detail}")

                    r.encoding = "utf-8"
                    for raw_line in r.iter_lines(decode_unicode=False):
                        if self.is_closing:
                            return
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        p = line[5:].strip()
                        if p == "[DONE]":
                            break
                        try:
                            delta = json.loads(p)["choices"][0].get("delta", {})
                            # reasoning_content(내부 독백/추론 과정)는 제외하고 순수한 대화 content만 가져옴
                            raw_tok = delta.get("content") or ""
                            if raw_tok:
                                tok = filter_obj.process(raw_tok)
                                if tok:
                                    if not started:
                                        tok = tok.lstrip("\r\n")
                                        if not tok:
                                            continue
                                        started = True
                                    full.append(tok)
                                    self.emit("token", tok)
                        except Exception:
                            pass

                remain = filter_obj.flush()
                if remain:
                    full.append(remain)
                    self.emit("token", remain)

            else:
                body = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "options": {"temperature": float(self.temp.get())}
                }
                full = []
                with requests.post(OLLAMA_API + "/api/chat", json=body, stream=True, timeout=(10, 600)) as r:
                    if r.status_code != 200:
                        try:
                            err_detail = r.json().get("error", r.text)
                        except Exception:
                            err_detail = r.text
                        raise RuntimeError(f"Ollama 에러 (HTTP {r.status_code}): {err_detail}")

                    r.encoding = "utf-8"
                    for raw_line in r.iter_lines(decode_unicode=False):
                        if self.is_closing:
                            return
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        o = json.loads(line)
                        if "error" in o:
                            raise RuntimeError(f"Ollama 생성 오류: {o['error']}")
                        raw_tok = (o.get("message") or {}).get("content", "")
                        if raw_tok:
                            tok = filter_obj.process(raw_tok)
                            if tok:
                                if not started:
                                    tok = tok.lstrip("\r\n")
                                    if not tok:
                                        continue
                                    started = True
                                full.append(tok)
                                self.emit("token", tok)
                        if o.get("done"):
                            break

                remain = filter_obj.flush()
                if remain:
                    full.append(remain)
                    self.emit("token", remain)

            text = "".join(full)
            if text:
                self.hist[backend].append({"role": "assistant", "content": text})
            self.emit("done", None)
        except Exception as e:
            self.emit("error", f"채팅 생성 오류:\n{e}")
            self.emit("done", None)


if __name__ == "__main__":
    try:
        DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    root = tk.Tk()
    Manager(root)
    root.mainloop()
