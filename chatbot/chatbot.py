import sys
import os
import requests

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextBrowser, QTextEdit, QPushButton, QLineEdit, QLabel,
    QSplitter, QFrame
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

# QSS 스타일시트 정의 (세련된 다크 테마)
STYLE_SHEET = """
QMainWindow {
    background-color: #1e1e2e;
}
QWidget {
    font-family: 'Segoe UI', Malgun Gothic, sans-serif;
    color: #cdd6f4;
}
QFrame#Sidebar {
    background-color: #252538;
    border-right: 1px solid #313244;
}
QLabel#SidebarTitle {
    font-size: 16px;
    font-weight: bold;
    color: #89b4fa;
    margin-bottom: 10px;
}
QLabel#StatusLabel {
    font-size: 13px;
    font-weight: bold;
}
QLineEdit {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 10px;
    color: #cdd6f4;
    font-size: 13px;
}
QLineEdit:focus {
    border: 1px solid #89b4fa;
}
QPushButton {
    background-color: #89b4fa;
    color: #11111b;
    border: none;
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 13px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: #b4befe;
}
QPushButton:pressed {
    background-color: #74c7ec;
}
QPushButton#SecondaryBtn {
    background-color: #45475a;
    color: #cdd6f4;
    border: 1px solid #585b70;
}
QPushButton#SecondaryBtn:hover {
    background-color: #585b70;
}
QPushButton#SendBtn {
    background-color: #a6e3a1;
    color: #11111b;
}
QPushButton#SendBtn:hover {
    background-color: #94e2d5;
}
QTextBrowser {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 8px;
    padding: 10px;
    font-size: 14px;
    line-height: 1.5;
}
QTextEdit#InputEdit {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 8px;
    padding: 8px;
    color: #cdd6f4;
    font-size: 14px;
}
QTextEdit#InputEdit:focus {
    border: 1px solid #89b4fa;
}
"""


class ConnectionWorker(QThread):
    """LLM 연결 상태를 확인하는 비동기 워커"""
    finished = pyqtSignal(bool, str, list)  # (성공여부, 상태메시지, 모델목록)

    def __init__(self, host, port):
        super().__init__()
        self.host = host
        self.port = port

    def run(self):
        url = f"http://{self.host}:{self.port}/v1/models"
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                models = [model.get("id") for model in data.get("data", [])]
                if models:
                    self.finished.emit(True, f"연결 성공! ({len(models)}개 모델 로드됨)", models)
                else:
                    self.finished.emit(True, "연결 성공! (로드된 모델이 없습니다)", [])
            else:
                self.finished.emit(False, f"연결 실패 (HTTP {response.status_code})", [])
        except requests.exceptions.RequestException as e:
            self.finished.emit(False, f"연결 실패: {str(e)}", [])


class ChatWorker(QThread):
    """챗봇 대화를 전송하고 답변을 받아오는 비동기 워커"""
    success = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, host, port, model, messages):
        super().__init__()
        self.host = host
        self.port = port
        self.model = model
        self.messages = messages

    def run(self):
        url = f"http://{self.host}:{self.port}/v1/chat/completions"
        
        payload_messages = []
        for msg in self.messages:
            payload_messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        payload = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": 0.7
        }

        try:
            # 로컬 LLM의 느린 응답 및 이미지 분석 처리를 위해 타임아웃 무제한(None)으로 설정
            response = requests.post(url, json=payload, timeout=None)
            if response.status_code == 200:
                data = response.json()
                reply = data["choices"][0]["message"]["content"]
                self.success.emit(reply)
            else:
                self.error.emit(f"LLM 오류 (HTTP {response.status_code}): {response.text}")
        except requests.exceptions.Timeout:
            self.error.emit("LLM 서버 응답 시간이 초과되었습니다. 모델이 현재 다른 작업을 수행 중이거나 로드 중일 수 있습니다.")
        except requests.exceptions.RequestException as e:
            self.error.emit(f"API 요청 실패: {str(e)}")


class MessageEdit(QTextEdit):
    returnPressed = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.returnPressed.emit()
        else:
            super().keyPressEvent(event)


class ChatbotApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Local LLM Client - KNU SW")
        self.resize(950, 700)
        self.setStyleSheet(STYLE_SHEET)

        self.loaded_models = []
        self.selected_model = None
        
        # 채팅 히스토리 저장 [ {"role": "user"|"assistant", "content": "text"} ]
        self.chat_history = []

        self.init_ui()

    def init_ui(self):
        # 메인 레이아웃 분할
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        # ----------------------------------------------------
        # 1. 좌측 사이드바 (연결 설정)
        # ----------------------------------------------------
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(15, 20, 15, 20)
        sidebar_layout.setSpacing(15)

        title_label = QLabel("LLM 설정")
        title_label.setObjectName("SidebarTitle")
        sidebar_layout.addWidget(title_label)

        # IP 입력
        ip_label = QLabel("서버 IP 주소")
        sidebar_layout.addWidget(ip_label)
        
        self.ip_input = QLineEdit("localhost")
        sidebar_layout.addWidget(self.ip_input)

        # 포트 입력
        port_label = QLabel("서버 포트")
        sidebar_layout.addWidget(port_label)
        
        self.port_input = QLineEdit("1234")
        sidebar_layout.addWidget(self.port_input)

        # 연결 상태 표시 영역
        self.status_indicator = QLabel("● 연결 대기 중")
        self.status_indicator.setObjectName("StatusLabel")
        self.status_indicator.setStyleSheet("color: #fab387;")  # 주황색
        sidebar_layout.addWidget(self.status_indicator)

        self.status_detail = QLabel("서버 연결을 확인해주세요.")
        self.status_detail.setWordWrap(True)
        self.status_detail.setStyleSheet("color: #a6adc8; font-size: 12px;")
        sidebar_layout.addWidget(self.status_detail)

        # 연결 설정 버튼
        self.connect_btn = QPushButton("연결 설정 확인")
        self.connect_btn.clicked.connect(self.check_connection)
        sidebar_layout.addWidget(self.connect_btn)

        # 로드된 모델 표시
        self.model_label = QLabel("사용 중인 모델:")
        self.model_label.setStyleSheet("font-weight: bold; margin-top: 10px;")
        sidebar_layout.addWidget(self.model_label)

        self.model_name_display = QLabel("지정되지 않음")
        self.model_name_display.setWordWrap(True)
        self.model_name_display.setStyleSheet("color: #b4befe; font-size: 13px;")
        sidebar_layout.addWidget(self.model_name_display)

        sidebar_layout.addStretch()
        splitter.addWidget(sidebar)

        # ----------------------------------------------------
        # 2. 우측 영역 (채팅창 및 입력창)
        # ----------------------------------------------------
        chat_area = QWidget()
        chat_layout = QVBoxLayout(chat_area)
        chat_layout.setContentsMargins(15, 15, 15, 15)
        chat_layout.setSpacing(10)

        # 채팅 로그 브라우저
        self.chat_display = QTextBrowser()
        self.chat_display.setOpenExternalLinks(True)
        self.chat_display.setHtml(
            "<div style='color: #a6adc8; text-align: center; margin-top: 100px;'>"
            "<h3>Chatbot(KNU-SW)에 오신 것을 환영합니다!</h3>"
            "LLM 서버에 연결한 후 대화를 시작하세요."
            "</div>"
        )
        chat_layout.addWidget(self.chat_display, stretch=4)

        # 입력 팁 표시 레이블
        self.tip_label = QLabel("💡 Enter: 전송 | Shift + Enter: 줄바꿈")
        self.tip_label.setStyleSheet("color: #7f849c; font-size: 11px; margin-left: 5px;")
        chat_layout.addWidget(self.tip_label)

        # 입력 및 전송 구역
        input_container = QHBoxLayout()
        input_container.setSpacing(10)

        # 텍스트 입력창
        self.input_edit = MessageEdit()
        self.input_edit.setObjectName("InputEdit")
        self.input_edit.setPlaceholderText("메시지를 입력하세요...")
        self.input_edit.setFixedHeight(50)
        self.input_edit.returnPressed.connect(self.send_message)
        input_container.addWidget(self.input_edit, stretch=1)

        # 전송 버튼
        self.send_btn = QPushButton("전송")
        self.send_btn.setObjectName("SendBtn")
        self.send_btn.setFixedHeight(50)
        self.send_btn.clicked.connect(self.send_message)
        input_container.addWidget(self.send_btn)

        chat_layout.addLayout(input_container, stretch=1)
        splitter.addWidget(chat_area)

        # 두 영역 비율 설정
        splitter.setSizes([250, 700])

    def check_connection(self):
        """서버 IP 및 포트로 연결 테스트 비동기 실행"""
        host = self.ip_input.text().strip()
        port = self.port_input.text().strip()
        if not host:
            self.status_indicator.setText("● IP 에러")
            self.status_indicator.setStyleSheet("color: #f38ba8;")
            self.status_detail.setText("IP 주소 혹은 호스트명을 입력해주세요.")
            return
        if not port.isdigit():
            self.status_indicator.setText("● 포트 에러")
            self.status_indicator.setStyleSheet("color: #f38ba8;")
            self.status_detail.setText("포트 번호는 숫자여야 합니다.")
            return

        self.status_indicator.setText("● 확인 중...")
        self.status_indicator.setStyleSheet("color: #fab387;")
        self.status_detail.setText("LLM에 연결하는 중입니다...")
        self.connect_btn.setEnabled(False)

        self.conn_worker = ConnectionWorker(host, port)
        self.conn_worker.finished.connect(self.on_connection_checked)
        self.conn_worker.start()

    def on_connection_checked(self, success, message, models):
        self.connect_btn.setEnabled(True)
        if success:
            self.status_indicator.setText("● 연결 성공")
            self.status_indicator.setStyleSheet("color: #a6e3a1;")  # 녹색
            self.status_detail.setText(message)
            self.loaded_models = models
            if models:
                self.selected_model = models[0]
                self.model_name_display.setText(self.selected_model)
            else:
                self.selected_model = None
                self.model_name_display.setText("로드된 모델 없음 (LLM 서버에서 모델을 먼저 로드하세요)")
        else:
            self.status_indicator.setText("● 연결 실패")
            self.status_indicator.setStyleSheet("color: #f38ba8;")  # 적색
            self.status_detail.setText(message)
            self.selected_model = None
            self.model_name_display.setText("지정되지 않음")

    def send_message(self):
        """메시지 전송 프로세스 시작"""
        text = self.input_edit.toPlainText().strip()
        if not text:
            return  # 전송할 내용 없음

        if not self.selected_model:
            # 보낼 모델이 없으면 먼저 연결 여부 안내
            self.status_indicator.setText("● 연결 필요")
            self.status_indicator.setStyleSheet("color: #f38ba8;")
            self.status_detail.setText("서버 연결을 확인하고 활성화된 모델이 있는지 점검해주세요.")
            return

        # UI에 전송한 메시지 즉시 반영
        self.chat_history.append({
            "role": "user",
            "content": text
        })
        self.update_chat_display()

        # 전송 후 입력칸 초기화
        self.input_edit.clear()

        # 비동기 전송 처리
        host = self.ip_input.text().strip()
        port = self.port_input.text().strip()
        self.send_btn.setEnabled(False)
        self.send_btn.setText("응답 중...")

        self.chat_worker = ChatWorker(host, port, self.selected_model, self.chat_history)
        self.chat_worker.success.connect(self.on_chat_success)
        self.chat_worker.error.connect(self.on_chat_error)
        self.chat_worker.start()

    def on_chat_success(self, reply):
        self.send_btn.setEnabled(True)
        self.send_btn.setText("전송")
        
        # 어시스턴트 메시지 추가
        self.chat_history.append({
            "role": "assistant",
            "content": reply
        })
        self.update_chat_display()

    def on_chat_error(self, err_msg):
        self.send_btn.setEnabled(True)
        self.send_btn.setText("전송")
        
        # 시스템 에러 로그 출력
        self.chat_history.append({
            "role": "assistant",
            "content": f"<span style='color: #f38ba8;'><b>[에러 발생]</b> {err_msg}</span>"
        })
        self.update_chat_display()

    def update_chat_display(self):
        """chat_history를 파싱하여 말풍선 형식의 HTML로 변환 후 렌더링"""
        html = """
        <style>
            .message-container { margin: 15px 0; display: flex; flex-direction: column; }
            .user-msg { background-color: #89b4fa; color: #11111b; border-radius: 12px 12px 0 12px; padding: 10px; margin-left: 20%; align-self: flex-end; }
            .assistant-msg { background-color: #313244; color: #cdd6f4; border-radius: 12px 12px 12px 0; padding: 10px; margin-right: 20%; align-self: flex-start; }
            .meta-info { font-size: 11px; color: #a6adc8; margin-bottom: 4px; }
        </style>
        """

        for msg in self.chat_history:
            role_name = "User" if msg["role"] == "user" else "Assistant"
            align = "right" if msg["role"] == "user" else "left"
            msg_class = "user-msg" if msg["role"] == "user" else "assistant-msg"
            
            html += f"<div style='text-align: {align};' class='message-container'>"
            html += f"<div class='meta-info'>{role_name}</div>"
            html += f"<div style='display: inline-block; text-align: left;' class='{msg_class}'>"
            
            # 텍스트 내용 처리 (줄바꿈 반영)
            formatted_content = msg["content"].replace("\n", "<br>")
            html += f"<div>{formatted_content}</div>"
            
            html += "</div></div>"

        self.chat_display.setHtml(html)
        
        # 스크롤바를 맨 아래로 이동
        scrollbar = self.chat_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


def main():
    app = QApplication(sys.argv)
    window = ChatbotApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
