"""가장 단순한 MCP 서버 예제입니다.

이 서버는 add라는 덧셈 도구 하나만 외부에 공개합니다.
MCP 클라이언트는 이 서버에 연결해 도구 목록을 조회하고 add를 실행할 수 있습니다.
"""

from mcp.server.fastmcp import FastMCP


# MCP 서버 객체를 만듭니다.
# 이름은 사람이 서버를 구분하기 위한 이름입니다.
mcp = FastMCP("Simple Calculator")


@mcp.tool()
def add(a: int, b: int) -> int:
    """두 정수를 더합니다.

    Args:
        a: 첫 번째 정수
        b: 두 번째 정수

    Returns:
        두 정수의 합
    """
    return a + b


if __name__ == "__main__":
    # stdio 방식은 MCP 클라이언트가 이 파일을 자식 프로세스로 실행하고,
    # 표준 입력과 표준 출력을 통해 MCP 메시지를 주고받는 방식입니다.
    #
    # 주의: stdio MCP 서버에서는 일반 print()를 사용하지 않는 것이 좋습니다.
    # 표준 출력은 MCP 통신에 사용되기 때문입니다.
    mcp.run(transport="stdio")
