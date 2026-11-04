import json
import os
import queue
import sys
import threading
from pathlib import Path


# ============================================================
# 1. Tcl/Tk 환경 안전 초기화 (TCL 에러 방지)
# ============================================================
def setup_tcl_tk_environment():
    """일반 계정, 가상환경, PyInstaller, 다양한 윈도우 환경에서
    Tcl/Tk 경로 오류(_tkinter.TclError: Can't find a usable init.tcl)를
    사전에 방지하고 올바른 라이브러리 경로를 자동 탐색하여 보정합니다.
    """
    # 기존 환경변수가 유효하지 않은 디렉터리를 가리키고 있으면 제거
    tcl_env = os.environ.get("TCL_LIBRARY")
    tk_env = os.environ.get("TK_LIBRARY")

    if tcl_env and not (Path(tcl_env) / "init.tcl").is_file():
        os.environ.pop("TCL_LIBRARY", None)
    if tk_env and not (Path(tk_env) / "tk.tcl").is_file():
        os.environ.pop("TK_LIBRARY", None)

    # 환경변수가 없거나 잘못되어 비워진 경우, 현재 파이썬 설치 환경에서 유효한 경로를 탐색
    if "TCL_LIBRARY" not in os.environ or "TK_LIBRARY" not in os.environ:
        search_roots = []
        # PyInstaller 패키징 번들 경로
        if hasattr(sys, "_MEIPASS"):
            search_roots.append(Path(sys._MEIPASS))
        # 가상환경 또는 표준 파이썬 경로
        if hasattr(sys, "base_prefix"):
            search_roots.append(Path(sys.base_prefix))
        search_roots.append(Path(sys.prefix))
        search_roots.append(Path(os.path.dirname(sys.executable)))

        for root in search_roots:
            # Python 표준 윈도우 설치 구조: <root>/tcl/tcl8.6, <root>/tcl/tk8.6
            tcl_dir = root / "tcl"
            if tcl_dir.is_dir():
                for sub in tcl_dir.iterdir():
                    if sub.is_dir():
                        if "TCL_LIBRARY" not in os.environ and (sub / "init.tcl").is_file():
                            os.environ["TCL_LIBRARY"] = str(sub)
                        if "TK_LIBRARY" not in os.environ and (sub / "tk.tcl").is_file():
                            os.environ["TK_LIBRARY"] = str(sub)

            # root 바로 아래 tcl8.x / tk8.x 디렉터리가 있는 경우
            for sub in root.glob("tcl8.*"):
                if sub.is_dir() and (sub / "init.tcl").is_file():
                    os.environ.setdefault("TCL_LIBRARY", str(sub))
            for sub in root.glob("tk8.*"):
                if sub.is_dir() and (sub / "tk.tcl").is_file():
                    os.environ.setdefault("TK_LIBRARY", str(sub))


setup_tcl_tk_environment()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import numpy as np
from openai import OpenAI
from pypdf import PdfReader


# ============================================================
# 2. UAC 및 일반 사용자 계정 안전 디렉터리 (권한 에러 방지)
# ============================================================
def get_safe_app_dir() -> Path:
    """일반 계정(Standard User)에서 UAC 관리자 권한 요청 없이
    100% 읽기/쓰기가 가능한 안전한 사용자 앱 데이터 폴더 경로를 반환합니다.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base_dir = Path(local_app_data)
    else:
        base_dir = Path.home() / ".config"

    app_dir = base_dir / "LMStudioRAG"
    try:
        app_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # 비상 시 홈 디렉터리로 폴백
        app_dir = Path.home() / ".lmstudio_rag"
        app_dir.mkdir(parents=True, exist_ok=True)

    return app_dir


CONFIG_FILE = get_safe_app_dir() / "settings.json"


def load_user_settings() -> dict:
    """사용자 환경 설정을 불러옵니다."""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_user_settings(settings: dict):
    """사용자 환경 설정을 저장합니다 (일반 계정 안전 쓰기)."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================
