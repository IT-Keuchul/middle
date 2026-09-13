import os

# ============================================================
# Windows / PyInstaller / 임시 실행기에서 남긴 Tcl/Tk 환경변수 제거
# 반드시 tkinter import보다 먼저 실행해야 합니다.
# ============================================================
os.environ.pop("TCL_LIBRARY", None)
os.environ.pop("TK_LIBRARY", None)

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import numpy as np
from openai import OpenAI
from pypdf import PdfReader


# ============================================================
# LM Studio 전용 PDF RAG GUI
#
# 설치:
#   uv add openai pypdf numpy
#
# LM Studio 준비:
#   1) LLM 모델 로드
#   2) Embedding 모델 로드
#   3) Developer -> Local Server 시작
#   4) 기본 API 주소: http://localhost:1234/v1
# ============================================================

DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_API_KEY = "lm-studio"

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200
TOP_K = 5


# ============================================================
# UI 색상
# ============================================================
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
    overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """긴 텍스트를 RAG 검색용 Chunk로 분할합니다."""

    text = " ".join(text.split())

    if not text:
        return []

    chunks = []
    start = 0

    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])

        if end >= len(text):
            break

        start = max(0, end - overlap)

    return chunks


class LMStudioRAGApp:
    def __init__(self, root: tk.Tk):
        self.root = root

        self.root.title("LM Studio PDF RAG Assistant")
        self.root.geometry("1180x820")
        self.root.minsize(960, 650)
        self.root.configure(bg=BG)

        # 스레드에서 GUI로 결과 전달
        self.msg_queue = queue.Queue()

        # RAG 데이터
        self.document_chunks: list[dict] = []
        self.document_embeddings: list[list[float]] = []

        self.pdf_name = ""
        self.is_ready = False

        # 멀티턴 대화 기록
        self.chat_history: list[dict] = []

        self.setup_style()
        self.create_widgets()

        # Queue 감시
        self.root.after(100, self.process_queue)

        # 시작 시 모델 목록 자동 조회
        self.root.after(500, self.fetch_models)

    # ========================================================
    # UI 설정
    # ========================================================
    def setup_style(self):
        style = ttk.Style()

        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            ".",
            font=(FONT, 10),
            background=BG,
            foreground=FG
        )

        style.configure(
            "TFrame",
            background=BG
        )

        style.configure(
            "Panel.TLabel",
            background=PANEL,
            foreground=FG
        )

        style.configure(
            "Header.TLabel",
            background=PANEL,
            foreground=FG,
            font=(FONT, 13, "bold")
        )

        style.configure(
            "TButton",
            font=(FONT, 9, "bold")
        )

    def create_widgets(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        # 좌측 설정 영역
        left = tk.Frame(
            main,
            bg=PANEL,
            width=390,
            highlightbackground=BORDER,
            highlightthickness=1
        )
        left.pack(
            side=tk.LEFT,
            fill=tk.Y,
            padx=(0, 12)
        )
        left.pack_propagate(False)

        # 우측 채팅 영역
        right = tk.Frame(
            main,
            bg=BG
        )
        right.pack(
            side=tk.RIGHT,
            fill=tk.BOTH,
            expand=True
        )

        self.build_left_panel(left)
        self.build_right_panel(right)

    def build_left_panel(self, parent):
        frame = tk.Frame(
            parent,
            bg=PANEL,
            padx=16,
            pady=16
        )
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="⚙️ LM Studio PDF RAG",
            style="Header.TLabel"
        ).pack(
            anchor="w",
            pady=(0, 14)
        )

        # ----------------------------------------------------
        # LM Studio URL
        # ----------------------------------------------------
        ttk.Label(
            frame,
            text="LM Studio Base URL",
            style="Panel.TLabel"
        ).pack(anchor="w")

        self.ent_base_url = tk.Entry(
            frame,
            bg=ENTRY_BG,
            fg=FG,
            relief="flat"
        )

        self.ent_base_url.insert(
            0,
            DEFAULT_BASE_URL
        )

        self.ent_base_url.pack(
            fill=tk.X,
            ipady=5,
            pady=(3, 10)
        )

        # ----------------------------------------------------
        # LLM 모델
        # ----------------------------------------------------
        ttk.Label(
            frame,
            text="LLM 모델",
            style="Panel.TLabel"
        ).pack(anchor="w")

        self.cmb_llm = ttk.Combobox(
            frame,
            state="normal"
        )

        self.cmb_llm.pack(
            fill=tk.X,
            pady=(3, 10)
        )

        # ----------------------------------------------------
        # Embedding 모델
        # ----------------------------------------------------
        ttk.Label(
            frame,
            text="Embedding 모델",
            style="Panel.TLabel"
        ).pack(anchor="w")

        self.cmb_embedding = ttk.Combobox(
            frame,
            state="normal"
        )

        self.cmb_embedding.pack(
            fill=tk.X,
            pady=(3, 10)
        )

        ttk.Button(
            frame,
            text="모델 목록 새로고침",
            command=self.fetch_models
        ).pack(
            fill=tk.X,
            pady=(0, 14)
        )

        # ----------------------------------------------------
        # PDF 선택
        # ----------------------------------------------------
        ttk.Label(
            frame,
            text="PDF 파일",
            style="Panel.TLabel"
        ).pack(anchor="w")

        pdf_row = tk.Frame(
            frame,
            bg=PANEL
        )
        pdf_row.pack(
            fill=tk.X,
            pady=(3, 8)
        )

        self.ent_pdf = tk.Entry(
            pdf_row,
            bg=ENTRY_BG,
            fg=FG,
            relief="flat"
        )

        self.ent_pdf.pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
            ipady=5
        )

        ttk.Button(
            pdf_row,
            text="찾기",
            command=self.select_pdf
        ).pack(
            side=tk.RIGHT,
            padx=(5, 0)
        )

        # ----------------------------------------------------
        # RAG 준비 버튼
        # ----------------------------------------------------
        self.btn_index = ttk.Button(
            frame,
            text="PDF 분석 및 RAG 준비",
            command=self.start_indexing
        )

        self.btn_index.pack(
            fill=tk.X,
            ipady=6,
            pady=(4, 12)
        )

        # ----------------------------------------------------
        # RAG 흐름 안내
        # ----------------------------------------------------
        info = tk.Label(
            frame,
            text=(
                "PDF → 텍스트 추출 → Chunk 분할\n"
                "→ LM Studio Embedding → 유사도 검색\n"
                "→ 관련 Chunk + 질문 → LM Studio LLM"
            ),
            bg="#EFF6FF",
            fg="#1E40AF",
            font=(FONT, 9),
            justify="left",
            padx=10,
            pady=10
        )

        info.pack(fill=tk.X)

        # ----------------------------------------------------
        # 상태 표시
        # ----------------------------------------------------
        self.lbl_status = tk.Label(
            frame,
            text="LM Studio 연결 확인 중...",
            bg=PANEL,
            fg=MUTED,
            font=(FONT, 9),
            justify="left",
            wraplength=340
        )

        self.lbl_status.pack(
            fill=tk.X,
            pady=(15, 0)
        )

    def build_right_panel(self, parent):
        # ----------------------------------------------------
        # 대화창
        # ----------------------------------------------------
        self.chat = ScrolledText(
            parent,
            wrap=tk.WORD,
            bg=TEXT_BG,
            fg=FG,
            font=(FONT, 10),
            padx=14,
            pady=14,
            relief="flat",
            state=tk.DISABLED
        )

        self.chat.pack(
            fill=tk.BOTH,
            expand=True,
            pady=(0, 10)
        )

        self.chat.tag_config(
            "system",
            foreground="#92400E"
        )

        self.chat.tag_config(
            "user",
            foreground="#1E40AF",
            font=(FONT, 10, "bold")
        )

        self.chat.tag_config(
            "ai",
            foreground=FG
        )

        self.chat.tag_config(
            "source",
            foreground="#047857",
            font=(FONT, 9)
        )

        self.chat.tag_config(
            "error",
            foreground="#B91C1C",
            font=(FONT, 9, "bold")
        )

        # ----------------------------------------------------
        # 질문 입력 영역
        # ----------------------------------------------------
        input_row = tk.Frame(
            parent,
            bg=BG
        )

        input_row.pack(
            fill=tk.X
        )

        self.ent_question = tk.Entry(
            input_row,
            bg=ENTRY_BG,
            fg=FG,
            font=(FONT, 10),
            relief="flat"
        )

        self.ent_question.pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
            ipady=9
        )

        self.ent_question.bind(
            "<Return>",
            lambda event: self.start_asking()
        )

        self.btn_send = ttk.Button(
            input_row,
            text="질문 전송",
            command=self.start_asking,
            state=tk.DISABLED
        )

        self.btn_send.pack(
            side=tk.RIGHT,
            padx=(8, 0),
            ipadx=8,
            ipady=5
        )

    # ========================================================
    # 공통 함수
    # ========================================================
    def write_chat(
        self,
        text: str,
        tag: str = "ai"
    ):
        self.chat.config(
            state=tk.NORMAL
        )

        self.chat.insert(
            tk.END,
            text,
            tag
        )

        self.chat.see(
            tk.END
        )

        self.chat.config(
            state=tk.DISABLED
        )

    def get_client(self) -> OpenAI:
        """LM Studio OpenAI 호환 API 클라이언트를 생성합니다."""

        base_url = (
            self.ent_base_url
            .get()
            .strip()
            .rstrip("/")
        )

        return OpenAI(
            base_url=base_url,
            api_key=DEFAULT_API_KEY
        )

    def select_pdf(self):
        """PDF 파일 선택 창을 엽니다."""

        file_path = filedialog.askopenfilename(
            title="PDF 파일 선택",
            filetypes=[
                ("PDF 파일", "*.pdf"),
                ("모든 파일", "*.*")
            ]
        )

        if file_path:
            self.ent_pdf.delete(
                0,
                tk.END
            )

            self.ent_pdf.insert(
                0,
                file_path
            )

    # ========================================================
    # LM Studio 모델 목록 조회
    # ========================================================
    def fetch_models(self):
        self.lbl_status.config(
            text="LM Studio 모델 목록 확인 중..."
        )

        def worker():
            try:
                client = self.get_client()

                response = (
                    client
                    .models
                    .list()
                )

                model_ids = [
                    model.id
                    for model
                    in response.data
                ]

                self.msg_queue.put(
                    (
                        "models",
                        model_ids
                    )
                )

            except Exception as e:
                self.msg_queue.put(
                    (
                        "error",
                        "LM Studio 연결 실패:\n"
                        f"{e}\n\n"
                        "LM Studio의 Developer에서 "
                        "Local Server가 실행 중인지 확인하세요."
                    )
                )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    # ========================================================
    # PDF -> Chunk -> Embedding
    # ========================================================
    def start_indexing(self):
        pdf_path = Path(
            self.ent_pdf.get().strip()
        )

        embedding_model = (
            self.cmb_embedding
            .get()
            .strip()
        )

        if not pdf_path.exists():
            messagebox.showwarning(
                "PDF 필요",
                "PDF 파일을 선택해주세요."
            )
            return

        if not embedding_model:
            messagebox.showwarning(
                "Embedding 모델 필요",
                "LM Studio에서 Embedding 모델을 로드하고 "
                "모델명을 선택하거나 직접 입력해주세요."
            )
            return

        self.btn_index.config(
            state=tk.DISABLED
        )

        self.btn_send.config(
            state=tk.DISABLED
        )

        self.lbl_status.config(
            text="PDF 분석 및 Embedding 생성 중..."
        )

        self.write_chat(
            f"\n📄 {pdf_path.name} 분석을 시작합니다.\n",
            "system"
        )

        threading.Thread(
            target=self.index_pdf,
            args=(
                pdf_path,
                embedding_model
            ),
            daemon=True
        ).start()

    def index_pdf(
        self,
        pdf_path: Path,
        embedding_model: str
    ):
        try:
            # ------------------------------------------------
            # 1. PDF 텍스트 추출
            # ------------------------------------------------
            reader = PdfReader(
                str(pdf_path)
            )

            chunks = []

            for page_no, page in enumerate(
                reader.pages,
                start=1
            ):
                text = (
                    page.extract_text()
                    or ""
                )

                if not text.strip():
                    continue

                page_chunks = split_text(
                    text
                )

                for chunk in page_chunks:
                    chunks.append(
                        {
                            "page": page_no,
                            "text": chunk
                        }
                    )

            if not chunks:
                raise RuntimeError(
                    "PDF에서 텍스트를 추출하지 못했습니다.\n"
                    "스캔 PDF라면 OCR 처리가 필요합니다."
                )

            # ------------------------------------------------
            # 2. LM Studio Embedding API 호출
            # ------------------------------------------------
            client = self.get_client()

            embeddings = []

            batch_size = 16

            for start in range(
                0,
                len(chunks),
                batch_size
            ):
                batch = chunks[
                    start:start + batch_size
                ]

                response = (
                    client
                    .embeddings
                    .create(
                        model=embedding_model,
                        input=[
                            item["text"]
                            for item
                            in batch
                        ]
                    )
                )

                embeddings.extend(
                    item.embedding
                    for item
                    in response.data
                )

                current = min(
                    start + batch_size,
                    len(chunks)
                )

                self.msg_queue.put(
                    (
                        "status",
                        f"Embedding 생성 중... "
                        f"{current}/{len(chunks)}"
                    )
                )

            self.document_chunks = chunks
            self.document_embeddings = embeddings

            self.pdf_name = pdf_path.name
            self.chat_history.clear()
            self.is_ready = True

            self.msg_queue.put(
                (
                    "indexed",
                    len(chunks)
                )
            )

        except Exception as e:
            self.msg_queue.put(
                (
                    "error",
                    f"RAG 준비 실패:\n{e}"
                )
            )

    # ========================================================
    # RAG 질문 처리
    # ========================================================
    def start_asking(self):
        if not self.is_ready:
            messagebox.showwarning(
                "RAG 준비 필요",
                "먼저 PDF 분석 및 RAG 준비를 실행해주세요."
            )
            return

        question = (
            self.ent_question
            .get()
            .strip()
        )

        llm_model = (
            self.cmb_llm
            .get()
            .strip()
        )

        embedding_model = (
            self.cmb_embedding
            .get()
            .strip()
        )

        if not question:
            return

        if not llm_model:
            messagebox.showwarning(
                "LLM 모델 필요",
                "LM Studio에서 LLM 모델을 선택하거나 "
                "직접 입력해주세요."
            )
            return

        if not embedding_model:
            messagebox.showwarning(
                "Embedding 모델 필요",
                "Embedding 모델을 선택해주세요."
            )
            return

        self.ent_question.delete(
            0,
            tk.END
        )

        self.btn_send.config(
            state=tk.DISABLED
        )

        self.write_chat(
            f"\n\n🙋 질문: {question}\n",
            "user"
        )

        self.write_chat(
            "🤖 답변: ",
            "ai"
        )

        self.lbl_status.config(
            text="관련 문서 검색 및 답변 생성 중..."
        )

        threading.Thread(
            target=self.ask_rag,
            args=(
                question,
                llm_model,
                embedding_model
            ),
            daemon=True
        ).start()

    def ask_rag(
        self,
        question: str,
        llm_model: str,
        embedding_model: str
    ):
        try:
            client = self.get_client()

            # ------------------------------------------------
            # 1. 질문 Embedding
            # ------------------------------------------------
            question_response = (
                client
                .embeddings
                .create(
                    model=embedding_model,
                    input=question
                )
            )

            question_embedding = (
                question_response
                .data[0]
                .embedding
            )

            # ------------------------------------------------
            # 2. 모든 Chunk와 유사도 비교
            # ------------------------------------------------
            scored_chunks = []

            for chunk, embedding in zip(
                self.document_chunks,
                self.document_embeddings
            ):
                score = cosine_similarity(
                    question_embedding,
                    embedding
                )

                scored_chunks.append(
                    (
                        score,
                        chunk
                    )
                )

            # ------------------------------------------------
            # 3. 상위 TOP_K 검색
            # ------------------------------------------------
            top_chunks = sorted(
                scored_chunks,
                key=lambda item: item[0],
                reverse=True
            )[:TOP_K]

            context_parts = []
            pages = []

            for score, chunk in top_chunks:
                pages.append(
                    chunk["page"]
                )

                context_parts.append(
                    f"[PDF {chunk['page']}쪽 / "
                    f"유사도 {score:.3f}]\n"
                    f"{chunk['text']}"
                )

            context = "\n\n".join(
                context_parts
            )

            # ------------------------------------------------
            # 4. LLM 메시지 구성
            # ------------------------------------------------
            messages = [
                {
                    "role": "system",
                    "content": (
                        "당신은 PDF 문서 분석 도우미입니다. "
                        "반드시 제공된 PDF 검색 결과를 우선 근거로 "
                        "한국어로 답하세요. "
                        "문서에서 확인할 수 없는 내용은 "
                        "추측하지 말고 확인할 수 없다고 말하세요."
                    )
                }
            ]

            # 최근 대화 일부 유지
            messages.extend(
                self.chat_history[-6:]
            )

            messages.append(
                {
                    "role": "user",
                    "content": (
                        "다음은 PDF에서 검색된 관련 내용입니다.\n\n"
                        f"{context}\n\n"
                        f"사용자 질문: {question}"
                    )
                }
            )

            # ------------------------------------------------
            # 5. LM Studio LLM 스트리밍
            # ------------------------------------------------
            stream = (
                client
                .chat
                .completions
                .create(
                    model=llm_model,
                    messages=messages,
                    stream=True
                )
            )

            full_answer = ""

            for chunk in stream:
                content = (
                    chunk
                    .choices[0]
                    .delta
                    .content
                    or ""
                )

                if content:
                    full_answer += content

                    self.msg_queue.put(
                        (
                            "answer_chunk",
                            content
                        )
                    )

            # ------------------------------------------------
            # 6. 대화 기록 저장
            # ------------------------------------------------
            self.chat_history.append(
                {
                    "role": "user",
                    "content": question
                }
            )

            self.chat_history.append(
                {
                    "role": "assistant",
                    "content": full_answer
                }
            )

            unique_pages = sorted(
                set(pages)
            )

            self.msg_queue.put(
                (
                    "answer_done",
                    f"{self.pdf_name} / 참고 페이지: "
                    + ", ".join(
                        str(page)
                        for page
                        in unique_pages
                    )
                )
            )

        except Exception as e:
            self.msg_queue.put(
                (
                    "error",
                    f"답변 생성 실패:\n{e}"
                )
            )

    # ========================================================
    # Queue 처리
    # ========================================================
    def process_queue(self):
        while not self.msg_queue.empty():
            msg_type, data = (
                self.msg_queue.get()
            )

            if msg_type == "models":
                self.cmb_llm["values"] = data
                self.cmb_embedding["values"] = data

                if data:
                    self.cmb_llm.set(
                        data[0]
                    )

                    # 모델 목록만으로 LLM/Embedding 타입을
                    # 확실히 판별하기 어려우므로 사용자가
                    # Embedding 모델을 직접 선택하도록 합니다.
                    if len(data) >= 2:
                        self.cmb_embedding.set(
                            data[1]
                        )
                    else:
                        self.cmb_embedding.set(
                            ""
                        )

                self.lbl_status.config(
                    text=(
                        f"LM Studio 연결 성공 / "
                        f"모델 {len(data)}개 발견\n"
                        "LLM과 Embedding 모델을 각각 선택하세요."
                    )
                )

            elif msg_type == "status":
                self.lbl_status.config(
                    text=data
                )

            elif msg_type == "indexed":
                self.btn_index.config(
                    state=tk.NORMAL
                )

                self.btn_send.config(
                    state=tk.NORMAL
                )

                self.lbl_status.config(
                    text=f"RAG 준비 완료 / {data}개 Chunk"
                )

                self.write_chat(
                    f"✅ RAG 준비 완료: "
                    f"{data}개 문서 조각을 인덱싱했습니다.\n"
                    "이제 PDF 내용에 대해 질문하세요.\n",
                    "system"
                )

                self.ent_question.focus()

            elif msg_type == "answer_chunk":
                self.write_chat(
                    data,
                    "ai"
                )

            elif msg_type == "answer_done":
                self.write_chat(
                    f"\n📚 {data}\n",
                    "source"
                )

                self.btn_send.config(
                    state=tk.NORMAL
                )

                self.lbl_status.config(
                    text="RAG 준비 완료"
                )

                self.ent_question.focus()

            elif msg_type == "error":
                self.btn_index.config(
                    state=tk.NORMAL
                )

                if self.is_ready:
                    self.btn_send.config(
                        state=tk.NORMAL
                    )

                self.lbl_status.config(
                    text=data
                )

                self.write_chat(
                    f"\n❌ {data}\n",
                    "error"
                )

        self.root.after(
            100,
            self.process_queue
        )


def main():
    root = tk.Tk()

    # Windows 고해상도 모니터에서 글자 흐림 완화
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    LMStudioRAGApp(root)

    root.mainloop()


if __name__ == "__main__":
    main()
