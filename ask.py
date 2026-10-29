"""터미널 RAG: python ask.py "펌프다운이 평소보다 오래 걸려요"
검색+생성은 rag/pipeline.answer — 워커(worker/main.py)와 같은 함수."""
import getpass
import sys
import uuid

from events import InboundEvent
from rag.pipeline import answer


def main(q: str) -> None:
    inbound = InboundEvent(event_id=uuid.uuid4().hex, source="cli", channel="terminal", user=getpass.getuser(), text=q)
    out = answer(inbound)
    print(f"Q: {q}\n")
    print(out.text)
    if out.citations:
        print("\n근거:", ", ".join(f"{c.ticket_id}({c.score:.2f})" for c in out.citations))
    print(f"[{out.status}]")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "펌프다운이 평소보다 오래 걸리는데 예전에 비슷한 일 있었어?")
