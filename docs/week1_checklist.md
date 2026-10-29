# 1주차 체크리스트 — "터미널에서 도는 RAG + 평가 도구"

Step은 작업 단계이며 달력 날짜와 무관. 진행 날짜는 docs/log/ 참조.

목표: 금요일에 **벡터 only 기준선 Recall@5 숫자 하나**가 나온다.

## Step 1 — 환경 + 카탈로그
- [x] `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- [x] `.env.example` → `.env` 복사, LLM 키 입력
- [ ] `domains/manufacturing/catalog.yaml` 40개 원인 **읽고 손보기** (전공 지식으로 어색한 표현 교정 — 이게 본인 기여) — 미완: C04·C14 근거만 docs/catalog_review.md, 본문 수정 전
- [x] `python -c "from rag import embed; print(embed.dim())"` 로 임베딩 모델 다운로드 확인

## Step 2 — 생성 + 검증
- [x] `python -m scripts.generate_tickets --per-cause 8`  (40×8 = 320건 목표)
- [x] 생성된 티켓 20개 **직접 읽기** — 원인 단어가 symptom에 새어 들어갔는지, 페르소나가 구분되는지
- [x] `python -m scripts.validate_tickets` → `validation_report.json` 확인, 부족 원인 보충
- [x] 일지: 생성 프롬프트에서 뭘 고쳤는지 기록

## Step 3 — 인덱싱 + 첫 질문
- [x] `python -m rag.index`
- [x] `python ask.py "펌프다운이 평소보다 오래 걸리는데 예전에 비슷한 일 있었어?"`
- [x] 답변에 [INC-xxxx] 인용이 붙는지, 엉뚱한 설비 티켓을 끌어오지 않는지 확인

## Step 4 — 평가셋 + 기준선
- [x] `python -m scripts.build_evalset --per-difficulty 30`
- [ ] `eval.jsonl`에 **손으로 쓴 질의 20개** 추가 (LLM 편향 완화) — 미완: 20개 전부 AI 초안(source=assistant_draft), '#human' 표시 0
- [x] `python -m rag.eval --method vector` → 난이도별 Recall@5, MRR
- [x] `failures_vector.md` 10개 읽고 패턴 적기 (알람 코드? 약어? 설비 혼동?)

## Step 5 — 진단 + 정리
- [x] 숫자가 0.5 이하 / 0.95 이상이면 Q&A의 체크리스트대로 의심 (언어·거리척도·누수·정답 과관대)
- [x] `experiments.jsonl` 첫 행 = README 결과표의 첫 줄
- [ ] 일지 5일치 완성, "한 줄" 5개 중 하나는 2분 발표 문장 후보 — 부분: 일지 3일치(10-26·27·29), 한 줄 4개, 발표 후보 3개 선정

## 하지 않는 것 (2·3주차)
- Slack, FastAPI, RabbitMQ, LangGraph, BM25/하이브리드, negative 분기
