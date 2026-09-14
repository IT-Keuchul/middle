# chatbot_mpc.py
# LM Studio + MCP 선택형 Chatbot GUI
# 필요 패키지: pip install requests

import os
import sys
from pathlib import Path

# 부모 프로세스(PyInstaller 런처 등)로부터 상속된 잘못된 TCL/TK 환경변수 완전 제거 및 현재 Python 경로로 재설정
for _var in ("TCL_LIBRARY", "TK_LIBRARY"):
    os.environ.pop(_var, None)

for _base in (sys.base_prefix, sys.prefix):
    _tcl = Path(_base) / "tcl" / "tcl8.6"
    _tk = Path(_base) / "tcl" / "tk8.6"
    if _tcl.exists() and "TCL_LIBRARY" not in os.environ:
        os.environ["TCL_LIBRARY"] = str(_tcl)
    if _tk.exists() and "TK_LIBRARY" not in os.environ:
        os.environ["TK_LIBRARY"] = str(_tk)

import json
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import requests

APP_TITLE = "LM Studio MCP Chatbot"
DEFAULT_BASE_URL = "http://127.0.0.1:1234"
DEFAULT_MCP_JSON = Path.home() / ".lmstudio" / "mcp.json"

# 한국어 전용 기본 시스템 프롬프트 (사고 과정 및 Raw JSON 출력 차단)
DEFAULT_SYSTEM_PROMPT = (
    "당신은 유능하고 정중한 한국어 전용 AI 어시스턴트입니다.\n"
    "1. 모든 답변은 반드시 자연스럽고 유려한 순수 한국어로만 작성하십시오. 영어를 병기하거나 영문 번역 문장을 출력하지 마십시오.\n"
    "2. Thinking Process, Step-by-step 분석 계획, 내부 사고 과정은 사용자에게 절대로 출력하지 마십시오. 오직 최종 답변만 제시하십시오.\n"
    "3. 도구(Tool)가 반환한 Raw JSON 데이터나 코드 블록을 그대로 출력하지 말고, 검색/조회된 핵심 정보를 알기 쉽게 한국어로 요약 및 설명하십시오."
)


def clean_answer(text: str) -> tuple[str, str]:
    """
    모델 응답에서 Thinking Process, <think> 태그, Raw Tool JSON 블록 등을 분리/정제합니다.
    반환: (정제된_답변_텍스트, 분리된_사고과정_및_원시데이터)
    """
    if not text:
        return "", ""
    thought_parts = []

    # 1. <think>...</think> 또는 <thought>...</thought> 태그 분리
    def _extract_tag(pattern, s):
        def _repl(m):
            thought_parts.append(m.group(1).strip())
            return ""
        return re.sub(pattern, _repl, s, flags=re.DOTALL | re.IGNORECASE)

    text = _extract_tag(r"<think>(.*?)</think>", text)
    text = _extract_tag(r"<thought>(.*?)</thought>", text)

    # 2. Thinking Process: / Thought Process: 블록 감지 및 분리
    tp_pattern = r"(?:^|\n)(?:Thinking Process|Thought Process|Reasoning Process):\s*(.*?)(?=\n\s*(?:(?:#+\s+)|(?:\[\s*\{)|(?:\*\(Self-Correction)|(?:[가-힣])|(?:Here is)|(?:Based on)|$))"
    tp_match = re.search(tp_pattern, text, flags=re.DOTALL | re.IGNORECASE)
    if tp_match and tp_match.start() < 120:
        thought_parts.append(tp_match.group(0).strip())
        text = text[:tp_match.start()] + "\n" + text[tp_match.end():]

    # 3. 자잘한 시뮬레이션 문구 분리 (예: *(Self-Correction during execution simulation:...)*)
    sim_pattern = r"\*\(Self-Correction[^\)]*\)\*"
    for sm in re.finditer(sim_pattern, text, flags=re.IGNORECASE):
        thought_parts.append(sm.group(0).strip())
    text = re.sub(sim_pattern, "", text, flags=re.IGNORECASE)

    # 4. Raw Tool JSON 문자열 분리 및 처리 (예: [{"type":"text","text":"..."}])
    raw_json_pattern = r'\[\s*\{\s*"type"\s*:\s*"text"\s*,\s*"text"\s*:\s*"(?:[^"\\]|\\.)*"\s*\}\s*\]'
    raw_json_matches = list(re.finditer(raw_json_pattern, text, flags=re.DOTALL))
    if raw_json_matches:
        # JSON 외에 사용자를 위한 다른 답변 텍스트가 남아 있는지 확인
        temp_text = re.sub(raw_json_pattern, "", text, flags=re.DOTALL).strip()
        if temp_text:
            # 다른 자연어 답변이 이미 존재하면 Tool JSON 블록은 thought_parts로 이동하여 숨김
            for m in raw_json_matches:
                thought_parts.append(m.group(0).strip())
            text = temp_text
        else:
            # 다른 답변이 전혀 없는 경우에만 JSON 내부 text 필드를 추출하여 표시
            def _json_repl(m):
                raw_inner = m.group(0)
                try:
                    parsed = json.loads(raw_inner)
                    if isinstance(parsed, list) and parsed and "text" in parsed[0]:
                        return parsed[0]["text"]
                except Exception:
                    pass
                return raw_inner
            text = re.sub(raw_json_pattern, _json_repl, text, flags=re.DOTALL)

    # 5. 프롬프트 에코 잔여물 정리 ([사용자]... [AI]... 구조가 모델 출력에 반사된 경우)
    echo_match = re.search(r"\n\s*\[(?:사용자|User)\]\s*\n.*?\n\s*\[(?:AI|Assistant)\]\s*\n?", text, flags=re.DOTALL)
    if echo_match:
        thought_parts.append(echo_match.group(0).strip())
        text = text[:echo_match.start()] + "\n" + text[echo_match.end():]

    # 공백 정돈
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    thoughts = "\n\n".join(t for t in thought_parts if t.strip()).strip()
    return text, thoughts

