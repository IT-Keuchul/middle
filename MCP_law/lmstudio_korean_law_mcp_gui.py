import os
import sys

# PyInstaller 기반 상위 런처에서 상속되는 Tcl/Tk 환경변수 오염 방지 및 경로 보정
for _var in ("TCL_LIBRARY", "TK_LIBRARY"):
    if _var in os.environ:
        del os.environ[_var]

_base_tcl = os.path.join(sys.base_prefix, "tcl")
if os.path.isdir(os.path.join(_base_tcl, "tcl8.6")):
    os.environ["TCL_LIBRARY"] = os.path.join(_base_tcl, "tcl8.6")
if os.path.isdir(os.path.join(_base_tcl, "tk8.6")):
    os.environ["TK_LIBRARY"] = os.path.join(_base_tcl, "tk8.6")

import json
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText
import requests


# 기본 프리셋 정의 (법률 분야가 기본값)
PRESETS = {
    "대한민국 법률 (기본)": {
        "mcp_enabled": True,
        "server_label": "korean-law",
        "server_url": "https://korean-law-mcp.fly.dev/mcp",
        "oc_param": "",
        "system_guide": (
            "당신은 대한민국 법률정보 검색 보조자입니다.\n"
            "■ 필수 준수 규칙:\n"
            "1. 사용자의 질문에서 요구하는 법령명(예: '민법', '형법', '개인정보 보호법' 등)을 정확히 파악하고, "
            "반드시 해당 법령명으로 search_law 도구를 새로 호출하세요.\n"
            "2. 이전 대화에서 조회했던 이전 법령(예: 개인정보보호법 등)의 결과나 식별정보(lawId, mst)를 현재 질문에 절대로 섞거나 재사용하지 마세요. "
            "질문의 법령이 다르면 무조건 새로운 도구 호출로 조회해야 합니다.\n"
            "3. 특정 조문(예: '민법 제750조')을 물으면 search_law(query='민법')로 mst/lawId를 찾은 후, "
            "반드시 get_law_text(jo='제750조') 도구로 해당 조문을 정확히 조회하여 설명하세요.\n"
            "4. 검색된 공식 법령 식별정보(법령명, 조문 번호, 시행일)를 근거로 핵심 의미를 설명하세요.\n"
            "5. 실제 사건 자문에 해당하면 단정적 결론 대신 일반 정보임을 안내하세요."
        ),
        "examples": [
            ("법령", "개인정보보호법의 개인정보 정의 조문을 찾아서 쉽게 설명해줘."),
            ("조문", "민법 제750조의 현행 조문을 찾아서 핵심 의미를 설명해줘."),
            ("판례", "부당해고와 관련된 판례를 검색하고 중요한 판례를 요약해줘."),
            ("자치법규", "대구광역시의 인공지능 또는 데이터 관련 자치법규가 있는지 찾아줘."),
            ("인용검증", "민법 제750조가 불법행위에 의한 손해배상 조항이라는 설명이 정확한지 검증해줘."),
        ],
        "info": (
            "대한민국 법률 모드입니다. 법령·조문·판례·자치법규 검색용 도구를 활용하며, "
            "실제 사건에 대한 법률 자문이나 확정적 결론을 대신하지 않습니다."
        ),
    },
    "사용자 지정 (Custom MCP)": {
        "mcp_enabled": True,
        "server_label": "custom-mcp",
        "server_url": "",
        "oc_param": "",
        "system_guide": (
            "당신은 유능한 AI 어시스턴트입니다.\n"
            "필요한 경우 연동된 MCP 도구를 적극적으로 호출하여 정확한 최신 정보나 계산 결과를 도출하세요.\n"
            "도구 실행 결과를 바탕으로 핵심 내용을 정리하여 친절하고 명확하게 답변하세요."
        ),
        "examples": [
            ("도구 조회", "현재 연동된 MCP 서버에서 제공하는 도구 목록과 사용 방법을 알려줘."),
            ("정보 검색", "연동된 도구를 사용해 관련 최신 정보를 검색하고 핵심을 요약해줘."),
            ("도구 실행", "입력 조건에 맞춰 필요한 도구를 호출하고 결과를 분석해줘."),
        ],
        "info": "사용자가 직접 입력한 MCP 서버 URL과 시스템 지침에 따라 도구를 호출하는 모드입니다.",
    },
    "일반 대화 (MCP 미사용)": {
        "mcp_enabled": False,
        "server_label": "",
        "server_url": "",
        "oc_param": "",
        "system_guide": (
            "당신은 친절하고 박학다식한 AI 어시스턴트입니다. 사용자의 질문에 정확하고 성실하게 답변하세요."
        ),
        "examples": [
            ("인사 및 소개", "안녕하세요! 당신이 지원하는 주요 능력과 활용 팁을 알려주세요."),
            ("코드 예제", "Python으로 REST API를 호출하는 비동기(async) 예제 코드를 작성해줘."),
            ("문서 요약", "다음 긴 문장의 핵심 요점을 3가지로 정리해줘: "),
        ],
        "info": "MCP 도구 연동 없이 LM Studio 로컬 모델과 직접 질의응답하는 일반 대화 모드입니다.",
    },
}

