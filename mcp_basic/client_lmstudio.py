"""MCP 서버와 LM Studio를 연결하는 예제입니다.

흐름
1. Python 프로그램이 MCP 서버(mcp_server.py)를 실행하고 연결합니다.
2. MCP 서버에서 add 도구(함수) 설명을 가져옵니다.
3. 그 도구 설명을 OpenAI 호환 형식으로 변환하여 LM Studio 로컬 서버에 전달합니다.
4. 모델이 add 도구 호출을 결정하면 MCP 서버의 add를 실행합니다.
5. 실행 결과를 다시 모델에 전달해 최종 자연어 답변을 만듭니다.

사전 준비
1. LM Studio를 실행합니다.
2. 도구 호출(Tool calling / Function calling)을 지원하는 모델을 로드합니다.
   (예: Qwen 2.5, Llama 3.1 / 3.2, Mistral 계열 등)
3. LM Studio 좌측의 "Developer" (또는 "Local Server") 탭에서 서버를 시작(Start Server)합니다.
   - 기본 주소: http://localhost:1234
   - OpenAI 호환 API 엔드포인트: http://localhost:1234/v1
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI


# LM Studio 설정
# LM Studio 로컬 서버의 기본 포트는 1234이며, OpenAI 호환 경로는 /v1 입니다.
LM_STUDIO_BASE_URL = "http://localhost:1234/v1"
LM_STUDIO_API_KEY = "lm-studio"  # LM Studio는 API 키 검증이 필요 없으나 형식상 지정합니다.

# 특정 모델을 지정하고 싶으면 모델 식별자를 입력하세요.
# 비워둘 경우("") LM Studio에 현재 로드된 모델을 자동으로 감지합니다.
MODEL = ""


def mcp_tools_to_openai_tools(mcp_tools: list) -> list[dict]:
    """MCP 도구 설명을 OpenAI / LM Studio 호환 tool 형식으로 변환합니다."""
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
    """MCP 도구 실행 결과에서 텍스트만 추출합니다."""
    texts: list[str] = []

    for item in result.content:
        if isinstance(item, types.TextContent):
            texts.append(item.text)

    return "\n".join(texts) if texts else str(result)


async def resolve_model_name(client: AsyncOpenAI, target_model: str) -> str:
    """사용할 모델 이름을 확인합니다. 비어있으면 로드된 모델을 자동 감지합니다."""
    if target_model.strip():
        return target_model.strip()

    try:
        models_response = await client.models.list()
        if models_response.data:
            detected = models_response.data[0].id
            print(f"LM Studio에서 로드된 모델을 감지했습니다: {detected}")
            return detected
    except Exception as e:
        print(f"[주의] LM Studio 모델 자동 감지 실패 (서버가 켜져 있는지 확인하세요): {e}")

    # 기본 대체 모델명
    return "local-model"


async def main() -> None:
    # 현재 파일과 같은 폴더에 있는 MCP 서버 파일(mcp_server.py)을 찾습니다.
    server_file = Path(__file__).with_name("mcp_server.py")

    # MCP 클라이언트가 mcp_server.py를 별도 프로세스로 실행하도록 설정합니다.
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_file)],
    )

    # LM Studio의 OpenAI 호환 로컬 API 클라이언트를 생성합니다.
    lm_client = AsyncOpenAI(
        base_url=LM_STUDIO_BASE_URL,
        api_key=LM_STUDIO_API_KEY,
    )

    # 실행할 모델 확인
    model_name = await resolve_model_name(lm_client, MODEL)

    # MCP 서버 실행 및 연결
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # MCP 초기화 핸드셰이크
            await session.initialize()

            # MCP 서버가 제공하는 도구 목록 가져오기
            tools_response = await session.list_tools()
            mcp_tools = tools_response.tools

            print("MCP 서버에서 발견한 도구:", [tool.name for tool in mcp_tools])

            # MCP 도구 형식을 OpenAI / LM Studio 호환 형식으로 변환
            llm_tools = mcp_tools_to_openai_tools(mcp_tools)

            question = input("질문을 입력하세요: ")

            messages = [
                {
                    "role": "system",
                    "content": "숫자 덧셈 질문에는 반드시 제공된 add 도구를 사용하세요.",
                },
                {"role": "user", "content": question},
            ]

            print(f"\nLM Studio ({model_name}) 모델에 질문 전달 중...")

            try:
                # 첫 번째 요청: 모델이 도구를 호출할지 판단합니다.
                first_response = await lm_client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    tools=llm_tools,
                )
            except Exception as e:
                print(f"\n[오류] LM Studio 서버와 통신할 수 없습니다: {e}")
                print("LM Studio에서 'Local Server'가 시작(Start Server)되어 있는지 확인해주세요.")
                return

            assistant_message = first_response.choices[0].message

            # 어시스턴트 메시지를 대화 기록에 추가
            messages.append(assistant_message.model_dump(exclude_none=True))

            tool_calls = assistant_message.tool_calls or []

            # 모델이 도구를 호출하지 않았다면 바로 답변 출력
            if not tool_calls:
                print("\n최종 답변:", assistant_message.content)
                return

            # 모델이 요청한 MCP 도구들을 순서대로 실행
            for tool_call in tool_calls:
                tool_name = tool_call.function.name

                # 전달된 arguments가 JSON 문자열인 경우 파싱
                raw_args = tool_call.function.arguments
                if isinstance(raw_args, str):
                    try:
                        tool_arguments = json.loads(raw_args)
                    except json.JSONDecodeError:
                        tool_arguments = {}
                else:
                    tool_arguments = raw_args or {}

                print(f"\n모델이 선택한 도구: {tool_name}")
                print(f"도구에 전달한 값: {tool_arguments}")

                # MCP 서버에 도구 실행 요청
                result = await session.call_tool(
                    tool_name,
                    arguments=tool_arguments,
                )
                result_text = tool_result_to_text(result)

                print(f"MCP 도구 실행 결과: {result_text}")

                # 도구 실행 결과를 대화 기록에 추가
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text,
                    }
                )

            # 두 번째 요청: 도구 실행 결과를 반영하여 모델이 최종 자연어 답변 생성
            try:
                final_response = await lm_client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    tools=llm_tools,
                )
                print("\n최종 답변:", final_response.choices[0].message.content)
            except Exception as e:
                print(f"\n[오류] 최종 답변 생성 실패: {e}")


if __name__ == "__main__":
    asyncio.run(main())