# 주요 MCP 서버의 알려진 기본 Tool 프리셋
KNOWN_MCP_TOOLS = {
    "huggingface": ["hub_repo_search", "hub_repo_details", "hf_whoami", "hf_fs"],
    "hf": ["hub_repo_search", "hub_repo_details", "hf_whoami", "hf_fs"],
    "hf-mcp-server": ["hub_repo_search", "hub_repo_details", "hf_whoami", "hf_fs"],
    "filesystem": [
        "read_file",
        "read_text_file",
        "read_media_file",
        "read_multiple_files",
        "write_file",
        "edit_file",
        "create_directory",
        "list_directory",
        "list_directory_with_sizes",
        "directory_tree",
        "move_file",
        "search_files",
        "get_file_info",
        "list_allowed_directories",
    ],
    "notion": [
        "notion_search",
        "notion_get_page",
        "notion_create_page",
        "notion_update_page",
        "notion_get_database",
        "notion_query_database",
        "notion_list_users",
    ],
    "github": [
        "search_repositories",
        "get_file_contents",
        "create_or_update_file",
        "create_issue",
        "list_issues",
        "search_code",
        "search_issues",
    ],
    "sqlite": ["read_query", "write_query", "create_table", "list_tables", "describe_table"],
    "brave-search": ["brave_web_search", "brave_local_search"],
    "fetch": ["fetch"],
    "puppeteer": [
        "puppeteer_navigate",
        "puppeteer_screenshot",
        "puppeteer_click",
        "puppeteer_fill",
        "puppeteer_select",
        "puppeteer_hover",
        "puppeteer_evaluate",
    ],
    "korea-cerified": [
        "recommend_qualifications", "build_career_roadmap", "check_legal_requirements",
        "search_qualification", "get_exam_schedule", "get_qualification_info",
        "get_exam_fee", "get_pass_rate", "find_test_sites", "get_disqualification_info",
        "get_exam_questions", "search_overseas_jobs", "get_overseas_employment_stats",
        "search_overseas_companies", "search_overseas_top_jobs", "get_overseas_job_fairs",
        "get_overseas_country_info", "get_overseas_hiring_stats", "get_overseas_settlement_support",
        "search_overseas_resources", "search_kmove_programs", "search_ncs",
        "get_ncs_related_qualifications", "search_ncs_learning_materials", "search_blind_hiring",
        "get_ncs_core_competencies", "get_ncs_classification", "search_course_qualifications",
        "search_work_learning_qualifications", "search_dual_training_orgs", "find_dual_exam_sites",
        "get_dual_exam_fee", "get_dual_exam_stats", "get_industry_training_stats",
        "search_master_craftsmen", "get_master_stats", "search_master_artworks",
        "get_skill_stats", "get_eps_topik_schedule", "find_eps_topik_sites",
        "search_eps_jobseekers", "get_eps_return_support_stats", "get_eps_topik_materials",
        "search_eps_return_jobs", "get_eps_education_info",
    ],
    "korea-certified": [
        "recommend_qualifications", "build_career_roadmap", "check_legal_requirements",
        "search_qualification", "get_exam_schedule", "get_qualification_info",
        "get_exam_fee", "get_pass_rate", "find_test_sites", "get_disqualification_info",
        "get_exam_questions", "search_overseas_jobs", "get_overseas_employment_stats",
        "search_overseas_companies", "search_overseas_top_jobs", "get_overseas_job_fairs",
        "get_overseas_country_info", "get_overseas_hiring_stats", "get_overseas_settlement_support",
        "search_overseas_resources", "search_kmove_programs", "search_ncs",
        "get_ncs_related_qualifications", "search_ncs_learning_materials", "search_blind_hiring",
        "get_ncs_core_competencies", "get_ncs_classification", "search_course_qualifications",
        "search_work_learning_qualifications", "search_dual_training_orgs", "find_dual_exam_sites",
        "get_dual_exam_fee", "get_dual_exam_stats", "get_industry_training_stats",
        "search_master_craftsmen", "get_master_stats", "search_master_artworks",
        "get_skill_stats", "get_eps_topik_schedule", "find_eps_topik_sites",
        "search_eps_jobseekers", "get_eps_return_support_stats", "get_eps_topik_materials",
        "search_eps_return_jobs", "get_eps_education_info",
    ],
}