DEFAULT_PRESET_NAME = "대한민국 법률 (기본)"


class MultiMcpGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LM Studio + MCP Client (기본: Korean Law MCP)")
        self.geometry("1300x680")
        self.minsize(960, 500)
        self.previous_response_id = None
        self.turn_count = 0
        self.is_generating = False
        self.cancel_requested = False
        self._build_ui()
        self._apply_preset(DEFAULT_PRESET_NAME)
        self.after(150, self._apply_initial_layout)

    def _build_ui(self):
        # 최상위 좌우 분할 PanedWindow (시각적 구분선 및 손잡이 제공)
        root_pane = tk.PanedWindow(
            self,
            orient="horizontal",
            sashrelief="ridge",
            sashwidth=7,
            showhandle=True,
            sashpad=2,
            bg="#cbd5e1",
            bd=0,
            opaqueresize=True,
        )
        root_pane.pack(fill="both", expand=True, padx=4, pady=4)

        # ====================================================
        # [좌측] 설정 및 컨트롤 사이드바
        # ====================================================
        left_outer = ttk.Frame(root_pane, width=390)
        root_pane.add(left_outer, minsize=260)

        # 저해상도 화면 대비 스크롤 가능한 Canvas 컨테이너
        left_canvas = tk.Canvas(left_outer, highlightthickness=0, width=380)
        left_scrollbar = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left_scrollable = ttk.Frame(left_canvas, padding=(4, 2, 8, 4))

        left_scrollable.bind(
            "<Configure>",
            lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")),
        )
        canvas_win = left_canvas.create_window((0, 0), window=left_scrollable, anchor="nw")
        left_canvas.bind(
            "<Configure>",
            lambda e: left_canvas.itemconfig(canvas_win, width=e.width),
        )
        left_canvas.configure(yscrollcommand=left_scrollbar.set)

        left_scrollbar.pack(side="right", fill="y")
        left_canvas.pack(side="left", fill="both", expand=True)

        # 사이드바 마우스 휠 스크롤 연동
        def _on_sidebar_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        left_canvas.bind("<Enter>", lambda e: left_canvas.bind_all("<MouseWheel>", _on_sidebar_mousewheel))
        left_canvas.bind("<Leave>", lambda e: left_canvas.unbind_all("<MouseWheel>"))

        # ----------------------------------------------------
        # 1. LM Studio 연결 설정 (좌측)
        # ----------------------------------------------------
        lm = ttk.LabelFrame(left_scrollable, text="1. LM Studio 연결 설정", padding=8)
        lm.pack(fill="x", pady=(0, 6))

        ttk.Label(lm, text="Server URL:").grid(row=0, column=0, sticky="w", pady=2)
        self.server_var = tk.StringVar(value="http://localhost:1234")
        ttk.Entry(lm, textvariable=self.server_var).grid(row=0, column=1, sticky="ew", padx=4, pady=2)

        ttk.Label(lm, text="API Token:").grid(row=1, column=0, sticky="w", pady=2)
        self.token_var = tk.StringVar()
        ttk.Entry(lm, textvariable=self.token_var, show="*").grid(row=1, column=1, sticky="ew", padx=4, pady=2)

        btn_row = ttk.Frame(lm)
        btn_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        ttk.Button(btn_row, text="모델 목록 조회", command=self.refresh_models).pack(side="left")
        self.lm_status = tk.StringVar(value="연결 안 됨")
        ttk.Label(btn_row, textvariable=self.lm_status, foreground="#64748b").pack(side="left", padx=6)

        ttk.Label(lm, text="선택 모델:").grid(row=3, column=0, sticky="w", pady=(4, 2))
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(lm, textvariable=self.model_var, state="readonly")
        self.model_combo.grid(row=3, column=1, sticky="ew", padx=4, pady=(4, 2))

        ttk.Label(lm, text="Context Length:").grid(row=4, column=0, sticky="w", pady=(4, 2))
        self.ctx_var = tk.StringVar(value="기본값 (중복 로드 방지)")
        self.ctx_combo = ttk.Combobox(
            lm,
            textvariable=self.ctx_var,
            values=["기본값 (중복 로드 방지)", "4096", "8192", "16384", "32768"],
            state="readonly",
        )
        self.ctx_combo.grid(row=4, column=1, sticky="ew", padx=4, pady=(4, 2))

        lm.columnconfigure(1, weight=1)

        # ----------------------------------------------------
        # 2. MCP 서버 설정 & 프리셋 (좌측)
        # ----------------------------------------------------
        mcp_frame = ttk.LabelFrame(left_scrollable, text="2. MCP 서버 및 도메인 설정", padding=8)
        mcp_frame.pack(fill="x", pady=(0, 6))

        # 프리셋 바
        preset_box = ttk.Frame(mcp_frame)
        preset_box.pack(fill="x", pady=(0, 4))

        ttk.Label(preset_box, text="프리셋:").pack(side="left")
        self.preset_var = tk.StringVar(value=DEFAULT_PRESET_NAME)
        preset_cb = ttk.Combobox(
            preset_box,
            textvariable=self.preset_var,
            values=list(PRESETS.keys()),
            state="readonly",
            width=18,
        )
        preset_cb.pack(side="left", padx=4, fill="x", expand=True)
        preset_cb.bind("<<ComboboxSelected>>", lambda e: self._on_preset_change())

        ttk.Button(
            preset_box,
            text="↺ 복원",
            width=6,
            command=lambda: self._apply_preset(DEFAULT_PRESET_NAME),
        ).pack(side="left", padx=2)

        self.mcp_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            mcp_frame,
            text="MCP 도구 연동 활성화 (해제 시 일반 대화)",
            variable=self.mcp_enabled_var,
            command=self._toggle_mcp_fields,
        ).pack(fill="x", pady=(2, 6))

        # 서버 상세 입력 그리드
        self.fields_grid = ttk.Frame(mcp_frame)
        self.fields_grid.pack(fill="x", pady=(0, 4))

        ttk.Label(self.fields_grid, text="Label:").grid(row=0, column=0, sticky="w", pady=2)
        self.label_var = tk.StringVar(value="korean-law")
        self.label_entry = ttk.Entry(self.fields_grid, textvariable=self.label_var)
        self.label_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=2)

        ttk.Label(self.fields_grid, text="MCP URL:").grid(row=1, column=0, sticky="w", pady=2)
        self.mcp_url_var = tk.StringVar()
        self.url_entry = ttk.Entry(self.fields_grid, textvariable=self.mcp_url_var)
        self.url_entry.grid(row=1, column=1, sticky="ew", padx=4, pady=2)

        ttk.Label(self.fields_grid, text="추가 파라미터:").grid(row=2, column=0, sticky="w", pady=2)
        self.oc_var = tk.StringVar()
        self.oc_entry = ttk.Entry(self.fields_grid, textvariable=self.oc_var, show="*")
        self.oc_entry.grid(row=2, column=1, sticky="ew", padx=4, pady=2)

        self.fields_grid.columnconfigure(1, weight=1)

        # 시스템 프롬프트(지침) 편집 패널 (접기/펼치기 토글 지원)
        sys_header = ttk.Frame(mcp_frame)
        sys_header.pack(fill="x", pady=(4, 2))

        self.sys_visible = tk.BooleanVar(value=False)
        self.sys_toggle_btn = ttk.Checkbutton(
            sys_header,
            text="▼ 시스템 지침(System Guide) 편집",
            variable=self.sys_visible,
            command=self._toggle_system_guide,
        )
        self.sys_toggle_btn.pack(side="left")

        self.sys_frame = ttk.Frame(mcp_frame)
        self.system_guide_text = ScrolledText(self.sys_frame, height=5, wrap="word", font=("Arial", 9))
        self.system_guide_text.pack(fill="x", expand=True)

        # ----------------------------------------------------
        # 3. 예제 질문 영역 (좌측)
        # ----------------------------------------------------
        self.ex_frame = ttk.LabelFrame(left_scrollable, text="3. 예제 질문 (클릭 시 입력창 반영)", padding=6)
        self.ex_frame.pack(fill="x", pady=(0, 6))

        self.ex_btn_container = ttk.Frame(self.ex_frame)
        self.ex_btn_container.pack(fill="x")

        # ====================================================
        # [우측] 메인 대화 & 로그 & 질문 입력 패널
        # ====================================================
        right_panel = ttk.Frame(root_pane)
        root_pane.add(right_panel, minsize=420)

        # 상단 크기 및 레이아웃 제어 툴바
        view_ctrl_bar = ttk.Frame(right_panel)
        view_ctrl_bar.pack(fill="x", pady=(0, 4))

        ttk.Label(
            view_ctrl_bar,
            text="가로 비율:",
            font=("맑은 고딕", 9, "bold"),
            foreground="#334155",
        ).pack(side="left")

        ttk.Button(
            view_ctrl_bar,
            text="7:3",
            width=5,
            command=lambda: self._set_chat_log_ratio(0.7),
        ).pack(side="left", padx=1)

        ttk.Button(
            view_ctrl_bar,
            text="5:5",
            width=5,
            command=lambda: self._set_chat_log_ratio(0.5),
        ).pack(side="left", padx=1)

        ttk.Button(
            view_ctrl_bar,
            text="3:7",
            width=5,
            command=lambda: self._set_chat_log_ratio(0.3),
        ).pack(side="left", padx=1)

        self.log_visible = True
        self.toggle_log_btn = ttk.Button(
            view_ctrl_bar,
            text="◧ 로그 숨기기",
            command=self._toggle_log_visible,
        )
        self.toggle_log_btn.pack(side="left", padx=(5, 1))

        self.split_orient_btn = ttk.Button(
            view_ctrl_bar,
            text="⬍ 상하/좌우 전환",
            command=self._toggle_split_orient,
        )
        self.split_orient_btn.pack(side="left", padx=1)

        # 세로 비율 빠른 조절 버튼 (입력창 확대/기본)
        ttk.Separator(view_ctrl_bar, orient="vertical").pack(side="left", fill="y", padx=4, pady=2)
        ttk.Label(
            view_ctrl_bar,
            text="세로 조절:",
            font=("맑은 고딕", 9, "bold"),
            foreground="#334155",
        ).pack(side="left")

        self.input_expanded = False
        self.toggle_inp_btn = ttk.Button(
            view_ctrl_bar,
            text="입력창 확대",
            command=self._toggle_input_height,
        )
        self.toggle_inp_btn.pack(side="left", padx=1)

        ttk.Button(
            view_ctrl_bar,
            text="기본 비율",
            command=self._apply_initial_layout,
        ).pack(side="left", padx=1)

        ttk.Label(
            view_ctrl_bar,
            text="※ 모든 구분선(손잡이)을 마우스로 드래그해 가로/세로 조절 가능",
            font=("맑은 고딕", 8),
            foreground="#64748b",
        ).pack(side="right")

        # ----------------------------------------------------
        # [우측 메인 세로 분할 PanedWindow]
        # 상단: Chat & Log 뷰
        # 하단: 5. 질문 입력 및 제어
        # ----------------------------------------------------
        self.right_vertical_pane = tk.PanedWindow(
            right_panel,
            orient="vertical",
            sashrelief="ridge",
            sashwidth=8,
            showhandle=True,
            sashpad=3,
            bg="#cbd5e1",
            bd=1,
            opaqueresize=True,
        )
        self.right_vertical_pane.pack(fill="both", expand=True)

        # 4. Chat & Log 영역 (가로/세로 분할 가능한 내부 PanedWindow)
        self.chat_log_pane = tk.PanedWindow(
            self.right_vertical_pane,
            orient="horizontal",
            sashrelief="ridge",
            sashwidth=8,
            showhandle=True,
            sashpad=3,
            bg="#cbd5e1",
            bd=0,
            opaqueresize=True,
        )
        self.right_vertical_pane.add(self.chat_log_pane, minsize=180)

        self.chat_frame = ttk.LabelFrame(self.chat_log_pane, text="4. Chat (질문 & 답변)", padding=6)
        self.log_frame = ttk.LabelFrame(self.chat_log_pane, text="MCP Tool 호출 및 실행 로그", padding=6)
        self.chat_log_pane.add(self.chat_frame, minsize=140)
        self.chat_log_pane.add(self.log_frame, minsize=100)

        self.chat = ScrolledText(
            self.chat_frame,
            wrap="word",
            state="disabled",
            font=("맑은 고딕", 11),
            spacing1=4,
            spacing2=7,
            spacing3=4,
            padx=14,
            pady=12,
            background="#ffffff",
            foreground="#1e293b",
            selectbackground="#cce8ff",
        )
        self.chat.pack(fill="both", expand=True)
        self._setup_chat_tags()

        self.log = ScrolledText(
            self.log_frame,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
            spacing1=3,
            spacing2=5,
            spacing3=3,
            padx=10,
            pady=10,
            background="#fafafa",
            foreground="#2d3748",
        )
        self.log.pack(fill="both", expand=True)

        # ----------------------------------------------------
        # 5. 질문 입력 및 전송 제어 (하단 패널: 세로 크기 조절 가능)
        # ----------------------------------------------------
        self.inp_frame = ttk.LabelFrame(self.right_vertical_pane, text="5. 질문 입력 및 제어", padding=6)
        self.right_vertical_pane.add(self.inp_frame, minsize=75)

        inp_content = ttk.Frame(self.inp_frame)
        inp_content.pack(fill="both", expand=True)

        self.input = tk.Text(
            inp_content,
            wrap="word",
            font=("맑은 고딕", 11),
            spacing1=2,
            spacing2=4,
            spacing3=2,
            padx=10,
            pady=8,
        )
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Control-Return>", lambda e: (self.send(), "break"))

        btn_frame = ttk.Frame(inp_content)
        btn_frame.pack(side="left", fill="y", padx=(6, 0))

        self.send_btn = ttk.Button(btn_frame, text="전송\n(Ctrl+Enter)", command=self.send, width=13)
        self.send_btn.pack(fill="both", expand=True)

        self.cancel_btn = ttk.Button(btn_frame, text="⏹ 생성 중단", command=self.cancel_request, width=13, state="disabled")
        self.cancel_btn.pack(fill="x", pady=(2, 0))

        ttk.Button(btn_frame, text="새 대화", command=self.new_chat, width=13).pack(fill="x", pady=(2, 0))

        self.keep_context_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            btn_frame,
            text="연속 대화 기억",
            variable=self.keep_context_var,
        ).pack(fill="x", pady=(2, 0))

        # 하단 상태 표시줄
        self.status = tk.StringVar(value="준비됨")
        ttk.Label(right_panel, textvariable=self.status, relief="sunken", anchor="w", padding=(4, 2)).pack(
            fill="x", pady=(4, 0)
        )

    def _apply_initial_layout(self):
        """초기 가로/세로 분할 비율을 적용합니다."""
        self._set_chat_log_ratio(0.6)
        self._set_vertical_ratio(0.76)

    def _set_vertical_ratio(self, ratio=0.76):
        """상단(대화/로그 창)과 하단(질문 입력창)의 세로 크기 비율을 설정합니다."""
        self.update_idletasks()
        try:
            total = self.right_vertical_pane.winfo_height()
            if total > 150:
                pos = max(180, min(total - 75, int(total * ratio)))
                self.right_vertical_pane.sash_place(0, 0, pos)
        except Exception:
            pass

    def _toggle_input_height(self):
        """질문 입력창의 세로 크기를 확대하거나 기본값으로 복원합니다."""
        if self.input_expanded:
            self._set_vertical_ratio(0.76)
            self.input_expanded = False
            self.toggle_inp_btn.config(text="입력창 확대")
        else:
            self._set_vertical_ratio(0.50)
            self.input_expanded = True
            self.toggle_inp_btn.config(text="입력창 축소")

    def _set_chat_log_ratio(self, ratio=0.6):
        """대화창과 로그창의 크기 비율을 설정합니다."""
        if not self.log_visible:
            self._toggle_log_visible()
        self.update_idletasks()
        try:
            orient = self.chat_log_pane.cget("orient")
            if orient == "horizontal":
                total = self.chat_log_pane.winfo_width()
                if total > 50:
                    pos = max(120, min(total - 100, int(total * ratio)))
                    self.chat_log_pane.sash_place(0, pos, 0)
            else:
                total = self.chat_log_pane.winfo_height()
                if total > 50:
                    pos = max(100, min(total - 80, int(total * ratio)))
                    self.chat_log_pane.sash_place(0, 0, pos)
        except Exception:
            pass

    def _toggle_log_visible(self):
        """MCP Tool 로그 창을 숨기거나 다시 표시합니다."""
        if self.log_visible:
            self.chat_log_pane.forget(self.log_frame)
            self.log_visible = False
            self.toggle_log_btn.config(text="◨ 로그 보이기")
        else:
            self.chat_log_pane.add(self.log_frame, minsize=100)
            self.log_visible = True
            self.toggle_log_btn.config(text="◧ 로그 숨기기")
            self.after(50, lambda: self._set_chat_log_ratio(0.6))

    def _toggle_split_orient(self):
        """대화창과 로그창의 가로(좌우) 분할과 세로(상하) 분할을 전환합니다."""
        current_orient = self.chat_log_pane.cget("orient")
        new_orient = "vertical" if current_orient == "horizontal" else "horizontal"

        self.chat_log_pane.forget(self.chat_frame)
        if self.log_visible:
            self.chat_log_pane.forget(self.log_frame)

        self.chat_log_pane.config(orient=new_orient)
        self.chat_log_pane.add(self.chat_frame, minsize=120)
        if self.log_visible:
            self.chat_log_pane.add(self.log_frame, minsize=100)

        self.after(50, lambda: self._set_chat_log_ratio(0.6))

    def _on_preset_change(self):
        selected = self.preset_var.get()
        self._apply_preset(selected)

    def _apply_preset(self, preset_name):
        if preset_name not in PRESETS:
            return

        self.preset_var.set(preset_name)
        preset = PRESETS[preset_name]

        self.mcp_enabled_var.set(preset["mcp_enabled"])
        self.label_var.set(preset["server_label"])
        self.mcp_url_var.set(preset["server_url"])
        self.oc_var.set(preset["oc_param"])

        # 시스템 가이드 입력
        self.system_guide_text.delete("1.0", "end")
        self.system_guide_text.insert("1.0", preset["system_guide"])

        self._toggle_mcp_fields()
        self._render_example_buttons(preset["examples"])

        # 대화창에 모드 변경 안내 출력
        self._append(self.chat, "SYSTEM", f"[{preset_name}] 모드가 적용되었습니다.\n{preset['info']}")

    def _render_example_buttons(self, examples):
        for widget in self.ex_btn_container.winfo_children():
            widget.destroy()

        for i, (name, prompt) in enumerate(examples):
            row = i // 2
            col = i % 2
            btn = ttk.Button(
                self.ex_btn_container,
                text=name,
                command=lambda p=prompt: self._put_prompt(p),
            )
            btn.grid(row=row, column=col, padx=2, pady=2, sticky="ew")
        self.ex_btn_container.columnconfigure(0, weight=1)
        self.ex_btn_container.columnconfigure(1, weight=1)

    def _toggle_mcp_fields(self):
        enabled = self.mcp_enabled_var.get()
        state = "normal" if enabled else "disabled"
        self.label_entry.config(state=state)
        self.url_entry.config(state=state)
        self.oc_entry.config(state=state)

    def _toggle_system_guide(self):
        if self.sys_visible.get():
            self.sys_frame.pack(fill="x", pady=(2, 4))
            self.sys_toggle_btn.config(text="▲ 시스템 지침(System Guide) 닫기")
        else:
            self.sys_frame.pack_forget()
            self.sys_toggle_btn.config(text="▼ 시스템 지침(System Guide) 직접 편집하기")

    def _put_prompt(self, prompt):
        # 예제 질문은 각각 독립된 주제이므로 이전 컨텍스트 자동 초기화
        self.previous_response_id = None
        self.turn_count = 0
        self.input.delete("1.0", "end")
        self.input.insert("1.0", prompt)
        self.input.focus_set()

    def _base(self):
        return self.server_var.get().strip().rstrip("/")

    def _headers(self):
        h = {"Content-Type": "application/json"}
        token = self.token_var.get().strip()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def refresh_models(self):
        self.status.set("LM Studio 모델 목록 조회 중...")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            r = requests.get(f"{self._base()}/api/v1/models", headers=self._headers(), timeout=15)
            r.raise_for_status()
            data = r.json()
            models = [m.get("key") for m in data.get("models", []) if m.get("type") == "llm" and m.get("key")]
            self.after(0, lambda: self._set_models(models))
        except Exception as e:
            self.after(0, lambda: self._error("LM Studio 연결 오류", e))

    def _set_models(self, models):
        self.model_combo["values"] = models
        if models and not self.model_var.get().strip():
            self.model_var.set(models[0])
        self.lm_status.set(f"연결됨 / {len(models)}개 모델 감지")
        self.status.set("모델 목록 조회 완료")

    def _build_integration(self):
        if not self.mcp_enabled_var.get():
            return None

        url = self.mcp_url_var.get().strip()
        if not url:
            raise ValueError("MCP 연동이 켜져 있으나 Remote MCP URL이 비어 있습니다.")

        oc = self.oc_var.get().strip()
        if oc and "oc=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}oc={oc}"

        label = self.label_var.get().strip() or "mcp-server"

        return {
            "type": "ephemeral_mcp",
            "server_label": label,
            "server_url": url,
        }

    def cancel_request(self):
        """사용자가 진행 중인 요청을 수동 중단합니다."""
        if self.is_generating:
            self.cancel_requested = True
            self.is_generating = False
            self.send_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")
            self.status.set("사용자에 의해 생성이 중단되었습니다.")
            self._append(self.chat, "SYSTEM", "⏹ 사용자에 의해 응답 생성이 중단되었습니다.")

    def send(self):
        if self.is_generating:
            return

        question = self.input.get("1.0", "end").strip()
        if not question:
            return

        model = self.model_var.get().strip()
        if not model:
            messagebox.showwarning("모델 선택 필요", "먼저 '모델 목록 조회'를 누르고 모델을 선택하세요.")
            return

        integration = None
        if self.mcp_enabled_var.get():
            try:
                integration = self._build_integration()
            except Exception as e:
                messagebox.showerror("MCP 설정 오류", str(e))
                return

        guide = self.system_guide_text.get("1.0", "end").strip()
        if guide:
            full_prompt = (
                f"{guide}\n\n"
                f"----------------------------------------\n"
                f"[현재 사용자 질문]\n{question}\n"
                f"※ 검색 지침: 위 질문에 명시된 법령명(예: 민법) 및 조문 번호를 공식 검색 도구(search_law, get_law_text)로 새로 조회하여 답변하세요. 이전 대화의 다른 법령 내용을 절대로 재사용하지 마세요."
            )
        else:
            full_prompt = question

        self.input.delete("1.0", "end")
        self._append(self.chat, "나", question)
        self.send_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.is_generating = True
        self.cancel_requested = False

        status_msg = "MCP 도구 조회 및 응답 생성 중..." if integration else "응답 생성 중..."
        self.status.set(status_msg)

        threading.Thread(
            target=self._chat_worker,
            args=(full_prompt, model, integration),
            daemon=True,
        ).start()

    def _chat_worker(self, prompt, model, integration):
        try:
            payload = {
                "model": model,
                "input": prompt,
                "temperature": 0.2,
                "store": True,
            }
            if integration:
                payload["integrations"] = [integration]

            # Context Length 옵션: 숫자로 지정된 경우에만 전송 (기본값일 때는 미전송 -> 중복 모델 인스턴스 생성 방지!)
            ctx_val = self.ctx_var.get().strip()
            if ctx_val.isdigit():
                payload["context_length"] = int(ctx_val)

            # '연속 대화 기억'이 체크되어 있을 때만 이전 대화 세션 컨텍스트 전달
            if self.keep_context_var.get() and self.previous_response_id:
                payload["previous_response_id"] = self.previous_response_id

            if self.cancel_requested:
                return

            # 1차 요청
            r = requests.post(
                f"{self._base()}/api/v1/chat",
                headers=self._headers(),
                json=payload,
                timeout=180,
            )

            if self.cancel_requested:
                return

            # Context Length 초과 오류 감지 및 자동 복구 (Auto-Recovery)
            if not r.ok:
                err_text = r.text.lower()
                is_context_exceeded = (
                    any(kw in err_text for kw in ["context", "token", "length", "window", "exceed", "limit", "overflow", "too long", "maximum"])
                    or r.status_code in (400, 413, 422)
                )

                # 이전 세션이 전달되었는데 실패한 경우 -> 이전 컨텍스트 비우고 현재 질문으로 즉시 자동 재시도
                if is_context_exceeded and payload.get("previous_response_id"):
                    self.after(0, lambda: self._append(
                        self.chat,
                        "SYSTEM",
                        "⚠️ [Context Window 초과 감지]\n"
                        "대화 누적 및 도구 검색 결과가 모델의 Context Length를 초과하여 멈춤을 방지하기 위해,\n"
                        "이전 컨텍스트를 자동 초기화하고 현재 질문으로 즉시 재시도합니다..."
                    ))
                    self.previous_response_id = None
                    self.turn_count = 0
                    payload.pop("previous_response_id", None)

                    r = requests.post(
                        f"{self._base()}/api/v1/chat",
                        headers=self._headers(),
                        json=payload,
                        timeout=180,
                    )

            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}\n{r.text}")

            if self.cancel_requested:
                return

            data = r.json()
            self.previous_response_id = data.get("response_id")
            self.turn_count += 1

            messages, calls, reasonings = [], [], []
            for item in data.get("output", []):
                itype = item.get("type")
                if itype == "message" and item.get("content"):
                    messages.append(item["content"])
                elif itype == "tool_call":
                    calls.append(item)
                elif itype == "reasoning" and item.get("content"):
                    reasonings.append(item["content"])

            answer = "\n\n".join(messages).strip() or "(최종 텍스트 응답 없음)"
            self.after(0, lambda: self._finish(answer, calls, reasonings, bool(integration)))
        except Exception as e:
            if not self.cancel_requested:
                self.after(0, lambda: self._error("요청 처리 오류", e))
        finally:
            self.is_generating = False
            self.after(0, lambda: self.cancel_btn.config(state="disabled"))

    def _finish(self, answer, calls, reasonings, had_integration):
        self._append(self.chat, "AI 응답", answer)

        if reasonings:
            self._append(self.log, "AI 추론 과정 (Thinking)", "\n".join(reasonings).strip())

        if had_integration:
            if calls:
                for c in calls:
                    info = {
                        "tool": c.get("tool"),
                        "arguments": c.get("arguments"),
                        "provider_info": c.get("provider_info"),
                        "output": c.get("output"),
                    }
                    self._append(self.log, "TOOL CALL 실행 결과", json.dumps(info, ensure_ascii=False, indent=2))
            else:
                self._append(
                    self.log,
                    "안내 (도구 미호출)",
                    "모델이 이번 질문에는 MCP 도구를 호출하지 않고 직접 답변했습니다.\n\n"
                    "■ 정상 동작인 경우:\n"
                    "  - '판례가 몇 개 있나요?'와 같이 단순 통계/개수를 묻는 질문은\n"
                    "    모델이 '검색 도구로는 전체 집계가 불가하다'고 판단하여 도구 없이 직접 설명합니다.\n"
                    "  - 일반적인 인사나 법률 개념 설명 질문 등.\n\n"
                    "■ MCP 도구 호출을 유도하려면:\n"
                    "  - 상단의 [판례], [조문], [법령] 예제 버튼을 누르거나\n"
                    "  - '부당해고 관련 판례를 검색해줘', '민법 제1조 조문을 찾아줘'처럼\n"
                    "    구체적인 검색 키워드를 포함해 질문하세요.\n\n"
                    "■ 만약 구체적인 검색 질문에도 도구가 호출되지 않는다면:\n"
                    "  - LM Studio Server Settings의 'Allow per-request MCPs' 허용 여부 확인\n"
                    "  - 사용 모델이 Tool Calling을 지원하는지 확인"
                )
        else:
            self._append(self.log, "INFO", "MCP 비활성화 모드로 일반 응답이 생성되었습니다.")

        self.send_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        ctx_info = f"연속 대화 {self.turn_count}턴" if (self.keep_context_var.get() and self.previous_response_id) else "독립 질문"
        self.status.set(f"완료 ({ctx_info})")
    def _setup_chat_tags(self):
        base_font = "맑은 고딕"

        # 굵은 글씨 (**XX**)
        self.chat.tag_configure("bold", font=(base_font, 11, "bold"), foreground="#0f172a")

        # 헤딩 태그
        self.chat.tag_configure("h1", font=(base_font, 14, "bold"), foreground="#0f172a", spacing1=8, spacing3=4)
        self.chat.tag_configure("h2", font=(base_font, 12, "bold"), foreground="#1e293b", spacing1=6, spacing3=3)
        self.chat.tag_configure("h3", font=(base_font, 11, "bold"), foreground="#334155", spacing1=4, spacing3=2)

        # 인라인 코드 & 코드 블록
        self.chat.tag_configure("code_inline", font=("Consolas", 10), background="#f1f5f9", foreground="#dc2626")
        self.chat.tag_configure(
            "code_block",
            font=("Consolas", 10),
            background="#f8fafc",
            foreground="#0f172a",
            spacing1=2,
            spacing2=3,
            spacing3=2,
        )

        # 구분선
        self.chat.tag_configure("divider", foreground="#cbd5e1", spacing1=6, spacing3=6)

        # 헤더 태그
        self.chat.tag_configure("header_you", font=(base_font, 11, "bold"), foreground="#2563eb", spacing1=12, spacing3=4)
        self.chat.tag_configure("header_ai", font=(base_font, 11, "bold"), foreground="#059669", spacing1=14, spacing3=4)
        self.chat.tag_configure("header_system", font=(base_font, 10, "bold"), foreground="#64748b", spacing1=8, spacing3=3)
        self.chat.tag_configure("header_guide", font=(base_font, 10, "bold"), foreground="#7c3aed", spacing1=8, spacing3=3)

        # 불릿 및 본문 텍스트
        self.chat.tag_configure("bullet", font=(base_font, 11, "bold"), foreground="#2563eb")
        self.chat.tag_configure("normal", font=(base_font, 11), foreground="#1e293b")

    def _insert_styled_text(self, text, base_tag="normal"):
        # **볼드** 및 `인라인코드` 파싱
        parts = re.split(r'(\*\*.*?\*\*|`[^`\n]+`)', text)
        for part in parts:
            if not part:
                continue
            if part.startswith("**") and part.endswith("**") and len(part) >= 4:
                content = part[2:-2]
                self.chat.insert("end", content, ("bold",))
            elif part.startswith("`") and part.endswith("`") and len(part) >= 2:
                content = part[1:-1]
                self.chat.insert("end", content, ("code_inline",))
            else:
                self.chat.insert("end", part, (base_tag,))

    def _append_markdown(self, widget, title, text):
        widget.config(state="normal")

        # 헤더 구분
        header_tag = "header_ai"
        if title in ("나", "User"):
            header_tag = "header_you"
        elif title in ("SYSTEM",):
            header_tag = "header_system"
        elif "안내" in title or "모드" in title:
            header_tag = "header_guide"

        widget.insert("end", f"\n[{title}]\n", (header_tag,))

        in_code_block = False
        lines = text.split("\n")
        for line in lines:
            stripped = line.strip()

            # 코드 블록 토글
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                widget.insert("end", f"{line}\n", ("code_block",))
                continue

            if in_code_block:
                widget.insert("end", f"{line}\n", ("code_block",))
                continue

            # 구분선
            if stripped in ("---", "___", "***"):
                widget.insert("end", "────────────────────────────────────────\n", ("divider",))
                continue

            # H1
            if line.startswith("# "):
                heading = line[2:].strip()
                self._insert_styled_text(heading, base_tag="h1")
                widget.insert("end", "\n")
                continue

            # H2
            if line.startswith("## "):
                heading = line[3:].strip()
                self._insert_styled_text(heading, base_tag="h2")
                widget.insert("end", "\n")
                continue

            # H3
            if line.startswith("### "):
                heading = line[4:].strip()
                self._insert_styled_text(heading, base_tag="h3")
                widget.insert("end", "\n")
                continue

            # 리스트 (불릿 기호)
            lstripped = line.lstrip()
            indent = len(line) - len(lstripped)
            indent_spaces = " " * indent
            if lstripped.startswith(("- ", "* ")):
                bullet_body = lstripped[2:]
                widget.insert("end", f"{indent_spaces}• ", ("bullet",))
                self._insert_styled_text(bullet_body, base_tag="normal")
                widget.insert("end", "\n")
                continue

            # 일반 줄
            self._insert_styled_text(line, base_tag="normal")
            widget.insert("end", "\n")

        widget.see("end")
        widget.config(state="disabled")

    def _append(self, widget, title, text):
        if widget == self.chat:
            self._append_markdown(widget, title, text)
        else:
            widget.config(state="normal")
            widget.insert("end", f"\n[{title}]\n{text}\n")
            widget.see("end")
            widget.config(state="disabled")

    def _error(self, title, error):
        self.send_btn.config(state="normal")
        self.status.set("오류 발생")
        messagebox.showerror(
            title,
            f"{error}\n\n"
            "확인 권장 사항:\n"
            "1) LM Studio 로컬 서버(Local Server)가 실행 중인지 확인\n"
            "2) Tool Calling 지원 모델 선택 확인\n"
            "3) LM Studio의 Server Settings > 'Allow per-request MCPs' 허용 여부\n"
            "4) 원격 MCP URL 접속 가능 여부 (방화벽/네트워크 확인)"
        )

    def new_chat(self):
        self.previous_response_id = None
        self._append(self.chat, "SYSTEM", "새 대화 세션이 시작되었습니다. 이전 컨텍스트가 초기화되었습니다.")
        self.status.set("새 대화 시작")


if __name__ == "__main__":
    MultiMcpGUI().mainloop()