# 3. 기본 상수 및 UI 디자인
# ============================================================
DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_API_KEY = "lm-studio"

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200
TOP_K = 5
API_TIMEOUT = 45.0  # 초 단위 타임아웃

BG = "#E8EEF5"
PANEL = "#F4F8FB"
ENTRY_BG = "#FFFFFF"
TEXT_BG = "#FFFFFF"
FG = "#1F2937"
MUTED = "#6B7280"
BORDER = "#BCD1E6"
FONT = "Malgun Gothic"


def cosine_similarity(a, b) -> float:
    """두 임베딩 벡터의 코사인 유사도를 계산합니다."""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)

    denominator = np.linalg.norm(a) * np.linalg.norm(b)
    if denominator == 0:
        return 0.0
    return float(np.dot(a, b) / denominator)


def split_text(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """긴 텍스트를 검색용 청크로 분할합니다."""
    text = " ".join(text.split())
    if not text:
        return []

    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= text_len:
            break

        start = max(0, end - overlap)

    return chunks


# ============================================================
# 4. 메인 애플리케이션 클래스
# ============================================================
class LMStudioRAGApp:
    def __init__(self, root: tk.Tk):
        self.root = root

        self.root.title("LM Studio PDF RAG Assistant")
        self.root.geometry("1180x840")
        self.root.minsize(960, 680)
        self.root.configure(bg=BG)

        # 프로그램 실행 및 스레드 생존 플래그 (창 닫힘 후 TclError 방지)
        self.is_running = True
        self.stop_requested = False

        # 스레드 안전 메시지 큐
        self.msg_queue = queue.Queue()

        # RAG 데이터
        self.document_chunks: list[dict] = []
        self.document_embeddings: list[list[float]] = []
        self.pdf_name = ""
        self.is_ready = False

        # 대화 기록
        self.chat_history: list[dict] = []

        # 저장된 설정 불러오기
        self.user_settings = load_user_settings()

        self.setup_style()
        self.create_widgets()

        # 창 닫기 핸들러 연결 (자원 정리 및 안전 종료)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 큐 감시 시작
        self.root.after(100, self.process_queue)

        # 시작 시 모델 목록 자동 조회
        self.root.after(400, self.fetch_models)

    def on_closing(self):
        """창 닫기 시 안전하게 백그라운드 작업을 중단하고 종료합니다."""
        self.is_running = False
        self.stop_requested = True

        # 최근 설정 저장
        try:
            self.user_settings["base_url"] = self.ent_base_url.get().strip()
            self.user_settings["pdf_path"] = self.ent_pdf.get().strip()
            self.user_settings["llm_model"] = self.cmb_llm.get().strip()
            self.user_settings["embedding_model"] = self.cmb_embedding.get().strip()
            save_user_settings(self.user_settings)
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            pass

    def setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", font=(FONT, 10), background=BG, foreground=FG)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TLabel", background=PANEL, foreground=FG)
        style.configure("Header.TLabel", background=PANEL, foreground=FG, font=(FONT, 13, "bold"))
        style.configure("TButton", font=(FONT, 9, "bold"))
        style.configure("Horizontal.TProgressbar", thickness=10)

    def create_widgets(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        # 좌측 패널 (설정 및 제어)
        left = tk.Frame(
            main,
            bg=PANEL,
            width=400,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        left.pack_propagate(False)

        # 우측 패널 (채팅 및 질문창)
        right = tk.Frame(main, bg=BG)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self.build_left_panel(left)
        self.build_right_panel(right)

    def build_left_panel(self, parent):
        frame = tk.Frame(parent, bg=PANEL, padx=16, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="⚙️ LM Studio PDF RAG",
            style="Header.TLabel",
        ).pack(anchor="w", pady=(0, 14))

        # ----------------------------------------------------
        # LM Studio Base URL
        # ----------------------------------------------------
        ttk.Label(frame, text="LM Studio Base URL", style="Panel.TLabel").pack(anchor="w")
        self.ent_base_url = tk.Entry(frame, bg=ENTRY_BG, fg=FG, relief="flat")
        saved_url = self.user_settings.get("base_url", DEFAULT_BASE_URL)
        self.ent_base_url.insert(0, saved_url)
        self.ent_base_url.pack(fill=tk.X, ipady=5, pady=(3, 10))

        # ----------------------------------------------------
        # LLM 모델
        # ----------------------------------------------------
        ttk.Label(frame, text="LLM 모델 (답변 생성용)", style="Panel.TLabel").pack(anchor="w")
        self.cmb_llm = ttk.Combobox(frame, state="normal")
        self.cmb_llm.pack(fill=tk.X, pady=(3, 10))

        # ----------------------------------------------------
        # Embedding 모델
        # ----------------------------------------------------
        ttk.Label(frame, text="Embedding 모델 (벡터 검색용)", style="Panel.TLabel").pack(anchor="w")
        self.cmb_embedding = ttk.Combobox(frame, state="normal")
        self.cmb_embedding.pack(fill=tk.X, pady=(3, 10))

        ttk.Button(
            frame,
            text="🔄 모델 목록 새로고침",
            command=self.fetch_models,
        ).pack(fill=tk.X, pady=(0, 14))

        # ----------------------------------------------------
        # PDF 파일 선택
        # ----------------------------------------------------
        ttk.Label(frame, text="PDF 문서 선택", style="Panel.TLabel").pack(anchor="w")
        pdf_row = tk.Frame(frame, bg=PANEL)
        pdf_row.pack(fill=tk.X, pady=(3, 8))

        self.ent_pdf = tk.Entry(pdf_row, bg=ENTRY_BG, fg=FG, relief="flat")
        saved_pdf = self.user_settings.get("pdf_path", "")
        if saved_pdf and Path(saved_pdf).exists():
            self.ent_pdf.insert(0, saved_pdf)
        self.ent_pdf.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)

        ttk.Button(
            pdf_row,
            text="찾기",
            command=self.select_pdf,
        ).pack(side=tk.RIGHT, padx=(5, 0))

        # ----------------------------------------------------
        # RAG 준비 버튼 및 진행률 바
        # ----------------------------------------------------
        self.btn_index = ttk.Button(
            frame,
            text="📑 PDF 분석 및 RAG 준비",
            command=self.start_indexing,
        )
        self.btn_index.pack(fill=tk.X, ipady=6, pady=(4, 6))

        self.progressbar = ttk.Progressbar(
            frame,
            mode="determinate",
            style="Horizontal.TProgressbar",
        )
        self.progressbar.pack(fill=tk.X, pady=(0, 10))

        # ----------------------------------------------------
        # 작업 제어 (취소 및 초기화 버튼)
        # ----------------------------------------------------
        ctrl_row = tk.Frame(frame, bg=PANEL)
        ctrl_row.pack(fill=tk.X, pady=(0, 12))

        self.btn_stop = ttk.Button(
            ctrl_row,
            text="⏹️ 작업 중지",
            command=self.request_stop,
            state=tk.DISABLED,
        )
        self.btn_stop.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        self.btn_clear_chat = ttk.Button(
            ctrl_row,
            text="🧹 대화 비우기",
            command=self.clear_chat,
        )
        self.btn_clear_chat.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(4, 0))

        # ----------------------------------------------------
        # 안내 문구
        # ----------------------------------------------------
        info = tk.Label(
            frame,
            text=(
                "💡 팁: LM Studio의 Developer 메뉴에서\n"
                "Local Server가 켜져 있는지 확인하세요.\n"
                "(LLM과 Embedding 모델이 각각 로드되어야 합니다.)"
            ),
            bg="#EFF6FF",
            fg="#1E40AF",
            font=(FONT, 8),
            justify="left",
            padx=8,
            pady=8,
        )
        info.pack(fill=tk.X)

        # ----------------------------------------------------
        # 상태 표시창
        # ----------------------------------------------------
        self.lbl_status = tk.Label(
            frame,
            text="LM Studio 연결 대기 중...",
            bg=PANEL,
            fg=MUTED,
            font=(FONT, 9),
            justify="left",
            wraplength=350,
        )
        self.lbl_status.pack(fill=tk.X, pady=(12, 0))

    def build_right_panel(self, parent):
        # 대화창
        self.chat = ScrolledText(
            parent,
            wrap=tk.WORD,
            bg=TEXT_BG,
            fg=FG,
            font=(FONT, 10),
            padx=14,
            pady=14,
            relief="flat",
            state=tk.DISABLED,
        )
        self.chat.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.chat.tag_config("system", foreground="#92400E")
        self.chat.tag_config("user", foreground="#1E40AF", font=(FONT, 10, "bold"))
        self.chat.tag_config("ai", foreground=FG)
        self.chat.tag_config("source", foreground="#047857", font=(FONT, 9))
        self.chat.tag_config("error", foreground="#B91C1C", font=(FONT, 9, "bold"))

        # 질문 입력 영역
        input_row = tk.Frame(parent, bg=BG)
        input_row.pack(fill=tk.X)

        self.ent_question = tk.Entry(
            input_row,
            bg=ENTRY_BG,
            fg=FG,
            font=(FONT, 10),
            relief="flat",
        )
        self.ent_question.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=9)
        self.ent_question.bind("<Return>", lambda event: self.start_asking())

        self.btn_send = ttk.Button(
            input_row,
            text="질문 전송",
            command=self.start_asking,
            state=tk.DISABLED,
        )
        self.btn_send.pack(side=tk.RIGHT, padx=(8, 0), ipadx=8, ipady=5)

    def write_chat(self, text: str, tag: str = "ai"):
        """스레드 안전한 텍스트 뷰 업데이트 (메인 스레드에서만 호출)"""
        if not self.is_running:
            return
        try:
            self.chat.config(state=tk.NORMAL)
            self.chat.insert(tk.END, text, tag)
            self.chat.see(tk.END)
            self.chat.config(state=tk.DISABLED)
        except Exception:
            pass

    def clear_chat(self):
        """채팅창 내용을 비웁니다."""
        try:
            self.chat.config(state=tk.NORMAL)
            self.chat.delete("1.0", tk.END)
            self.chat.config(state=tk.DISABLED)
            self.chat_history.clear()
            self.lbl_status.config(text="대화 기록이 초기화되었습니다.")
        except Exception:
            pass

    def request_stop(self):
        """진행 중인 인덱싱 또는 답변 생성을 중단합니다."""
        self.stop_requested = True
        self.lbl_status.config(text="작업 중단 요청됨...")

    def get_client(self) -> OpenAI:
        """LM Studio OpenAI 호환 API 클라이언트를 타임아웃과 함께 생성합니다."""
        base_url = self.ent_base_url.get().strip().rstrip("/")
        if not base_url:
            base_url = DEFAULT_BASE_URL
        return OpenAI(
            base_url=base_url,
            api_key=DEFAULT_API_KEY,
            timeout=API_TIMEOUT,
        )

    def select_pdf(self):
        """PDF 파일 선택 창 (일반 계정 권한 안전)"""
        initial_dir = None
        current_val = self.ent_pdf.get().strip()
        if current_val and Path(current_val).parent.exists():
            initial_dir = str(Path(current_val).parent)
        else:
            initial_dir = str(Path.home() / "Documents")

        file_path = filedialog.askopenfilename(
            parent=self.root,
            title="PDF 문서 선택",
            initialdir=initial_dir,
            filetypes=[("PDF 파일", "*.pdf"), ("모든 파일", "*.*")],
        )

        if file_path:
            self.ent_pdf.delete(0, tk.END)
            self.ent_pdf.insert(0, file_path)

    # ========================================================
    # 모델 목록 조회
    # ========================================================
    def fetch_models(self):
        self.lbl_status.config(text="LM Studio 모델 목록 확인 중...")

        def worker():
            try:
                client = self.get_client()
                response = client.models.list()
                model_ids = [m.id for m in response.data]
                if self.is_running:
                    self.msg_queue.put(("models", model_ids))
            except Exception as e:
                if self.is_running:
                    err_msg = (
                        "LM Studio 연결 실패:\n"
                        f"{e}\n\n"
                        "확인 사항:\n"
                        "1) LM Studio가 실행 중인가요?\n"
                        "2) Developer 탭에서 'Local Server'가 Started 상태인가요?\n"
                        "3) 포트 번호(기본 1234)가 맞는지 확인하세요."
                    )
                    self.msg_queue.put(("error", err_msg))

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # PDF 인덱싱
    # ========================================================
    def start_indexing(self):
        pdf_path_str = self.ent_pdf.get().strip()
        embedding_model = self.cmb_embedding.get().strip()

        if not pdf_path_str:
            messagebox.showwarning("PDF 파일 필요", "먼저 분석할 PDF 파일을 선택해주세요.")
            return

        pdf_path = Path(pdf_path_str)
        if not pdf_path.exists() or not pdf_path.is_file():
            messagebox.showerror(
                "파일 없음",
                f"선택한 PDF 파일을 찾을 수 없습니다:\n{pdf_path_str}",
            )
            return

        if not embedding_model:
            messagebox.showwarning(
                "Embedding 모델 필요",
                "LM Studio에서 로드된 Embedding 모델을 선택하거나 모델명을 직접 입력해주세요.",
            )
            return

        self.stop_requested = False
        self.btn_index.config(state=tk.DISABLED)
        self.btn_send.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.progressbar["value"] = 0

        self.lbl_status.config(text="PDF 문서 읽기 및 텍스트 추출 중...")
        self.write_chat(f"\n📄 [{pdf_path.name}] 문서 분석을 시작합니다...\n", "system")

        threading.Thread(
            target=self.index_pdf_worker,
            args=(pdf_path, embedding_model),
            daemon=True,
        ).start()

    def index_pdf_worker(self, pdf_path: Path, embedding_model: str):
        try:
            # 1. 일반 계정 및 파일 권한/잠금 예외 안전 처리
            try:
                reader = PdfReader(str(pdf_path))
            except PermissionError:
                raise RuntimeError(
                    f"파일 접근 권한이 없거나 다른 프로그램(Acrobat 등)에서 파일을 잠그고 있습니다:\n{pdf_path}"
                )
            except Exception as e:
                raise RuntimeError(f"PDF 파일을 열 수 없습니다:\n{e}")

            if reader.is_encrypted:
                raise RuntimeError("암호화되어 보호된 PDF 파일입니다. 암호를 해제한 후 시도해주세요.")

            total_pages = len(reader.pages)
            if total_pages == 0:
                raise RuntimeError("PDF 문서의 페이지 수가 0개입니다.")

            chunks = []
            for page_no, page in enumerate(reader.pages, start=1):
                if self.stop_requested:
                    raise RuntimeError("사용자에 의해 작업이 중단되었습니다.")

                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""

                if not text.strip():
                    continue

                page_chunks = split_text(text)
                for chunk in page_chunks:
                    chunks.append({"page": page_no, "text": chunk})

            if not chunks:
                raise RuntimeError(
                    "PDF에서 텍스트를 추출하지 못했습니다.\n"
                    "스캔된 이미지로만 이루어진 PDF인 경우 OCR 처리가 필요합니다."
                )

            # 2. Embedding 생성
            client = self.get_client()
            embeddings = []
            batch_size = 16
            total_chunks = len(chunks)

            for start in range(0, total_chunks, batch_size):
                if self.stop_requested:
                    raise RuntimeError("사용자에 의해 작업이 중단되었습니다.")

                batch = chunks[start : start + batch_size]
                response = client.embeddings.create(
                    model=embedding_model,
                    input=[item["text"] for item in batch],
                )

                embeddings.extend(item.embedding for item in response.data)

                current = min(start + batch_size, total_chunks)
                progress_pct = int((current / total_chunks) * 100)

                self.msg_queue.put(
                    (
                        "index_progress",
                        (progress_pct, f"Embedding 생성 중... ({current}/{total_chunks})"),
                    )
                )

            self.document_chunks = chunks
            self.document_embeddings = embeddings
            self.pdf_name = pdf_path.name
            self.chat_history.clear()
            self.is_ready = True

            self.msg_queue.put(("indexed", total_chunks))

        except Exception as e:
            self.msg_queue.put(("error", f"RAG 준비 실패:\n{e}"))

    # ========================================================
    # RAG 질의응답
    # ========================================================
    def start_asking(self):
        if not self.is_ready:
            messagebox.showwarning("RAG 준비 필요", "먼저 PDF 문서를 분석해주세요.")
            return

        question = self.ent_question.get().strip()
        llm_model = self.cmb_llm.get().strip()
        embedding_model = self.cmb_embedding.get().strip()

        if not question:
            return

        if not llm_model:
            messagebox.showwarning("LLM 모델 필요", "LM Studio에서 LLM 모델을 선택해주세요.")
            return

        if not embedding_model:
            messagebox.showwarning("Embedding 모델 필요", "Embedding 모델을 선택해주세요.")
            return

        self.stop_requested = False
        self.ent_question.delete(0, tk.END)
        self.btn_send.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

        self.write_chat(f"\n\n🙋 질문: {question}\n", "user")
        self.write_chat("🤖 답변: ", "ai")
        self.lbl_status.config(text="관련 문서 검색 및 답변 생성 중...")

        threading.Thread(
            target=self.ask_rag_worker,
            args=(question, llm_model, embedding_model),
            daemon=True,
        ).start()

    def ask_rag_worker(self, question: str, llm_model: str, embedding_model: str):
        try:
            client = self.get_client()

            # 1. 질문 Embedding
            q_res = client.embeddings.create(model=embedding_model, input=question)
            q_emb = q_res.data[0].embedding

            if self.stop_requested:
                raise RuntimeError("사용자에 의해 중단되었습니다.")

            # 2. 코사인 유사도 검색
            scored = []
            for chunk, emb in zip(self.document_chunks, self.document_embeddings):
                score = cosine_similarity(q_emb, emb)
                scored.append((score, chunk))

            top_chunks = sorted(scored, key=lambda x: x[0], reverse=True)[:TOP_K]

            context_parts = []
            pages = []
            for score, chunk in top_chunks:
                pages.append(chunk["page"])
                context_parts.append(
                    f"[PDF {chunk['page']}쪽 / 유사도 {score:.3f}]\n{chunk['text']}"
                )

            context = "\n\n".join(context_parts)

            # 3. 프롬프트 구성
            messages = [
                {
                    "role": "system",
                    "content": (
                        "당신은 PDF 문서 분석 전문 AI 도우미입니다. "
                        "반드시 제공된 PDF 검색 문맥을 최우선 근거로 한국어로 정확하고 친절하게 답변하세요. "
                        "문서에 언급되지 않은 내용은 억지로 지어내지 말고, "
                        "문서에서 확인할 수 없다고 명확히 밝히세요."
                    ),
                }
            ]

            # 최근 6턴의 대화 맥락 포함
            messages.extend(self.chat_history[-6:])

            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"다음은 PDF에서 검색된 관련 내용입니다:\n\n"
                        f"{context}\n\n"
                        f"사용자 질문: {question}"
                    ),
                }
            )

            # 4. LLM 스트리밍 호출
            stream = client.chat.completions.create(
                model=llm_model,
                messages=messages,
                stream=True,
            )

            full_answer = ""
            for chunk in stream:
                if self.stop_requested:
                    self.msg_queue.put(("answer_chunk", "\n[답변 생성이 중단되었습니다.]"))
                    break

                content = chunk.choices[0].delta.content or ""
                if content:
                    full_answer += content
                    self.msg_queue.put(("answer_chunk", content))

            if full_answer:
                self.chat_history.append({"role": "user", "content": question})
                self.chat_history.append({"role": "assistant", "content": full_answer})

            unique_pages = sorted(set(pages))
            page_info = f"{self.pdf_name} / 참고 페이지: " + ", ".join(str(p) for p in unique_pages)
            self.msg_queue.put(("answer_done", page_info))

        except Exception as e:
            self.msg_queue.put(("error", f"답변 생성 실패:\n{e}"))

    # ========================================================
    # UI 이벤트 큐 처리 (메인 스레드 전용)
    # ========================================================
    def process_queue(self):
        if not self.is_running:
            return

        try:
            while not self.msg_queue.empty():
                msg_type, data = self.msg_queue.get_nowait()

                if msg_type == "models":
                    self.cmb_llm["values"] = data
                    self.cmb_embedding["values"] = data

                    saved_llm = self.user_settings.get("llm_model", "")
                    saved_emb = self.user_settings.get("embedding_model", "")

                    if saved_llm in data:
                        self.cmb_llm.set(saved_llm)
                    elif data:
                        self.cmb_llm.set(data[0])

                    if saved_emb in data:
                        self.cmb_embedding.set(saved_emb)
                    elif len(data) >= 2:
                        self.cmb_embedding.set(data[1])
                    elif data:
                        self.cmb_embedding.set(data[0])

                    self.lbl_status.config(
                        text=(
                            f"LM Studio 연결 성공 (모델 {len(data)}개 발견)\n"
                            "LLM과 Embedding 모델을 확인하세요."
                        )
                    )

                elif msg_type == "index_progress":
                    pct, text = data
                    self.progressbar["value"] = pct
                    self.lbl_status.config(text=text)

                elif msg_type == "indexed":
                    self.progressbar["value"] = 100
                    self.btn_index.config(state=tk.NORMAL)
                    self.btn_send.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED)
                    self.lbl_status.config(text=f"RAG 준비 완료 ({data}개 청크)")
                    self.write_chat(
                        f"✅ RAG 준비 완료: {data}개 문서 조각을 인덱싱했습니다.\n"
                        "이제 PDF 내용에 대해 무엇이든 질문하세요.\n",
                        "system",
                    )
                    self.ent_question.focus()

                elif msg_type == "answer_chunk":
                    self.write_chat(data, "ai")

                elif msg_type == "answer_done":
                    self.write_chat(f"\n📚 {data}\n", "source")
                    self.btn_send.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED)
                    self.lbl_status.config(text="답변 완료")
                    self.ent_question.focus()

                elif msg_type == "error":
                    self.btn_index.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED)
                    if self.is_ready:
                        self.btn_send.config(state=tk.NORMAL)
                    self.lbl_status.config(text="오류 발생")
                    self.write_chat(f"\n❌ {data}\n", "error")

        except Exception:
            pass

        # 창이 여전히 실행 중일 때만 다음 주기 등록
        if self.is_running:
            try:
                self.root.after(100, self.process_queue)
            except Exception:
                pass


# ============================================================
# 5. 엔트리포인트 및 고해상도 DPI 설정
# ============================================================
def enable_high_dpi_awareness():
    """Windows 고해상도(HiDPI) 모니터 글자 흐림 방지 (다단계 안전 폴백)"""
    if os.name != "nt":
        return
    try:
        import ctypes

        # Windows 10 Creators Update (1703) 이상: Per-Monitor V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            import ctypes

            # Windows 8.1 이상: Per-Monitor DPI
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                import ctypes

                # Windows Vista / 7 이상: System DPI
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def main():
    enable_high_dpi_awareness()

    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"[치명적 오류] Tkinter/Tcl 초기화 실패: {e}", file=sys.stderr)
        return

    LMStudioRAGApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
