"""터미널 RAG: python ask.py "펌프다운이 평소보다 오래 걸려요" """
import sys

from rag import config
from rag.llm import chat
from rag.retrieve import search

SYSTEM = """<role>
반도체 팹 설비 엔지니어의 보조. 과거 장애 티켓을 근거로 현재 증상에 대한 유사 이력과 확인 순서를 알려준다.
</role>

<context>
[과거 티켓]은 벡터 검색 상위 {k}건이며 전체 이력이 아니다. 건수를 말할 때는 "검색된 상위 {k}건 기준"이라고 명시한다.
각 티켓에는 증상·조치 기록·조치 결과·최종 원인이 있다.
</context>

<rules>
- [과거 티켓]만 근거로 답한다.
- 근거 티켓은 반드시 [INC-xxxx] 형태로 인용한다.
- 전체 400자 이내.
- 권장 확인 순서는 최대 4개.
</rules>

<output>
(1) 유사 사례 요약 (2) 과거 조치 내용 (3) 권장 확인 순서 — 세 항목, 간결하게.
</output>

티켓에 없는 내용은 추정하지 말고, 관련 티켓이 없으면 '유사 이력 없음'이라고 답하라."""


def answer(query: str, method: str = "vector") -> str:
    hits = search(query, k=config.TOP_K, method=method)
    ctx = "\n\n".join(
        f"[{h.ticket_id}] (유사도 {h.score:.2f})\n{h.payload['_text']}\n최종 원인: {h.payload['root_cause']}"
        for h in hits
    )
    user = f"[질문]\n{query}\n\n[과거 티켓]\n{ctx}"
    return chat(user, SYSTEM.format(k=len(hits)))


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "펌프다운이 평소보다 오래 걸리는데 예전에 비슷한 일 있었어?"
    print(f"Q: {q}\n")
    print(answer(q))