class ToolSelectionDialog(tk.Toplevel):
    """MCP 서버의 Tool을 개별 선택, 전체 선택/해제, 추가할 수 있는 다이얼로그"""
    def __init__(self, parent, server_name, tools_var, app_ref, config=None):
        super().__init__(parent)
        self.server_name = server_name
        self.tools_var = tools_var
        self.app_ref = app_ref
        self.config = config or {}

        self.title(f"MCP Tool 선택 - {server_name}")
        self.geometry("580x620")
        self.minsize(480, 440)
        self.transient(parent)
        self.grab_set()

        # 현재 입력된 툴 파싱
        current_text = self.tools_var.get().strip()
        self.selected_set = set(x.strip() for x in current_text.split(",") if x.strip())

        # 초기 툴 목록 구성 (프리셋 + 현재 텍스트에 이미 입력된 툴)
        preset_tools = KNOWN_MCP_TOOLS.get(server_name.lower(), [])
        self.all_tools = list(dict.fromkeys(preset_tools + list(self.selected_set)))

        self.check_vars = {}
        self.new_tool_var = tk.StringVar()
        self.status_var = tk.StringVar()

        self._build_ui()
        self._refresh_list()
        self._update_status()

        # 다이얼로그를 부모 창의 오른쪽 옆에 배치 (글이나 이름이 길어져도 가리지 않고 볼 수 있게)
        self.update_idletasks()
        try:
            screen_w = self.winfo_screenwidth()
            dialog_w = self.winfo_width()
            desired_x = parent.winfo_rootx() + parent.winfo_width() + 12
            if desired_x + dialog_w > screen_w:
                desired_x = max(10, parent.winfo_rootx() + parent.winfo_width() - dialog_w - 24)
            py = max(10, parent.winfo_rooty() + 40)
            self.geometry(f"+{desired_x}+{py}")
        except Exception:
            pass

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # 상단 안내 영역
        top_frame = ttk.Frame(self, padding=(14, 10, 14, 6))
        top_frame.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            top_frame,
            text=f"MCP 서버: {self.server_name}",
            font=("맑은 고딕", 11, "bold")
        ).pack(anchor="w")
        ttk.Label(
            top_frame,
            text="사용할 Tool을 선택하세요. (모두 체크 해제 시 모든 Tool이 허용됩니다)",
            foreground="#555555"
        ).pack(anchor="w", pady=(2, 0))

        # 툴바 (전체 선택 / 전체 해제 / Tool 자동 감지)
        tb_frame = ttk.Frame(self, padding=(14, 2, 14, 6))
        tb_frame.grid(row=1, column=0, sticky="ew")
        ttk.Button(tb_frame, text="전체 선택", command=self.select_all).pack(side="left", padx=(0, 4))
        ttk.Button(tb_frame, text="전체 해제", command=self.deselect_all).pack(side="left", padx=4)
        ttk.Button(tb_frame, text="Tool 자동 감지", command=self.fetch_tools).pack(side="right")

        # 중앙 리스트 (Canvas + Scrollbar)
        list_container = ttk.LabelFrame(self, text="Tool 목록 (체크된 Tool만 허용)", padding=6)
        list_container.grid(row=2, column=0, padx=14, pady=4, sticky="nsew")
        list_container.columnconfigure(0, weight=1)
        list_container.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(list_container, borderwidth=0, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.canvas.yview)
        self.inner_frame = ttk.Frame(self.canvas)

        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")

        self.canvas_window = self.canvas.create_window((0, 0), window=self.inner_frame, anchor="nw")
        self.inner_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width))

        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # 신규 Tool 수동 추가 영역
        add_frame = ttk.Frame(self, padding=(14, 6, 14, 6))
        add_frame.grid(row=3, column=0, sticky="ew")
        add_frame.columnconfigure(1, weight=1)
        ttk.Label(add_frame, text="새 Tool 직접 추가:").grid(row=0, column=0, padx=(0, 6))
        add_entry = ttk.Entry(add_frame, textvariable=self.new_tool_var)
        add_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        add_entry.bind("<Return>", lambda e: self.add_tool())
        ttk.Button(add_frame, text="추가", command=self.add_tool).grid(row=0, column=2)

        # 하단 상태 표시 & 액션 버튼
        bottom_frame = ttk.Frame(self, padding=(14, 6, 14, 12))
        bottom_frame.grid(row=4, column=0, sticky="ew")
        ttk.Label(bottom_frame, textvariable=self.status_var).pack(side="left")
        ttk.Button(bottom_frame, text="취소", command=self._close).pack(side="right", padx=(6, 0))
        ttk.Button(bottom_frame, text="선택 적용 (OK)", command=self.apply_selection).pack(side="right")

    def _on_mousewheel(self, event):
        if self.winfo_exists():
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _close(self):
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self.destroy()

    def _refresh_list(self):
        for w in self.inner_frame.winfo_children():
            w.destroy()

        if not self.all_tools:
            ttk.Label(
                self.inner_frame,
                text="등록된 Tool이 없습니다.\n[LM Studio에서 Tool 감지]를 누르거나 직접 추가하세요.",
                foreground="#777777",
                padding=10
            ).pack(anchor="w")
            return

        for tool in self.all_tools:
            var = self.check_vars.get(tool)
            if var is None:
                var = tk.BooleanVar(value=(tool in self.selected_set))
                self.check_vars[tool] = var

            row = ttk.Frame(self.inner_frame)
            row.pack(fill="x", pady=2, padx=4)

            cb = ttk.Checkbutton(
                row,
                text=tool,
                variable=var,
                command=self._update_status
            )
            cb.pack(side="left", fill="x", expand=True)

            del_btn = ttk.Button(
                row,
                text="✕",
                width=3,
                command=lambda t=tool: self.remove_tool(t)
            )
            del_btn.pack(side="right", padx=2)

    def _update_status(self):
        sel_count = sum(1 for v in self.check_vars.values() if v.get())
        total_count = len(self.all_tools)
        if sel_count == 0:
            self.status_var.set(f"선택: 0 / {total_count}개 (전체 Tool 허용)")
        else:
            self.status_var.set(f"선택: {sel_count} / {total_count}개 허용")

    def select_all(self):
        for v in self.check_vars.values():
            v.set(True)
        self._update_status()

    def deselect_all(self):
        for v in self.check_vars.values():
            v.set(False)
        self._update_status()

    def add_tool(self):
        val = self.new_tool_var.get().strip()
        if not val:
            return
        tools = [x.strip() for x in val.split(",") if x.strip()]
        for t in tools:
            if t not in self.all_tools:
                self.all_tools.append(t)
            self.check_vars[t] = tk.BooleanVar(value=True)
        self.new_tool_var.set("")
        self._refresh_list()
        self._update_status()

    def remove_tool(self, tool):
        if tool in self.all_tools:
            self.all_tools.remove(tool)
        if tool in self.check_vars:
            del self.check_vars[tool]
        self._refresh_list()
        self._update_status()

    def apply_selection(self):
        selected = [t for t in self.all_tools if self.check_vars.get(t) and self.check_vars[t].get()]
        self.tools_var.set(", ".join(selected))
        self._close()

    def fetch_tools(self):
        # 1. 원격 MCP 서버(URL) 직접 조회 시도 (표준 MCP JSON-RPC)
        url = self.config.get("url") if isinstance(self.config, dict) else None
        if url:
            try:
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json"
                }
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                r = requests.post(url, headers=headers, json=payload, timeout=8, verify=False)
                if r.ok:
                    content = r.content.decode("utf-8", errors="ignore")
                    detected = []
                    # SSE data: {...} 라인 파싱
                    for line in content.splitlines():
                        line = line.strip()
                        if line.startswith("data:"):
                            try:
                                d = json.loads(line[5:].strip())
                                for t in d.get("result", {}).get("tools", []):
                                    if "name" in t and t["name"] not in detected:
                                        detected.append(t["name"])
                            except Exception:
                                pass
                    # 일반 JSON 파싱
                    if not detected:
                        try:
                            d = r.json()
                            for t in d.get("result", {}).get("tools", []):
                                if "name" in t and t["name"] not in detected:
                                    detected.append(t["name"])
                        except Exception:
                            pass

                    if detected:
                        added_count = 0
                        for t in detected:
                            if t not in self.all_tools:
                                self.all_tools.append(t)
                                added_count += 1
                            if t not in self.check_vars:
                                self.check_vars[t] = tk.BooleanVar(value=True)

                        self._refresh_list()
                        self._update_status()
                        sample_str = ", ".join(detected[:10]) + ("..." if len(detected) > 10 else "")
                        messagebox.showinfo(
                            "Tool 감지 성공",
                            f"MCP 서버({self.server_name})로부터 {len(detected)}개 Tool을 직접 감지했습니다!\n(새로 추가된 Tool: {added_count}개)\n\n{sample_str}"
                        )
                        return
            except Exception:
                pass

        # 2. LM Studio API를 통한 probe 감지
        if not self.app_ref:
            messagebox.showwarning("오류", "메인 앱 참조를 찾을 수 없습니다.")
            return

        base_url = self.app_ref.base()
        token = self.app_ref.api_token.get().strip()
        model = self.app_ref.model.get().strip()

        if not model:
            # 모델이 없더라도 내장 프리셋이 있으면 프리셋으로 자동 채움
            preset = KNOWN_MCP_TOOLS.get(self.server_name.lower(), [])
            if preset:
                added = 0
                for t in preset:
                    if t not in self.all_tools:
                        self.all_tools.append(t)
                        added += 1
                    if t not in self.check_vars:
                        self.check_vars[t] = tk.BooleanVar(value=True)
                self._refresh_list()
                self._update_status()
                messagebox.showinfo(
                    "프리셋 로드 완료",
                    f"기본 내장된 {len(preset)}개 Tool 목록을 불러왔습니다."
                )
                return

            messagebox.showwarning(
                "모델 필요",
                "LM Studio에서 Tool을 조회하려면 먼저 상단에서 '모델 조회' 후 'Model'을 선택해주세요."
            )
            return

        headers = self.app_ref.headers()
        # LM Studio의 artifact identifier는 소문자/하이픈만 허용하므로 소문자로 정규화
        safe_server_id = self.server_name.lower().replace("_", "-")
        payload = {
            "model": model,
            "input": "tool_probe",
            "integrations": [
                {
                    "type": "plugin",
                    "id": f"mcp/{safe_server_id}",
                    "allowed_tools": ["__probe_inspect_tools__"]
                }
            ]
        }

        try:
            r = requests.post(f"{base_url}/api/v1/chat", headers=headers, json=payload, timeout=10)
            text = r.text
            # 에러 메시지에서 Available: tool1, tool2, tool3... 추출
            m = re.search(r"Available:\s*([^\"'\n\r\}]+)", text)
            if m:
                raw_tools = m.group(1).split(",")
                detected = [x.strip() for x in raw_tools if x.strip()]
                added_count = 0
                for t in detected:
                    if t not in self.all_tools:
                        self.all_tools.append(t)
                        added_count += 1
                    if t not in self.check_vars:
                        self.check_vars[t] = tk.BooleanVar(value=True)

                self._refresh_list()
                self._update_status()
                messagebox.showinfo(
                    "Tool 감지 성공",
                    f"LM Studio로부터 {len(detected)}개 Tool을 감지했습니다.\n(새로 추가된 Tool: {added_count}개)\n\n" + ", ".join(detected)
                )
            else:
                guide = ""
                if "Invalid artifact identifier format" in text:
                    guide = (
                        "\n\n[참고: 식별자 형식 안내]\n"
                        "LM Studio는 대문자가 들어간 서버 이름을 지원하지 않습니다.\n"
                        "mcp.json의 서버 이름을 소문자(예: korea-certified)로 변경하시면 LM Studio에서도 정상 인식됩니다."
                    )
                messagebox.showinfo(
                    "알림",
                    f"LM Studio 응답에서 Tool 목록을 찾지 못했습니다.\n응답 내용:\n{text[:300]}{guide}"
                )
        except Exception as e:
            messagebox.showerror("Tool 조회 실패", f"LM Studio 요청 중 오류가 발생했습니다:\n{e}")


