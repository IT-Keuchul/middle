# Ollama + MCP 초간단 강의 예제

## 구성 파일

- `mcp_server.py`: `add(a, b)` 도구 하나를 제공하는 기본 계산기 MCP 서버
- `mcp_server_utils.py`: 현재 시각 조회 및 텍스트 통계 도구를 제공하는 유틸리티 MCP 서버
- `client_ollama.py`: Ollama API에 직접 연결하는 MCP 클라이언트
- `client_openwebui.py`: Open WebUI의 OpenAI 호환 API를 거쳐 연결하는 MCP 클라이언트
- `client_lmstudio.py`: LM Studio의 OpenAI 호환 로컬 API로 연결하는 CLI 클라이언트
- `client_lmstugio_gui.py`: 다중 MCP 서버 선택 및 실시간 도구 로그를 지원하는 LM Studio GUI 챗봇

## MCP를 한 문장으로 설명하면

MCP는 AI 프로그램이 계산기, 파일, 데이터베이스 같은 외부 기능을 일정한 형식으로 발견하고 실행하도록 만드는 표준 연결 규칙입니다.

## 설치

```powershell
uv sync
```

설치 명령을 직접 입력하려면 다음과 같이 할 수 있습니다.

```powershell
uv add "mcp[cli]>=1.27,<2" ollama openai
```

## 모델 준비

도구 호출을 지원하는 모델을 준비합니다.

```powershell
ollama pull qwen3:4b
```

이미 다른 도구 호출 지원 모델이 설치되어 있으면 각 클라이언트의 `MODEL`만 변경합니다.

## 1. Ollama 직접 연결 실행

```powershell
uv run client_ollama.py
```

예시 질문:

```text
27과 15를 더해줘.
```

## 2. Open WebUI 경유 실행

`client_openwebui.py`에서 다음 값을 수정합니다.

```python
OPEN_WEBUI_BASE_URL = "http://OpenWebUI주소:포트/api"
OPEN_WEBUI_API_KEY = "sk-발급받은_API_KEY"
MODEL = "Open_WebUI에_표시되는_모델_ID"
```

실행:

```powershell
uv run client_openwebui.py
```

## 3. LM Studio 로컬 연결 실행

1. LM Studio에서 Function/Tool Calling을 지원하는 모델(예: Qwen 2.5, Llama 3.1 등)을 로드합니다.
2. 좌측 **Developer** (또는 **Local Server**) 탭으로 이동하여 **Start Server**를 클릭합니다. (기본 포트: `1234`)
3. 스크립트를 실행합니다 (`MODEL`을 비워두면 현재 로드된 모델을 자동으로 감지합니다):

```powershell
uv run client_lmstudio.py
```

## 4. LM Studio 다중 MCP GUI 챗봇 실행

1. LM Studio에서 로컬 서버(기본 포트: `1234`)를 시작합니다.
2. GUI 챗봇 프로그램을 실행합니다:

```powershell
uv run client_lmstugio_gui.py
```
*(또는 `uv run client_lmstudio_gui.py`)*

### GUI 주요 기능
- **다중 MCP 선택**: 좌측 사이드바에서 원하는 MCP 서버(`mcp_server.py`, `mcp_server_utils.py` 등)를 체크박스로 자유롭게 활성화/비활성화할 수 있습니다.
- **새 MCP 파일 추가**: `+ 새 MCP 파일 추가` 버튼을 눌러 직접 작성한 다른 MCP 서버(`.py`)를 즉시 등록할 수 있습니다.
- **도구 목록 실시간 확인**: 선택된 MCP 서버들에서 제공하는 도구 목록(함수 이름, 설명)이 실시간으로 표시됩니다.
- **실시간 도구 실행 로그**: 대화창에서 모델이 어떤 도구를 어떤 인자로 호출했고 MCP 서버가 어떤 값을 반환했는지 색상별 로그로 확인할 수 있습니다.

## 수업에서 관찰할 출력

```text
MCP 서버에서 발견한 도구: ['add']
모델이 선택한 도구: add
도구에 전달한 값: {'a': 27, 'b': 15}
MCP 도구 실행 결과: 42
최종 답변: 27과 15를 더하면 42입니다.
```

## 가장 중요한 개념

Ollama나 Open WebUI가 `add()` 함수를 직접 실행하는 것이 아닙니다.

1. 모델은 어떤 도구를 어떤 값으로 호출할지만 결정합니다.
2. Python MCP 클라이언트가 실제 MCP 서버에 실행을 요청합니다.
3. MCP 서버가 함수를 실행합니다.
4. 결과를 모델에 다시 전달합니다.
