"""테스트 기본 환경. rag.graph는 import 시점에 환경변수를 읽으므로 테스트 모듈이 import되기 전(여기)에 고정한다.

GRAPH_TOOLS=0: 파이프라인 테스트는 chat만 fake로 주입한다 — 도구 호출(tool_chat)까지 기본 on이면 실제 LLM을 부르러 간다
(11-09 기본값을 on으로 바꾼 뒤 전체 실행에서 test_pipeline 8건·test_clarify 1건이 305초 걸리며 실패). 도구 테스트(test_tools)는 tools=True를 명시한다.
"""
import os

os.environ.setdefault("GRAPH_TOOLS", "0")
