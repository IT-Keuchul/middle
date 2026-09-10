Local LLM Manager

Windows 10/11용 GUI 프로그램입니다.

기능
1. LM Studio/llmster 설치 및 업데이트
2. Ollama 설치 및 업데이트
3. RAM, NVIDIA GPU VRAM, 디스크 자동 확인
4. Hugging Face GGUF text-generation 모델 검색
5. PC 사양에 맞는 모델 추천
6. Q4_K_M 우선 GGUF 다운로드
7. LM Studio(lms import) / Ollama(ollama create) 등록
8. 설치된 모델 조회
9. 선택한 모델과 스트리밍 채팅

처음 실행
1. Python 3 설치
2. setup.bat 실행
3. run.bat 실행

설치 명령
LM Studio/llmster:
irm https://lmstudio.ai/install.ps1 | iex

Ollama:
irm https://ollama.com/install.ps1 | iex

주의
LM Studio의 위 PowerShell 설치 명령은 공식 문서상 Desktop GUI가 아니라
llmster + lms CLI를 설치하는 방식입니다.
이 프로그램이 자체 GUI 역할을 합니다.

기본 API
LM Studio: http://127.0.0.1:1234
Ollama:    http://127.0.0.1:11434

모델 권장 크기는 Q4 기준의 근사치입니다.
실제 메모리 사용량은 컨텍스트 길이와 모델 구조에 따라 달라집니다.
