"""LM Studio와 사용자가 직접 선택한 다중 MCP 서버를 연동하는 모던 Python GUI 챗봇 클라이언트

주요 기능:
1. 파일 탐색기를 통한 MCP 파이썬 파일(.py) 직접 선택 (복수 선택 지원)
2. 선택된 파일별 활성화/비활성화 체크박스 및 삭제 기능
3. LM Studio 로컬 서버(http://localhost:1234/v1) 연결 및 모델 목록 자동 감지
4. 실시간 도구(Tool Calling) 실행 로그 및 멀티턴 대화 지원
5. Tkinter 기반 비동기 스레딩 지원 (UI 프리징 없음)
"""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading


# Windows 환경에서 외부/임시 TCL_LIBRARY 환경변수 오염 방지 및 자동 교정
def _fix_tkinter_tcl_environment():
    """Windows에서 다른 프로그램(_MEI 등)에 의해 오염된 TCL_LIBRARY 환경변수를 자동 복구합니다."""
    base_prefix = Path(sys.base_prefix)
    tcl_candidates = [
        base_prefix / "tcl" / "tcl8.6",
        base_prefix / "lib" / "tcl8.6",
        base_prefix / "tcl8.6",
    ]
    tk_candidates = [
        base_prefix / "tcl" / "tk8.6",
        base_prefix / "lib" / "tk8.6",
        base_prefix / "tk8.6",
    ]

    # 올바른 tcl8.6 경로 찾기
    for candidate in tcl_candidates:
        if candidate.exists() and (candidate / "init.tcl").exists():
            os.environ["TCL_LIBRARY"] = str(candidate)
            break
    else:
        curr_tcl = os.environ.get("TCL_LIBRARY", "")
        if "_MEI" in curr_tcl or not Path(curr_tcl).exists():
            os.environ.pop("TCL_LIBRARY", None)

    # 올바른 tk8.6 경로 찾기
    for candidate in tk_candidates:
        if candidate.exists() and (candidate / "tk.tcl").exists():
            os.environ["TK_LIBRARY"] = str(candidate)
            break
    else:
        curr_tk = os.environ.get("TK_LIBRARY", "")
        if "_MEI" in curr_tk or not Path(curr_tk).exists():
            os.environ.pop("TK_LIBRARY", None)


_fix_tkinter_tcl_environment()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI


# 기본 상수 설정
DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"
DEFAULT_API_KEY = "lm-studio"


