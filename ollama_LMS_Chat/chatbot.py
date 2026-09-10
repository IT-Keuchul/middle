# -*- coding: utf-8 -*-
import json, queue, threading, tkinter as tk
from tkinter import ttk, messagebox
import requests

LM_URL = "http://127.0.0.1:1234"
OLLAMA_URL = "http://127.0.0.1:11434"

class App:
    def __init__(self, root):
        self.root = root
        root.title("LM Studio / Ollama Chat")
        root.geometry("960x720")
        root.minsize(840, 620)

        self.q = queue.Queue()
        self.history = []

        self.backend = tk.StringVar(value="LM Studio")
        self.server = tk.StringVar(value=LM_URL)
        self.key = tk.StringVar()
        self.model = tk.StringVar()
        self.status = tk.StringVar(value="연결 대기")
        self.temp = tk.DoubleVar(value=0.7)
        self.system = tk.StringVar(value="당신은 친절하고 정확한 AI 도우미입니다.")

        self.build_ui()
        root.after(100, self.process_events)

    def build_ui(self):
        conn = ttk.LabelFrame(self.root, text="서버 연결")
        conn.pack(fill="x", padx=10, pady=10)

        ttk.Label(conn, text="종류").grid(row=0, column=0, padx=5, pady=6)
        backend_box = ttk.Combobox(
            conn, textvariable=self.backend,
            values=["LM Studio", "Ollama"],
            state="readonly", width=12
        )
        backend_box.grid(row=0, column=1, padx=5)
        backend_box.bind("<<ComboboxSelected>>", self.change_backend)

        ttk.Label(conn, text="서버 주소").grid(row=0, column=2, padx=5)
        ttk.Entry(conn, textvariable=self.server, width=34).grid(
            row=0, column=3, padx=5, sticky="ew"
        )

        ttk.Label(conn, text="API Key").grid(row=0, column=4, padx=5)
        ttk.Entry(conn, textvariable=self.key, show="*", width=20).grid(
            row=0, column=5, padx=5
        )

        ttk.Button(
            conn, text="모델 조회 및 연결",
            command=self.fetch_models
        ).grid(row=0, column=6, padx=5)

        ttk.Label(conn, text="모델").grid(row=1, column=0, padx=5, pady=6)
        self.model_box = ttk.Combobox(
            conn, textvariable=self.model,
            state="readonly", width=45
        )
        self.model_box.grid(
            row=1, column=1, columnspan=3,
            padx=5, sticky="ew"
        )

        ttk.Label(conn, text="Temperature").grid(row=1, column=4, padx=5)
        ttk.Scale(
            conn, from_=0.0, to=1.5,
            variable=self.temp, orient="horizontal"
        ).grid(row=1, column=5, padx=5, sticky="ew")

        ttk.Label(conn, textvariable=self.status).grid(
            row=1, column=6, padx=5
        )

        conn.columnconfigure(3, weight=1)

        sysf = ttk.LabelFrame(self.root, text="시스템 프롬프트")
        sysf.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Entry(sysf, textvariable=self.system).pack(
            fill="x", padx=8, pady=8
        )

        chatf = ttk.LabelFrame(self.root, text="대화")
        chatf.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        self.chat = tk.Text(chatf, wrap="word", state="disabled")
        self.chat.pack(fill="both", expand=True, padx=8, pady=8)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=10, pady=(0, 10))

        self.input = tk.Text(bottom, height=5, wrap="word")
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Control-Return>", lambda e: self.send_message())

        btns = ttk.Frame(bottom)
        btns.pack(side="right", padx=(8, 0))

        ttk.Button(
            btns, text="전송\nCtrl+Enter",
            command=self.send_message
        ).pack(fill="x")

        ttk.Button(
            btns, text="대화 초기화",
            command=self.clear_chat
        ).pack(fill="x", pady=5)

    def change_backend(self, event=None):
        self.server.set(
            LM_URL if self.backend.get() == "LM Studio"
            else OLLAMA_URL
        )
        self.model_box["values"] = []
        self.model.set("")
        self.history.clear()
        self.status.set("연결 대기")

    def headers(self):
        headers = {"Content-Type": "application/json"}
        key = self.key.get().strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def fetch_models(self):
        self.status.set("연결 중...")
        threading.Thread(
            target=self._fetch_models_worker,
            daemon=True
        ).start()

    def _fetch_models_worker(self):
        try:
            base = self.server.get().strip().rstrip("/")

            if self.backend.get() == "LM Studio":
                r = requests.get(
                    base + "/v1/models",
                    headers=self.headers(),
                    timeout=10
                )
                r.raise_for_status()

                models = [
                    x["id"]
                    for x in r.json().get("data", [])
                    if x.get("id")
                ]
            else:
                r = requests.get(
                    base + "/api/tags",
                    timeout=10
                )
                r.raise_for_status()

                models = [
                    x["name"]
                    for x in r.json().get("models", [])
                    if x.get("name")
                ]

            self.q.put(("models", models))

        except Exception as e:
            self.q.put(("error", str(e)))

    def build_messages(self):
        messages = []

        system_prompt = self.system.get().strip()
        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt
            })

        messages.extend(self.history)
        return messages

    def send_message(self):
        text = self.input.get("1.0", "end").strip()

        if not text:
            return

        if not self.model.get():
            messagebox.showwarning(
                "모델 없음",
                "먼저 모델 조회 및 연결을 실행하세요."
            )
            return

        self.input.delete("1.0", "end")

        self.append_chat("사용자", text)
        self.append_chat("LLM", "")

        self.history.append({
            "role": "user",
            "content": text
        })

        self.status.set("응답 생성 중...")

        threading.Thread(
            target=self._chat_worker,
            args=(self.backend.get(), self.model.get()),
            daemon=True
        ).start()

    def _chat_worker(self, backend, model):
        try:
            base = self.server.get().strip().rstrip("/")
            full = []

            if backend == "LM Studio":
                body = {
                    "model": model,
                    "messages": self.build_messages(),
                    "temperature": float(self.temp.get()),
                    "stream": True
                }

                with requests.post(
                    base + "/v1/chat/completions",
                    headers=self.headers(),
                    json=body,
                    stream=True,
                    timeout=(10, 600)
                ) as r:
                    r.raise_for_status()

                    for line in r.iter_lines(decode_unicode=True):
                        if not line or not line.startswith("data:"):
                            continue

                        payload = line[5:].strip()

                        if payload == "[DONE]":
                            break

                        try:
                            obj = json.loads(payload)
                            token = (
                                obj["choices"][0]
                                .get("delta", {})
                                .get("content", "")
                            )

                            if token:
                                full.append(token)
                                self.q.put(("token", token))
                        except Exception:
                            pass

            else:
                body = {
                    "model": model,
                    "messages": self.build_messages(),
                    "stream": True,
                    "options": {
                        "temperature": float(self.temp.get())
                    }
                }

                with requests.post(
                    base + "/api/chat",
                    json=body,
                    stream=True,
                    timeout=(10, 600)
                ) as r:
                    r.raise_for_status()

                    for line in r.iter_lines(decode_unicode=True):
                        if not line:
                            continue

                        obj = json.loads(line)

                        token = (
                            obj.get("message", {})
                            .get("content", "")
                        )

                        if token:
                            full.append(token)
                            self.q.put(("token", token))

                        if obj.get("done"):
                            break

            answer = "".join(full)

            if answer:
                self.history.append({
                    "role": "assistant",
                    "content": answer
                })

            self.q.put(("done", None))

        except Exception as e:
            self.q.put(("error", str(e)))

    def append_chat(self, speaker, text):
        self.chat.config(state="normal")

        if speaker:
            self.chat.insert("end", f"\n[{speaker}]\n")

        self.chat.insert("end", text)
        self.chat.see("end")
        self.chat.config(state="disabled")

    def clear_chat(self):
        self.history.clear()

        self.chat.config(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.config(state="disabled")

        self.status.set("대화 초기화")

    def process_events(self):
        try:
            while True:
                kind, data = self.q.get_nowait()

                if kind == "models":
                    self.model_box["values"] = data

                    if data:
                        self.model.set(data[0])
                        self.status.set(
                            f"연결됨 / 모델 {len(data)}개"
                        )
                    else:
                        self.status.set(
                            "연결됨 / 모델 없음"
                        )

                elif kind == "token":
                    self.append_chat("", data)

                elif kind == "done":
                    self.append_chat("", "\n")
                    self.status.set("연결됨")

                elif kind == "error":
                    self.status.set("오류")

                    messagebox.showerror(
                        "오류",
                        data +
                        "\n\nLM Studio 기본 포트: 1234"
                        "\nOllama 기본 포트: 11434"
                    )

        except queue.Empty:
            pass

        self.root.after(
            100, self.process_events
        )


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