class MCPRow:
    def __init__(self, master, server_name, config, row_no, app_ref=None):
        self.master = master
        self.server_name = server_name
        self.config = config
        self.app_ref = app_ref
        self.use_var = tk.BooleanVar(value=False)
        self.tools_var = tk.StringVar(value="")

        ttk.Checkbutton(master, variable=self.use_var).grid(row=row_no, column=0, padx=5, pady=4)
        ttk.Label(master, text=server_name, width=18).grid(row=row_no, column=1, padx=5, pady=4, sticky="w")
        ttk.Label(master, text="Remote" if "url" in config else "Local", width=8).grid(row=row_no, column=2, padx=5, pady=4)
        ttk.Entry(master, textvariable=self.tools_var).grid(row=row_no, column=3, padx=5, pady=4, sticky="ew")
        ttk.Button(master, text="Tool 선택", command=self.open_tool_dialog).grid(row=row_no, column=4, padx=5, pady=4)

        # Hugging Face MCP의 올바른 검색 Tool 이름 기본 제안
        if server_name.lower() in {"huggingface", "hf", "hf-mcp-server"}:
            self.tools_var.set("hub_repo_search")

    def open_tool_dialog(self):
        ToolSelectionDialog(
            self.master.winfo_toplevel(),
            self.server_name,
            self.tools_var,
            self.app_ref,
            config=self.config
        )

    def integration(self):
        if not self.use_var.get():
            return None
        # LM Studio 규격에 맞게 id를 소문자로 변환하여 전송 (대문자 식별자 오류 방지)
        safe_id = self.server_name.lower().replace("_", "-")
        item = {"type": "plugin", "id": f"mcp/{safe_id}"}
        tools = [x.strip() for x in self.tools_var.get().split(",") if x.strip()]
        if tools:
            item["allowed_tools"] = tools
        return item


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1260x820")
        self.minsize(1000, 660)

        self.base_url = tk.StringVar(value=DEFAULT_BASE_URL)
        self.api_token = tk.StringVar()
        self.model = tk.StringVar()
        self.mcp_path = tk.StringVar(value=str(DEFAULT_MCP_JSON))
        self.korean_only = tk.BooleanVar(value=True)
        self.hide_thinking = tk.BooleanVar(value=True)
        self.system_prompt = tk.StringVar(value=DEFAULT_SYSTEM_PROMPT)
        self.status = tk.StringVar(value="준비")
        self.previous_response_id = None
        self.mcp_rows = []
        self.uiq = queue.Queue()

        self._build_ui()
        self.after(100, self._poll_uiq)
        if DEFAULT_MCP_JSON.exists():
            self.load_mcp_json(silent=True)

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # 좌우 2열 분할 PanedWindow (글이 길어지더라도 시원하게 볼 수 있도록 우측에 Chat창 배치)
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 2))

        left_pane = ttk.Frame(paned, padding=4)
        right_pane = ttk.Frame(paned, padding=4)

        paned.add(left_pane, weight=4)   # 설정 및 MCP/로그 영역 (약 45%)
        paned.add(right_pane, weight=6)  # Chat 대화 및 입력 영역 (약 55% 이상)

        # ==========================================
        # 좌측 패널 (Left Pane): 설정 / MCP / 로그
        # ==========================================
        left_pane.columnconfigure(0, weight=1)
        left_pane.rowconfigure(2, weight=1)

        # 1. LM Studio 연결
        f = ttk.LabelFrame(left_pane, text="1. LM Studio 연결 및 지침")
        f.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)

        ttk.Label(f, text="Base URL").grid(row=0, column=0, padx=5, pady=5)
        ttk.Entry(f, textvariable=self.base_url).grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        ttk.Label(f, text="API Token").grid(row=0, column=2, padx=5, pady=5)
        ttk.Entry(f, textvariable=self.api_token, show="*").grid(row=0, column=3, padx=5, pady=5, sticky="ew")
        ttk.Button(f, text="모델 조회", command=self.refresh_models).grid(row=0, column=4, padx=5, pady=5)

        ttk.Label(f, text="Model").grid(row=1, column=0, padx=5, pady=5)
        self.model_combo = ttk.Combobox(f, textvariable=self.model, state="normal")
        self.model_combo.grid(row=1, column=1, columnspan=3, padx=5, pady=5, sticky="ew")
        ttk.Button(f, text="연결 테스트", command=self.test_connection).grid(row=1, column=4, padx=5, pady=5)

        # 한국어 전용 옵션 및 System Prompt 설정
        ttk.Label(f, text="언어 지침").grid(row=2, column=0, padx=5, pady=5)
        ttk.Checkbutton(
            f,
            text="한국어로만 답변 (한영 병기 및 영어 문장 출력 금지)",
            variable=self.korean_only
        ).grid(row=2, column=1, columnspan=3, padx=5, pady=5, sticky="w")
        ttk.Button(f, text="지침 초기화", command=self.reset_system_prompt).grid(row=2, column=4, padx=5, pady=5)

        # 사고 과정 및 Raw 데이터 숨기기 옵션
        ttk.Label(f, text="출력 옵션").grid(row=3, column=0, padx=5, pady=5)
        ttk.Checkbutton(
            f,
            text="AI 사고 과정(Thinking) 및 Tool 원시 데이터 숨기기",
            variable=self.hide_thinking
        ).grid(row=3, column=1, columnspan=4, padx=5, pady=5, sticky="w")

        ttk.Label(f, text="System Prompt").grid(row=4, column=0, padx=5, pady=5)
        ttk.Entry(f, textvariable=self.system_prompt).grid(row=4, column=1, columnspan=4, padx=5, pady=5, sticky="ew")

        # 2. MCP 선택
        m = ttk.LabelFrame(left_pane, text="2. MCP 서버 / Tool 선택")
        m.grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        m.columnconfigure(0, weight=1)

        bar = ttk.Frame(m)
        bar.grid(row=0, column=0, padx=6, pady=4, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="mcp.json").grid(row=0, column=0, padx=(0, 4))
        ttk.Entry(bar, textvariable=self.mcp_path).grid(row=0, column=1, sticky="ew")
        ttk.Button(bar, text="찾기", command=self.browse_mcp).grid(row=0, column=2, padx=2)
        ttk.Button(bar, text="새로 읽기", command=self.load_mcp_json).grid(row=0, column=3, padx=2)
        ttk.Button(bar, text="전체 선택", command=self.select_all_servers).grid(row=0, column=4, padx=2)
        ttk.Button(bar, text="전체 해제", command=self.deselect_all_servers).grid(row=0, column=5, padx=2)

        ttk.Label(
            m,
            text="[Tool 선택]으로 사용할 Tool을 지정하세요. (비워두면 모든 Tool 허용)",
            foreground="#555555"
        ).grid(row=1, column=0, padx=8, pady=(0, 4), sticky="w")

        self.mcp_frame = ttk.Frame(m)
        self.mcp_frame.grid(row=2, column=0, padx=4, pady=(0, 4), sticky="ew")
        self.mcp_frame.columnconfigure(3, weight=1)
        ttk.Label(self.mcp_frame, text="사용").grid(row=0, column=0, padx=4)
        ttk.Label(self.mcp_frame, text="MCP 서버").grid(row=0, column=1, padx=4)
        ttk.Label(self.mcp_frame, text="형식").grid(row=0, column=2, padx=4)
        ttk.Label(self.mcp_frame, text="allowed_tools (선택된 Tool)").grid(row=0, column=3, padx=4, sticky="w")
        ttk.Label(self.mcp_frame, text="Tool 관리").grid(row=0, column=4, padx=4)

        # 3. 로그 창 (좌측 하단)
        l = ttk.LabelFrame(left_pane, text="MCP / 응답 로그")
        l.grid(row=2, column=0, padx=4, pady=4, sticky="nsew")
        l.columnconfigure(0, weight=1)
        l.rowconfigure(0, weight=1)
        self.log = tk.Text(l, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        sb_log = ttk.Scrollbar(l, orient="vertical", command=self.log.yview)
        sb_log.grid(row=0, column=1, padx=(0, 6), pady=6, sticky="ns")
        self.log.configure(yscrollcommand=sb_log.set)

        # ==========================================
        # 우측 패널 (Right Pane): Chat 대화창 (글이 길어져도 시원하게 표시) & 메시지 입력
        # ==========================================
        right_pane.columnconfigure(0, weight=1)
        right_pane.rowconfigure(0, weight=1)

        # 3. Chat (우측 상단 전체 차지)
        c = ttk.LabelFrame(right_pane, text="3. Chat (대화 내용)")
        c.grid(row=0, column=0, padx=4, pady=4, sticky="nsew")
        c.columnconfigure(0, weight=1)
        c.rowconfigure(0, weight=1)

        self.chat = tk.Text(c, wrap="word", state="disabled", font=("맑은 고딕", 10))
        self.chat.grid(row=0, column=0, padx=(6, 0), pady=6, sticky="nsew")
        sb_chat = ttk.Scrollbar(c, orient="vertical", command=self.chat.yview)
        sb_chat.grid(row=0, column=1, padx=(0, 6), pady=6, sticky="ns")
        self.chat.configure(yscrollcommand=sb_chat.set)

        # 4. 메시지 입력 (우측 하단)
        inp = ttk.LabelFrame(right_pane, text="4. 메시지 입력")
        inp.grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        inp.columnconfigure(0, weight=1)
        self.input = tk.Text(inp, height=5, wrap="word", font=("맑은 고딕", 10))
        self.input.grid(row=0, column=0, rowspan=3, padx=6, pady=6, sticky="ew")
        self.send_btn = ttk.Button(inp, text="전송", command=self.send_message)
        self.send_btn.grid(row=0, column=1, padx=6, pady=(6, 2), sticky="ew")
        ttk.Button(inp, text="새 대화", command=self.new_chat).grid(row=1, column=1, padx=6, pady=2, sticky="ew")
        ttk.Button(inp, text="대화 지우기", command=self.clear_chat).grid(row=2, column=1, padx=6, pady=(2, 6), sticky="ew")
        self.input.bind("<Control-Return>", lambda e: self.send_message())

        # 최하단 상태 표시줄
        ttk.Label(self, textvariable=self.status, padding=(10, 4)).grid(row=1, column=0, sticky="w")

    def headers(self):
        h = {"Content-Type": "application/json"}
        token = self.api_token.get().strip()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def base(self):
        return self.base_url.get().strip().rstrip("/")

    def append_chat(self, who, text):
        self.chat.configure(state="normal")
        self.chat.insert("end", f"\n[{who}]\n{text}\n")
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def run_bg(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _poll_uiq(self):
        try:
            while True:
                fn, args = self.uiq.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.after(100, self._poll_uiq)

    # ---------- MCP ----------
    def browse_mcp(self):
        p = filedialog.askopenfilename(title="mcp.json 선택", filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if p:
            self.mcp_path.set(p)
            self.load_mcp_json()

    def load_mcp_json(self, silent=False):
        try:
            path = Path(self.mcp_path.get().strip()).expanduser()
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError("mcpServers 항목이 올바르지 않습니다.")

            for w in self.mcp_frame.grid_slaves():
                if int(w.grid_info().get("row", 0)) >= 1:
                    w.destroy()
            self.mcp_rows.clear()

            for i, (name, cfg) in enumerate(servers.items(), start=1):
                row = MCPRow(self.mcp_frame, name, cfg if isinstance(cfg, dict) else {}, i, app_ref=self)
                self.mcp_rows.append(row)

            if not silent:
                self.append_log(f"[MCP] {len(self.mcp_rows)}개 서버 로드: {', '.join(servers.keys())}")
                self.status.set(f"MCP 서버 {len(self.mcp_rows)}개 로드 완료")
        except Exception as e:
            if not silent:
                messagebox.showerror("mcp.json 읽기 실패", str(e))

    def select_all_servers(self):
        for row in self.mcp_rows:
            row.use_var.set(True)
        self.status.set(f"모든 MCP 서버 ({len(self.mcp_rows)}개) 선택됨")

    def deselect_all_servers(self):
        for row in self.mcp_rows:
            row.use_var.set(False)
        self.status.set("모든 MCP 서버 선택 해제됨")

    def integrations(self):
        out = []
        for row in self.mcp_rows:
            x = row.integration()
            if x:
                out.append(x)
        return out

    # ---------- Models ----------
    def refresh_models(self):
        self.status.set("모델 조회 중...")
        self.run_bg(self._refresh_models_worker)

    def _refresh_models_worker(self):
        try:
            r = requests.get(f"{self.base()}/api/v1/models", headers=self.headers(), timeout=15)
            r.raise_for_status()
            data = r.json()
            items = data.get("data") or data.get("models") or [] if isinstance(data, dict) else data
            models = []
            if isinstance(items, list):
                for x in items:
                    if isinstance(x, str):
                        models.append(x)
                    elif isinstance(x, dict):
                        mid = x.get("id") or x.get("model") or x.get("name") or x.get("key")
                        if mid:
                            models.append(str(mid))
            self.uiq.put((self._set_models, (list(dict.fromkeys(models)),)))
        except Exception as e:
            self.uiq.put((self._error, ("모델 조회 실패", str(e))))

    def _set_models(self, models):
        self.model_combo["values"] = models
        if models and not self.model.get().strip():
            self.model.set(models[0])
        self.append_log(f"[MODEL] {len(models)}개 조회")
        self.status.set("모델 조회 완료")

    def test_connection(self):
        self.run_bg(self._test_worker)

    def _test_worker(self):
        try:
            r = requests.get(f"{self.base()}/api/v1/models", headers=self.headers(), timeout=10)
            r.raise_for_status()
            self.uiq.put((self._connection_ok, ()))
        except Exception as e:
            self.uiq.put((self._error, ("연결 실패", str(e))))

    def _connection_ok(self):
        self.status.set("LM Studio 연결됨")
        self.append_log("[OK] LM Studio 연결 성공")

    def reset_system_prompt(self):
        self.system_prompt.set(DEFAULT_SYSTEM_PROMPT)
        self.korean_only.set(True)
        self.hide_thinking.set(True)
        self.status.set("시스템 프롬프트 기본값 복원 완료")
        self.append_log("[PROMPT] 한국어 전용 및 사고과정 숨김 기본 설정 복원")

    # ---------- Chat ----------
    def send_message(self):
        text = self.input.get("1.0", "end").strip()
        if not text:
            return
        model = self.model.get().strip()
        if not model:
            messagebox.showwarning("모델 필요", "사용할 모델을 선택하세요.")
            return

        mcp = self.integrations()
        self.append_chat("사용자", text)
        self.append_log("[MCP] " + json.dumps(mcp, ensure_ascii=False))
        self.input.delete("1.0", "end")
        self.send_btn.configure(state="disabled")
        self.status.set("응답 생성 중...")
        self.run_bg(self._chat_worker, text, model, mcp)

    def _chat_worker(self, text, model, mcp):
        try:
            payload = {"model": model, "input": text}
            if mcp:
                payload["integrations"] = mcp
            if self.previous_response_id:
                payload["previous_response_id"] = self.previous_response_id

            # 시스템 프롬프트 및 지침 주입 (한영 병기 원천 방지 및 불필요한 사고과정/Raw JSON 노출 억제)
            directives = []
            if self.korean_only.get():
                directives.append(
                    "1. 모든 답변은 반드시 자연스럽고 유려한 순수 한국어로만 작성하십시오. "
                    "절대로 영어를 병기하거나 영문 번역 문장을 함께 출력하지 마십시오."
                )
            if self.hide_thinking.get():
                directives.append(
                    "2. Thinking Process, Step-by-step 내부 계획, 추론 과정 등은 출력하지 마십시오. "
                    "오직 사용자를 위한 최종 답변만 제시하십시오."
                )
                directives.append(
                    "3. 도구(Tool) 실행 결과의 원시 JSON 데이터나 코드 블록을 그대로 출력하지 말고, "
                    "조회된 핵심 정보를 알기 쉽게 한국어로 요약하여 설명하십시오."
                )

            sys_text = self.system_prompt.get().strip()
            sys_parts = []
            if directives:
                sys_parts.append("[지침]\n" + "\n".join(directives))
            if sys_text:
                if sys_text != DEFAULT_SYSTEM_PROMPT:
                    sys_parts.append("[추가 지침]\n" + sys_text)
                elif not directives:
                    sys_parts.append(sys_text)

            if sys_parts:
                payload["system_prompt"] = "\n\n".join(sys_parts)

            r = requests.post(
                f"{self.base()}/api/v1/chat",
                headers=self.headers(),
                json=payload,
                timeout=300
            )
            if not r.ok:
                try:
                    detail = json.dumps(r.json(), ensure_ascii=False, indent=2)
                except Exception:
                    detail = r.text
                raise RuntimeError(f"HTTP {r.status_code}\n{detail}")

            data = r.json()
            rid = data.get("response_id") or data.get("id") if isinstance(data, dict) else None
            answer, thoughts = self.extract_text(data)
            self.uiq.put((self._chat_ok, (answer, rid, data, thoughts)))
        except Exception as e:
            self.uiq.put((self._chat_error, (str(e),)))

    def extract_text(self, data):
        """
        LM Studio /api/v1/chat 또는 OpenAI 호환 API 응답에서
        (최종_답변_텍스트, 사고과정_및_원시도구데이터)를 분리하여 추출합니다.
        """
        if not isinstance(data, dict):
            return clean_answer(str(data))

        message_texts = []
        thought_texts = []

        # 1. LM Studio 표준 응답 포맷 (output 배열 구조 파싱)
        output_list = data.get("output")
        if isinstance(output_list, list):
            for item in output_list:
                if not isinstance(item, dict):
                    continue
                itype = str(item.get("type", "")).lower()
                role = str(item.get("role", "")).lower()
                content = item.get("content")

                # (1) 실제 AI 사용자 메시지
                if itype in ("message", "text") or role == "assistant":
                    if isinstance(content, str) and content.strip():
                        message_texts.append(content.strip())
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("text"):
                                message_texts.append(c["text"].strip())
                            elif isinstance(c, str) and c.strip():
                                message_texts.append(c.strip())

                # (2) 모델 내부 사고 과정 (Thinking / Reasoning)
                elif itype in ("reasoning", "thought", "thinking"):
                    if isinstance(content, str) and content.strip():
                        thought_texts.append(content.strip())

                # (3) 도구 호출 및 도구 실행 결과 (Raw Tool Result)
                elif itype in ("tool_result", "tool_call", "tool_use", "action"):
                    t_str = ""
                    if isinstance(content, str):
                        t_str = content.strip()
                    elif isinstance(content, (dict, list)):
                        t_str = json.dumps(content, ensure_ascii=False)
                    if t_str:
                        thought_texts.append(f"[{itype.upper()}]\n{t_str}")

        # 2. OpenAI 호환 포맷 (choices 구조 파싱)
        if not message_texts:
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                for ch in choices:
                    if not isinstance(ch, dict):
                        continue
                    msg = ch.get("message", {})
                    if isinstance(msg, dict):
                        # reasoning_content (DeepSeek / Qwen 등 모델의 사고 과정)
                        if msg.get("reasoning_content"):
                            thought_texts.append(str(msg["reasoning_content"]).strip())
                        c = msg.get("content")
                        if isinstance(c, str) and c.strip():
                            message_texts.append(c.strip())
                    elif isinstance(ch.get("text"), str) and ch["text"].strip():
                        message_texts.append(ch["text"].strip())

        # 3. 단일 텍스트 필드 fallback
        if not message_texts:
            for key in ("output_text", "text", "response"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    message_texts.append(val.strip())
                    break

        # 4. 일반 fallback (구조를 알 수 없을 때의 안전한 재귀 탐색)
        if not message_texts:
            def collect(obj):
                res = []
                if isinstance(obj, str):
                    res.append(obj)
                elif isinstance(obj, list):
                    for x in obj:
                        res.extend(collect(x))
                elif isinstance(obj, dict):
                    dtype = str(obj.get("type", "")).lower()
                    if dtype in ("reasoning", "thought", "thinking", "tool_result", "tool_call"):
                        c = obj.get("content") or obj.get("text")
                        if c:
                            thought_texts.append(str(c))
                        return res
                    if isinstance(obj.get("text"), str):
                        res.append(obj["text"])
                    elif isinstance(obj.get("content"), str):
                        res.append(obj["content"])
                    else:
                        for k in ("message", "content", "output", "choices"):
                            if k in obj:
                                res.extend(collect(obj[k]))
                return res

            collected = [x.strip() for x in collect(data) if x.strip()]
            if collected:
                message_texts = list(dict.fromkeys(collected))
            else:
                message_texts = [json.dumps(data, ensure_ascii=False, indent=2)]

        raw_answer = "\n\n".join(dict.fromkeys(message_texts)).strip()

        # 5. 텍스트 레벨 2차 정제 (Thinking Process:, *(Self-Correction...)*, Raw JSON 분리)
        clean_ans, inline_thoughts = clean_answer(raw_answer)
        if inline_thoughts:
            thought_texts.append(inline_thoughts)

        all_thoughts = "\n\n".join(t for t in thought_texts if t.strip()).strip()
        return clean_ans, all_thoughts

    def _chat_ok(self, answer, rid, raw, thoughts=""):
        if rid:
            self.previous_response_id = rid

        # 사고 과정 분리 및 숨기기 처리
        if self.hide_thinking.get():
            final_display = answer if answer else "(답변 내용이 없습니다)"
            if thoughts:
                self.append_log("[THINKING / TOOL RAW DATA]\n" + thoughts)
        else:
            if thoughts:
                final_display = f"[AI 사고 및 도구 데이터]\n{thoughts}\n\n[최종 답변]\n{answer}"
            else:
                final_display = answer

        self.append_chat("AI", final_display)
        if isinstance(raw, dict) and raw.get("usage"):
            self.append_log("[USAGE] " + json.dumps(raw["usage"], ensure_ascii=False))
        self.send_btn.configure(state="normal")
        self.status.set("응답 완료")

    def _chat_error(self, msg):
        self.append_chat("오류", msg)
        self.append_log("[ERROR] " + msg)
        self.send_btn.configure(state="normal")
        self.status.set("오류 발생")

    def new_chat(self):
        self.previous_response_id = None
        self.status.set("새 대화 시작")
        self.append_log("[CHAT] previous_response_id 초기화")

    def clear_chat(self):
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")

    def _error(self, title, msg):
        self.status.set(title)
        messagebox.showerror(title, msg)


if __name__ == "__main__":
    App().mainloop()