def mcp_tools_to_openai_tools(mcp_tools: list) -> list[dict]:
    """MCP 도구 명세를 OpenAI 호환 tool 형식으로 변환합니다."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in mcp_tools
    ]


def tool_result_to_text(result) -> str:
    """MCP 도구 결과에서 텍스트 콘텐츠를 추출합니다."""
    texts: list[str] = []
    for item in result.content:
        if isinstance(item, types.TextContent):
            texts.append(item.text)
    return "\n".join(texts) if texts else str(result)


class LMStudioMCPChatbotGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("LM Studio + 다중 MCP 챗봇 (직접 파일 선택)")
        self.root.geometry("1120x760")
        self.root.minsize(920, 620)

        # 상태 변수
        self.conversation_history: list[dict] = []
        self.is_processing = False

        # 사용자가 직접 선택한 MCP 파일 목록
        # 구조: [{"name": "mcp_server.py", "path": "E:/...", "enabled": BooleanVar}]
        self.mcp_files: list[dict] = []

        # 시작 시 현재 작업 디렉토리에 기본 예제 파일이 있으면 편의상 1개 등록해둠
        base_dir = Path(__file__).parent.resolve()
        default_calc = base_dir / "mcp_server.py"
        if default_calc.exists():
            self._add_file_entry(str(default_calc))

        self._setup_style()
        self._build_ui()

        # 시작 시 비동기로 모델 목록 및 도구 목록 조회
        self.root.after(300, self.refresh_models)
        self.root.after(500, self.refresh_mcp_tools)

    def _setup_style(self):
        """UI 스타일 및 색상 구성"""
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        # 모던 다크 테마 색상 팔레트
        self.c_bg = "#1e1e2e"
        self.c_sidebar = "#181825"
        self.c_card = "#24273a"
        self.c_fg = "#cad3f5"
        self.c_accent = "#8aadf4"
        self.c_accent_hover = "#7dc4e4"
        self.c_success = "#a6da95"
        self.c_warning = "#eed49f"
        self.c_danger = "#ed8796"
        self.c_border = "#363a4f"

        self.root.configure(bg=self.c_bg)

        self.style.configure(".", background=self.c_bg, foreground=self.c_fg)
        self.style.configure("TFrame", background=self.c_bg)
        self.style.configure("Sidebar.TFrame", background=self.c_sidebar)
        self.style.configure("Card.TFrame", background=self.c_card, relief="solid", borderwidth=1)

        self.style.configure("TLabel", background=self.c_bg, foreground=self.c_fg, font=("Malgun Gothic", 9))
        self.style.configure("Sidebar.TLabel", background=self.c_sidebar, foreground=self.c_fg, font=("Malgun Gothic", 9))
        self.style.configure("Header.TLabel", background=self.c_sidebar, foreground=self.c_accent, font=("Malgun Gothic", 10, "bold"))

        self.style.configure("TCheckbutton", background=self.c_sidebar, foreground=self.c_fg, font=("Malgun Gothic", 9))
        self.style.map("TCheckbutton", background=[("active", self.c_sidebar)])

    def _build_ui(self):
        """메인 레이아웃 구성"""
        main_container = tk.Frame(self.root, bg=self.c_bg)
        main_container.pack(fill=tk.BOTH, expand=True)

        # 1. 좌측 사이드바 (설정 및 MCP 파일 직접 선택)
        sidebar = tk.Frame(main_container, bg=self.c_sidebar, width=370, padx=12, pady=12)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        # 2. 우측 메인 영역 (대화창)
        content_area = tk.Frame(main_container, bg=self.c_bg, padx=12, pady=12)
        content_area.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self._build_sidebar(sidebar)
        self._build_chat_area(content_area)
        self._build_status_bar()

    def _build_sidebar(self, parent: tk.Frame):
        """좌측 사이드바 UI 구축"""
        # (1) LM Studio 설정 섹션
        lbl_lm_sec = ttk.Label(parent, text="⚙️ LM Studio 로컬 서버", style="Header.TLabel")
        lbl_lm_sec.pack(anchor=tk.W, pady=(0, 6))

        frame_url = tk.Frame(parent, bg=self.c_sidebar)
        frame_url.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(frame_url, text="Base URL:", style="Sidebar.TLabel").pack(side=tk.LEFT)
        self.entry_url = tk.Entry(
            frame_url, bg=self.c_card, fg=self.c_fg, insertbackground=self.c_fg,
            relief="flat", highlightthickness=1, highlightbackground=self.c_border
        )
        self.entry_url.insert(0, DEFAULT_LM_STUDIO_URL)
        self.entry_url.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(6, 0))

        # 모델 선택
        frame_model = tk.Frame(parent, bg=self.c_sidebar)
        frame_model.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(frame_model, text="선택 모델:", style="Sidebar.TLabel").pack(side=tk.LEFT)

        self.combo_model = ttk.Combobox(frame_model, values=["자동 감지 (로드된 모델)"], state="readonly")
        self.combo_model.current(0)
        self.combo_model.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(6, 0))

        # 모델 새로고침 버튼 & 상태
        frame_model_btns = tk.Frame(parent, bg=self.c_sidebar)
        frame_model_btns.pack(fill=tk.X, pady=(0, 10))

        self.lbl_server_status = tk.Label(
            frame_model_btns, text="● 상태 확인 중...", bg=self.c_sidebar,
            fg=self.c_warning, font=("Malgun Gothic", 8)
        )
        self.lbl_server_status.pack(side=tk.LEFT)

        btn_refresh_models = tk.Button(
            frame_model_btns, text="새로고침", command=self.refresh_models,
            bg=self.c_card, fg=self.c_accent, activebackground=self.c_border,
            relief="flat", font=("Malgun Gothic", 8), cursor="hand2"
        )
        btn_refresh_models.pack(side=tk.RIGHT)

        ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=8)

        # (2) MCP 파일 직접 선택 섹션
        lbl_mcp_sec = ttk.Label(parent, text="📂 사용할 MCP 파일 직접 선택", style="Header.TLabel")
        lbl_mcp_sec.pack(anchor=tk.W, pady=(0, 4))

        lbl_mcp_desc = tk.Label(
            parent, text="사용할 MCP 파이썬 파일(.py)을 직접 선택하세요.\n(Ctrl / Shift 키로 여러 파일 동시 선택 가능)",
            bg=self.c_sidebar, fg="#9399b2", font=("Malgun Gothic", 8), wraplength=340, justify=tk.LEFT
        )
        lbl_mcp_desc.pack(anchor=tk.W, pady=(0, 6))

        # 메인 파일 선택 버튼 (눈에 띄는 스타일)
        btn_browse = tk.Button(
            parent, text="📂 MCP 파일 선택... (다중 선택 가능)", command=self.browse_mcp_files,
            bg=self.c_accent, fg="#11111b", activebackground=self.c_accent_hover,
            relief="flat", font=("Malgun Gothic", 9, "bold"), cursor="hand2", pady=5
        )
        btn_browse.pack(fill=tk.X, pady=(0, 6))

        # 파일 목록 보조 조작 버튼들
        frame_tool_btns = tk.Frame(parent, bg=self.c_sidebar)
        frame_tool_btns.pack(fill=tk.X, pady=(0, 6))

        btn_add_more = tk.Button(
            frame_tool_btns, text="+ 파일 추가", command=self.add_mcp_files,
            bg=self.c_card, fg=self.c_success, activebackground=self.c_border,
            relief="flat", font=("Malgun Gothic", 8), cursor="hand2"
        )
        btn_add_more.pack(side=tk.LEFT, padx=(0, 4))

        btn_toggle_all = tk.Button(
            frame_tool_btns, text="전체 선택/해제", command=self.toggle_all_files,
            bg=self.c_card, fg=self.c_fg, activebackground=self.c_border,
            relief="flat", font=("Malgun Gothic", 8), cursor="hand2"
        )
        btn_toggle_all.pack(side=tk.LEFT, padx=(0, 4))

        btn_clear_list = tk.Button(
            frame_tool_btns, text="목록 비우기", command=self.clear_all_files,
            bg=self.c_card, fg=self.c_danger, activebackground=self.c_border,
            relief="flat", font=("Malgun Gothic", 8), cursor="hand2"
        )
        btn_clear_list.pack(side=tk.RIGHT)

        # 선택된 파일 목록 스크롤 프레임
        lbl_file_count = tk.Label(parent, text="선택된 파일 목록:", bg=self.c_sidebar, fg=self.c_fg, font=("Malgun Gothic", 8, "bold"))
        lbl_file_count.pack(anchor=tk.W, pady=(2, 2))

        list_container = tk.Frame(parent, bg=self.c_card, highlightthickness=1, highlightbackground=self.c_border)
        list_container.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        canvas = tk.Canvas(list_container, bg=self.c_card, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        self.frame_file_items = tk.Frame(canvas, bg=self.c_card)

        self.frame_file_items.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.frame_file_items, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._render_file_list()

        ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=8)

        # (3) 활성화된 도구 목록 미리보기
        lbl_tools_sec = ttk.Label(parent, text="🛠️ 감지된 활성 도구(Tools) 목록", style="Header.TLabel")
        lbl_tools_sec.pack(anchor=tk.W, pady=(0, 4))

        self.list_tools_box = tk.Listbox(
            parent, bg=self.c_card, fg=self.c_fg,
            selectbackground=self.c_accent, selectforeground="#11111b",
            relief="flat", highlightthickness=1, highlightbackground=self.c_border,
            font=("Consolas", 8), height=6
        )
        self.list_tools_box.pack(fill=tk.X)

    def _render_file_list(self):
        """선택된 파일 목록을 UI에 렌더링"""
        for widget in self.frame_file_items.winfo_children():
            widget.destroy()

        if not self.mcp_files:
            lbl_empty = tk.Label(
                self.frame_file_items,
                text="선택된 MCP 파일이 없습니다.\n상단의 'MCP 파일 선택' 버튼을 눌러주세요.",
                bg=self.c_card, fg="#6e738d", font=("Malgun Gothic", 8),
                padx=10, pady=15, justify=tk.CENTER
            )
            lbl_empty.pack(fill=tk.X, expand=True)
            return

        for idx, item in enumerate(self.mcp_files):
            row = tk.Frame(self.frame_file_items, bg=self.c_card, padx=6, pady=4)
            row.pack(fill=tk.X, expand=True)

            # 상단: 체크박스 + 삭제버튼
            top_row = tk.Frame(row, bg=self.c_card)
            top_row.pack(fill=tk.X)

            cb = tk.Checkbutton(
                top_row, text=f"📄 {item['name']}", variable=item["enabled"],
                command=self.on_file_toggled,
                bg=self.c_card, fg=self.c_accent, selectcolor=self.c_sidebar,
                activebackground=self.c_card, activeforeground=self.c_accent_hover,
                font=("Malgun Gothic", 9, "bold"), anchor="w"
            )
            cb.pack(side=tk.LEFT, fill=tk.X, expand=True)

            btn_del = tk.Button(
                top_row, text="✕", command=lambda i=idx: self.remove_mcp_file(i),
                bg=self.c_card, fg=self.c_danger, activebackground=self.c_border,
                relief="flat", font=("Malgun Gothic", 8, "bold"), cursor="hand2", padx=4
            )
            btn_del.pack(side=tk.RIGHT)

            # 하단: 전체 경로 표시 (툴팁 역할)
            lbl_path = tk.Label(
                row, text=item["path"], bg=self.c_card, fg="#6e738d",
                font=("Consolas", 7), anchor="w", wraplength=310, justify=tk.LEFT
            )
            lbl_path.pack(fill=tk.X, padx=(22, 0))

    def _build_chat_area(self, parent: tk.Frame):
        """우측 메인 대화 영역 구축"""
        header_frame = tk.Frame(parent, bg=self.c_card, padx=10, pady=8)
        header_frame.pack(fill=tk.X, pady=(0, 8))

        self.lbl_chat_title = tk.Label(
            header_frame, text="💬 LM Studio Assistant with Custom MCP Files",
            bg=self.c_card, fg=self.c_accent, font=("Malgun Gothic", 11, "bold")
        )
        self.lbl_chat_title.pack(side=tk.LEFT)

        self.lbl_active_summary = tk.Label(
            header_frame, text="활성 도구: 0개 준비됨",
            bg=self.c_card, fg=self.c_success, font=("Malgun Gothic", 9)
        )
        self.lbl_active_summary.pack(side=tk.RIGHT)

        # 중앙 대화 내역 (ScrolledText)
        chat_frame = tk.Frame(parent, bg=self.c_card)
        chat_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.txt_chat = tk.Text(
            chat_frame, wrap=tk.WORD, bg=self.c_card, fg=self.c_fg,
            insertbackground=self.c_fg, relief="flat", padx=12, pady=12,
            font=("Malgun Gothic", 10), state=tk.DISABLED
        )
        scrollbar = ttk.Scrollbar(chat_frame, command=self.txt_chat.yview)
        self.txt_chat.configure(yscrollcommand=scrollbar.set)

        self.txt_chat.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 텍스트 태그 스타일
        self.txt_chat.tag_configure("user_tag", foreground=self.c_accent, font=("Malgun Gothic", 10, "bold"))
        self.txt_chat.tag_configure("ai_tag", foreground=self.c_success, font=("Malgun Gothic", 10, "bold"))
        self.txt_chat.tag_configure("tool_tag", foreground=self.c_warning, font=("Consolas", 9))
        self.txt_chat.tag_configure("sys_tag", foreground="#a5adcb", font=("Malgun Gothic", 9, "italic"))

        # 하단 입력 영역
        input_frame = tk.Frame(parent, bg=self.c_bg)
        input_frame.pack(fill=tk.X)

        self.txt_input = tk.Text(
            input_frame, height=3, wrap=tk.WORD, bg=self.c_card, fg=self.c_fg,
            insertbackground=self.c_fg, relief="flat", highlightthickness=1,
            highlightbackground=self.c_border, padx=8, pady=8, font=("Malgun Gothic", 10)
        )
        self.txt_input.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))
        self.txt_input.bind("<Return>", self._on_enter_pressed)
        self.txt_input.bind("<Shift-Return>", lambda e: None)

        btn_panel = tk.Frame(input_frame, bg=self.c_bg)
        btn_panel.pack(side=tk.RIGHT, fill=tk.Y)

        self.btn_send = tk.Button(
            btn_panel, text="전송 ↵", command=self.send_message,
            bg=self.c_accent, fg="#11111b", activebackground=self.c_accent_hover,
            relief="flat", font=("Malgun Gothic", 10, "bold"), width=8, cursor="hand2"
        )
        self.btn_send.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        self.btn_clear = tk.Button(
            btn_panel, text="대화 지우기", command=self.clear_conversation,
            bg=self.c_card, fg="#a5adcb", activebackground=self.c_border,
            relief="flat", font=("Malgun Gothic", 8), width=8, cursor="hand2"
        )
        self.btn_clear.pack(fill=tk.X)

    def _build_status_bar(self):
        """하단 상태 표시줄"""
        self.status_bar = tk.Label(
            self.root, text="준비 완료", bg=self.c_sidebar, fg="#9399b2",
            font=("Malgun Gothic", 8), anchor="w", padx=10, pady=3
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def set_status(self, text: str):
        self.status_bar.config(text=text)

    # -------------------------------------------------------------
    # 대화창 출력 헬퍼
    # -------------------------------------------------------------
    def append_chat(self, role_tag: str, header: str, content: str):
        self.txt_chat.config(state=tk.NORMAL)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.txt_chat.insert(tk.END, f"\n[{header}] ({timestamp})\n", role_tag)
        self.txt_chat.insert(tk.END, f"{content}\n")
        self.txt_chat.see(tk.END)
        self.txt_chat.config(state=tk.DISABLED)

    def append_system_msg(self, msg: str):
        self.txt_chat.config(state=tk.NORMAL)
        self.txt_chat.insert(tk.END, f"\nℹ️ {msg}\n", "sys_tag")
        self.txt_chat.see(tk.END)
        self.txt_chat.config(state=tk.DISABLED)

    def append_tool_log(self, text: str):
        self.txt_chat.config(state=tk.NORMAL)
        self.txt_chat.insert(tk.END, f"  ⚙️ {text}\n", "tool_tag")
        self.txt_chat.see(tk.END)
        self.txt_chat.config(state=tk.DISABLED)

    # -------------------------------------------------------------
    # MCP 파일 직접 선택 및 관리 로직
    # -------------------------------------------------------------
    def _add_file_entry(self, file_path_str: str) -> bool:
        """목록에 파일 엔트리 추가 (중복 방지)"""
        norm_path = str(Path(file_path_str).resolve())
        for item in self.mcp_files:
            if str(Path(item["path"]).resolve()) == norm_path:
                return False

        file_name = Path(norm_path).name
        self.mcp_files.append({
            "name": file_name,
            "path": norm_path,
            "enabled": tk.BooleanVar(value=True),
        })
        return True

    def browse_mcp_files(self):
        """파일 다이얼로그를 띄워 사용자가 MCP 파이썬 파일들을 직접 다중 선택"""
        base_dir = str(Path(__file__).parent.resolve())
        selected_files = filedialog.askopenfilenames(
            title="사용할 MCP 서버 파이썬(.py) 파일을 직접 선택하세요 (다중 선택 가능)",
            initialdir=base_dir,
            filetypes=[("Python Files", "*.py"), ("All Files", "*.*")]
        )
        if not selected_files:
            return

        added_count = 0
        for f in selected_files:
            if self._add_file_entry(f):
                added_count += 1

        self._render_file_list()
        self.refresh_mcp_tools()
        self.append_system_msg(f"{len(selected_files)}개 파일 선택됨 ({added_count}개 신규 추가)")

    def add_mcp_files(self):
        """추가로 파일 선택"""
        self.browse_mcp_files()

    def remove_mcp_file(self, index: int):
        """목록에서 특정 파일 제거"""
        if 0 <= index < len(self.mcp_files):
            removed = self.mcp_files.pop(index)
            self._render_file_list()
            self.refresh_mcp_tools()
            self.append_system_msg(f"MCP 파일 제거됨: {removed['name']}")

    def clear_all_files(self):
        """파일 목록 전체 비우기"""
        if not self.mcp_files:
            return
        self.mcp_files.clear()
        self._render_file_list()
        self.refresh_mcp_tools()
        self.append_system_msg("선택된 MCP 파일 목록을 모두 비웠습니다.")

    def toggle_all_files(self):
        """모든 파일의 체크 상태를 일괄 토글"""
        if not self.mcp_files:
            return
        # 만약 하나라도 꺼져있으면 전체 켜기, 모두 켜져있으면 전체 끄기
        all_enabled = all(item["enabled"].get() for item in self.mcp_files)
        new_state = not all_enabled
        for item in self.mcp_files:
            item["enabled"].set(new_state)
        self.refresh_mcp_tools()

    def on_file_toggled(self):
        """체크박스 변경 시 즉시 도구 목록 갱신"""
        self.refresh_mcp_tools()

    # -------------------------------------------------------------
    # 비동기 새로고침 (LM Studio 모델 & 도구 목록)
    # -------------------------------------------------------------
    def refresh_models(self):
        self.lbl_server_status.config(text="● 모델 확인 중...", fg=self.c_warning)
        threading.Thread(target=self._worker_refresh_models, daemon=True).start()

    def _worker_refresh_models(self):
        url = self.entry_url.get().strip() or DEFAULT_LM_STUDIO_URL

        async def _fetch():
            client = AsyncOpenAI(base_url=url, api_key=DEFAULT_API_KEY)
            return await client.models.list()

        try:
            res = asyncio.run(_fetch())
            model_ids = [m.id for m in res.data]
            self.root.after(0, self._update_model_combobox, model_ids, None)
        except Exception as e:
            self.root.after(0, self._update_model_combobox, [], str(e))

    def _update_model_combobox(self, model_ids: list[str], error: str | None):
        if error:
            self.lbl_server_status.config(text="● 서버 연결 실패", fg=self.c_danger)
            self.combo_model.config(values=["연결 실패 (LM Studio 확인 필요)"])
            self.combo_model.current(0)
            self.set_status(f"LM Studio 연결 실패: {error}")
        else:
            self.lbl_server_status.config(text=f"● 연결됨 ({len(model_ids)}개 모델)", fg=self.c_success)
            values = ["자동 감지 (로드된 모델)"] + model_ids
            self.combo_model.config(values=values)
            self.combo_model.current(0)
            self.set_status(f"LM Studio 연결 성공 ({len(model_ids)}개 모델 발견)")

    def refresh_mcp_tools(self):
        """체크된 파일들의 도구 목록 조회"""
        enabled_files = [f for f in self.mcp_files if f["enabled"].get()]
        threading.Thread(target=self._worker_refresh_mcp_tools, args=(enabled_files,), daemon=True).start()

    def _worker_refresh_mcp_tools(self, files: list[dict]):
        async def _fetch_tools():
            all_tools = []
            async with AsyncExitStack() as stack:
                for f_item in files:
                    f_path = f_item["path"]
                    if not Path(f_path).exists():
                        continue
                    params = StdioServerParameters(command=sys.executable, args=[f_path])
                    try:
                        read_s, write_s = await stack.enter_async_context(stdio_client(params))
                        session = await stack.enter_async_context(ClientSession(read_s, write_s))
                        await session.initialize()
                        res = await session.list_tools()
                        for t in res.tools:
                            all_tools.append({
                                "file_name": f_item["name"],
                                "name": t.name,
                                "description": t.description or "",
                            })
                    except Exception as e:
                        print(f"도구 조회 실패 ({f_item['name']}): {e}")
            return all_tools

        try:
            tools = asyncio.run(_fetch_tools())
            self.root.after(0, self._update_tools_ui, tools)
        except Exception as e:
            print(f"도구 목록 조회 오류: {e}")

    def _update_tools_ui(self, tools: list[dict]):
        self.list_tools_box.delete(0, tk.END)
        for t in tools:
            self.list_tools_box.insert(tk.END, f"[{t['file_name']}] {t['name']}: {t['description']}")

        self.lbl_active_summary.config(text=f"활성 도구: {len(tools)}개 준비됨")

    # -------------------------------------------------------------
    # 대화 전송 및 실행 로직
    # -------------------------------------------------------------
    def _on_enter_pressed(self, event):
        if not event.state & 0x1:
            self.send_message()
            return "break"

    def send_message(self):
        if self.is_processing:
            return

        question = self.txt_input.get("1.0", tk.END).strip()
        if not question:
            return

        # 활성화된 MCP 파일 목록 추출
        enabled_files = [
            {"name": f["name"], "path": f["path"]}
            for f in self.mcp_files
            if f["enabled"].get()
        ]

        self.txt_input.delete("1.0", tk.END)
        self.append_chat("user_tag", "사용자", question)

        selected_model = self.combo_model.get()
        if "자동 감지" in selected_model or "연결 실패" in selected_model:
            model_to_use = ""
        else:
            model_to_use = selected_model

        url = self.entry_url.get().strip() or DEFAULT_LM_STUDIO_URL

        self.is_processing = True
        self.btn_send.config(state=tk.DISABLED, text="처리 중...")
        self.set_status("LM Studio 및 선택된 MCP 파일들과 통신 중...")

        threading.Thread(
            target=self._worker_chat_turn,
            args=(question, enabled_files, model_to_use, url),
            daemon=True
        ).start()

    def _worker_chat_turn(
        self,
        question: str,
        enabled_files: list[dict],
        target_model: str,
        base_url: str
    ):
        async def _run():
            lm_client = AsyncOpenAI(base_url=base_url, api_key=DEFAULT_API_KEY)

            # 1. 모델 결정
            model_name = target_model
            if not model_name:
                try:
                    models_resp = await lm_client.models.list()
                    if models_resp.data:
                        model_name = models_resp.data[0].id
                except Exception as e:
                    self.root.after(0, self.append_system_msg, f"LM Studio 모델 감지 실패: {e}")
                    return

            if not model_name:
                model_name = "local-model"

            self.root.after(0, self.set_status, f"선택된 모델: {model_name} / MCP 파일 초기화 중...")

            # 2. 다중 MCP 파일 세션 연결 (AsyncExitStack 사용)
            async with AsyncExitStack() as stack:
                tool_to_session = {}
                openai_tools = []

                for f_item in enabled_files:
                    f_path = f_item["path"]
                    if not Path(f_path).exists():
                        continue

                    params = StdioServerParameters(command=sys.executable, args=[f_path])
                    try:
                        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
                        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                        await session.initialize()

                        tools_resp = await session.list_tools()
                        for t in tools_resp.tools:
                            tool_to_session[t.name] = (session, f_item["name"])
                            openai_tools.append({
                                "type": "function",
                                "function": {
                                    "name": t.name,
                                    "description": t.description or "",
                                    "parameters": t.inputSchema,
                                }
                            })
                    except Exception as e:
                        self.root.after(
                            0, self.append_system_msg,
                            f"MCP 파일 실행 오류 ({f_item['name']}): {e}"
                        )

                # 대화 메시지 구성
                if not self.conversation_history:
                    self.conversation_history.append({
                        "role": "system",
                        "content": "당신은 사용자를 돕는 똑똑한 AI 비서입니다. 제공된 도구(함수)가 필요한 질문에는 반드시 적절한 도구를 사용하세요.",
                    })

                self.conversation_history.append({"role": "user", "content": question})

                self.root.after(0, self.set_status, f"{model_name} 모델의 첫 번째 응답 생성 대기 중...")

                # 3. 1차 요청
                try:
                    kwargs = {
                        "model": model_name,
                        "messages": self.conversation_history,
                    }
                    if openai_tools:
                        kwargs["tools"] = openai_tools

                    first_response = await lm_client.chat.completions.create(**kwargs)
                except Exception as e:
                    self.root.after(0, self.append_system_msg, f"LM Studio 통신 실패: {e}")
                    return

                assistant_msg = first_response.choices[0].message
                self.conversation_history.append(assistant_msg.model_dump(exclude_none=True))

                tool_calls = assistant_msg.tool_calls or []

                # 도구 호출이 없는 경우 곧바로 완료
                if not tool_calls:
                    reply_text = assistant_msg.content or "(답변 없음)"
                    self.root.after(0, self.append_chat, "ai_tag", f"AI ({model_name})", reply_text)
                    return

                # 4. 도구 실행
                self.root.after(0, self.set_status, f"{len(tool_calls)}개의 도구 호출 실행 중...")

                for call in tool_calls:
                    t_name = call.function.name
                    raw_args = call.function.arguments

                    if isinstance(raw_args, str):
                        try:
                            parsed_args = json.loads(raw_args)
                        except json.JSONDecodeError:
                            parsed_args = {}
                    else:
                        parsed_args = raw_args or {}

                    # 라우팅
                    if t_name in tool_to_session:
                        session, f_name = tool_to_session[t_name]
                        self.root.after(
                            0, self.append_tool_log,
                            f"도구 호출: [{f_name}] {t_name}({parsed_args})"
                        )

                        try:
                            res = await session.call_tool(t_name, arguments=parsed_args)
                            res_text = tool_result_to_text(res)
                        except Exception as e:
                            res_text = f"도구 실행 오류: {e}"

                        self.root.after(0, self.append_tool_log, f"실행 결과: {res_text}")
                    else:
                        res_text = f"해당 도구({t_name})를 제공하는 활성화된 MCP 파일이 없습니다."
                        self.root.after(0, self.append_tool_log, res_text)

                    self.conversation_history.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": res_text,
                    })

                # 5. 도구 결과를 반영한 2차 최종 요청
                self.root.after(0, self.set_status, "도구 결과를 바탕으로 최종 답변 생성 중...")

                try:
                    kwargs_final = {
                        "model": model_name,
                        "messages": self.conversation_history,
                    }
                    if openai_tools:
                        kwargs_final["tools"] = openai_tools

                    final_response = await lm_client.chat.completions.create(**kwargs_final)
                    final_reply = final_response.choices[0].message.content or ""
                    self.conversation_history.append({
                        "role": "assistant",
                        "content": final_reply
                    })
                    self.root.after(0, self.append_chat, "ai_tag", f"AI ({model_name})", final_reply)
                except Exception as e:
                    self.root.after(0, self.append_system_msg, f"최종 답변 생성 실패: {e}")

        try:
            asyncio.run(_run())
        finally:
            self.root.after(0, self._finish_chat_turn)

    def _finish_chat_turn(self):
        self.is_processing = False
        self.btn_send.config(state=tk.NORMAL, text="전송 ↵")
        self.set_status("준비 완료")

    def clear_conversation(self):
        self.conversation_history.clear()
        self.txt_chat.config(state=tk.NORMAL)
        self.txt_chat.delete("1.0", tk.END)
        self.txt_chat.config(state=tk.DISABLED)
        self.append_system_msg("대화 기록이 초기화되었습니다.")


def main():
    root = tk.Tk()
    app = LMStudioMCPChatbotGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
