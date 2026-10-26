"""터미널 RAG: python ask.py "펌프다운이 평소보다 오래 걸려요" """
import sys

from rag import config
from rag.llm import chat
from rag.retrieve import search

SYSTEM = """당신은 반도체 팹 설비 엔지니어의 보조입니다.
아래 [과거 티켓]만 근거로 답하세요. 근거가 되는 티켓은 반드시 [INC-xxxx] 형태로 인용하세요.
과거 티켓이 질문과 관련 없으면 "유사 이력을 찾지 못했습니다"라고 답하고, 가장 가까운 티켓을 참고로 언급하세요.
답변은 (1) 유사 사례 요약 (2) 과거 조치 내용 (3) 권장 확인 순서 로 간결하게."""


def answer(query: str, method: str = "vector") -> str:
    hits = search(query, k=config.TOP_K, method=method)
    ctx = "\n\n".join(
        f"[{h.ticket_id}] (유사도 {h.score:.2f})\n{h.payload['_text']}\n최종 원인: {h.payload['root_cause']}"
        for h in hits
    )
    user = f"[질문]\n{query}\n\n[과거 티켓]\n{ctx}"
    return chat(user, SYSTEM)


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "펌프다운이 평소보다 오래 걸리는데 예전에 비슷한 일 있었어?"
    print(f"Q: {q}\n")
    print(answer(q))
