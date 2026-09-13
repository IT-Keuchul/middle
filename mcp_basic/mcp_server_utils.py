"""다양한 유틸리티 도구를 제공하는 두 번째 MCP 서버 예제입니다.

GUI에서 다중 MCP 서버 선택 기능을 테스트할 때 유용하게 사용할 수 있습니다.
- get_current_time: 현재 한국 시간(KST) 조회
- calculate_text_stats: 텍스트의 글자 수 및 단어 수 계산
"""

from datetime import datetime
from mcp.server.fastmcp import FastMCP

# 유틸리티 MCP 서버 생성
mcp = FastMCP("Utility Tools")


@mcp.tool()
def get_current_time() -> str:
    """현재 날짜와 시간을 한국 표준시(KST) 형식 문자열로 반환합니다."""
    return datetime.now().strftime("%Y년 %m월 %d일 %H시 %M분 %S초")


@mcp.tool()
def calculate_text_stats(text: str) -> str:
    """입력된 문자열의 총 글자 수(공백 포함/제외)와 단어 수를 분석합니다.

    Args:
        text: 분석할 문자열
    """
    total_len = len(text)
    no_space_len = len(text.replace(" ", "").replace("\n", "").replace("\t", ""))
    words_count = len(text.split())
    return (
        f"분석 결과 - 총 글자 수: {total_len}자, "
        f"공백 제외: {no_space_len}자, 단어 수: {words_count}단어"
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
