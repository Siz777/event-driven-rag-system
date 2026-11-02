# 3주차 체크리스트 — "검색을 섞고, 모른다고 말하고, 숫자로 증명한다"

계획 단위: 3주차 / 시작 예정: 2026-11-02

Step은 작업 단계이며 달력 날짜와 무관. 진행 날짜는 docs/log/ 참조.
2주차 산출물(이벤트 경로 전체, 워커·어댑터·멱등 가드)은 그대로 쓴다. 3주차는 워커 안의 `rag/pipeline.answer`만 바꾼다 — 계약·큐·어댑터는 워밍업 1건 외 손대지 않는다.

목표: 금요일에 **코드·약어 질의가 잡히고(BM25), 이력이 없으면 "없다"고 답하며(negative grade), 그 차이가 평가표 한 장에 있다.** README에 데모 GIF.

## Step 0 — 워밍업: 질문 ts → OutboundEvent
- [x] `InboundEvent.ts`를 `OutboundEvent.asked_at`(tz-aware, 선택)으로 복사. `inbound.reply()`가 채움. 계약 version은 그대로(필드 추가 + 기본 None은 호환)
- [x] `adapters/slack_reply.py`의 latency_from_question을 asked_at 기준으로(없으면 reply_to 폴백). 스레드 안 질문도 정확
- [x] 테스트: contracts 왕복 1, reply 지연 로그 1. 2주차 미실험 2개 중 drain 타임아웃 초과만(requeue 통과, 300s 안 재시작은 멱등 가드가 버림 — 일지 11-02). retry 3단 155s 전 구간은 미실험
- [x] 산출물: 커밋 1개, 일지 1줄

## Step 1 — Kiwi + BM25
- [x] `rag/tokenize.py` + `rag/bm25_index.py`(lexical.py 대신 둘로 나눔): Kiwi 토크나이저(명사·영문·숫자·코드 `E-1301`/`INC-…` 보존) + bm25s 색인. 사용자 사전: 설비명·부품명·알람 코드(카탈로그에서 추출)
- [x] `python -m rag.bm25_index` (별도 명령). 색인 파일은 `domains/<domain>/artifacts/bm25/` 로컬(gitignore)
- [x] 평가: `rag.eval --method bm25`. 확인: 코드 단독 질의 0/5 → 5/5, human 0.842 → 0.947, paraphrase_hard 0.6 → 0.367 (일지 11-02)
- [x] 산출물: BM25 단독 행 1개(experiments.jsonl) + 토크나이저 샘플 10개 기록 (사전 보강 판단 대기: 리크·미도달·SN)

## Step 2 — 하이브리드 (RRF)
- [ ] `rag/retrieve.py`: vector top-k·bm25 top-k → RRF(k=60) 병합 → top-5. 가중치는 1:1 고정(튜닝 금지 — 평가셋이 170행이라 과적합)
- [ ] 평가: `--method hybrid`. 비교표: vector / bm25 / hybrid × (exact·paraphrase·paraphrase_hard·multi_hop·human·code)
- [ ] 산출물: README 결과 표 3행 채움

## Step 3 — 설비 메타데이터 필터
- [ ] 질문에서 설비명·설비 ID 추출(사전 매칭, LLM 아님) → Qdrant payload filter + BM25 사후 필터. 추출 실패면 필터 없음
- [ ] 평가: 1주차 실패 분석의 "설비 혼동 2건"이 잡히는지. 필터 오탐(엉뚱한 설비로 좁힘)이 더 많으면 끈다
- [ ] 산출물: 필터 on/off 행 2개

## Step 4 — negative grade 노드 (LangGraph)
- [ ] `rag/graph.py`: retrieve → grade → (answer | no_match). grade = 점수 임계값(보조) + LLM 판정("검색 결과가 질문과 같은 설비·증상인가", yes/no, 소형 모델). LangGraph StateGraph로 노드 3개
- [ ] `pipeline.answer`가 graph를 호출. 계약·워커 변경 없음
- [ ] 평가: negative 31행에서 no_match 비율(목표 0.8 이상) vs positive에서 오탐 no_match(목표 0.05 이하). 1주차 수치: 임계값만으로는 못 가름(negative 0.547/0.625 vs positive 0.57~0.73)
- [ ] 산출물: 혼동 행렬 1개 + LLM 판정 프롬프트(모델 등급별 원칙 적용)

## Step 5 — 평가 재실행
- [ ] 전 설정 재실행: vector / bm25 / hybrid / hybrid+filter / hybrid+filter+grade. 같은 평가셋(170행), 같은 k
- [ ] 비교표: paraphrase_hard·human 열을 따로 — 1주차 결론("용어를 안 쓰면 무너진다")이 하이브리드로 얼마나 바뀌는지
- [ ] 산출물: docs/experiments/week3_summary.md + README 결과 표 최종

## Step 6 — GitHub 이슈 교차 검증
- [ ] 공개 저장소 이슈 200~300건(한 프로젝트, 라벨 있는 것) → 같은 스키마로 변환 → 색인 → 질의 30개(제목 바꿔 쓰기)
- [ ] 합성 티켓 vs 실제 이슈에서 vector/hybrid 차이가 같은 방향인지 확인. README "Recall@5 (GitHub 이슈)" 열 채움
- [ ] 산출물: 변환 스크립트 + 행 2개

## Step 7 — 노이즈 주입 실험
- [ ] 티켓 본문에 오타·줄임말·구어체 주입(비율 10/30%) → 재색인 → 평가. 벡터 vs BM25 중 어느 쪽이 먼저 무너지는지
- [ ] 산출물: 행 4개 + 한 줄 결론

## Step 8 — README 최종 · 데모 GIF
- [ ] 데모 GIF: Cmd+Shift+5 선택 영역(Slack 창 스레드만), 질문은 미리 입력해 두고 Enter부터 답변까지 20초 내. `ffmpeg -i demo.mov -vf "fps=10,scale=800:-1" -loop 0 docs/demo.gif`
- [ ] README: 아키텍처 그림 실제와 일치(LangGraph·BM25 들어감), 결과 표 전부 숫자, 설계 결정 3주차 3개 추가, 한계 갱신
- [ ] 산출물: GIF + README + 3주차 요약

## 주차 마무리
- [ ] docs/experiments/week3_summary.md, 일지 결정 칸(RRF 가중치 고정 이유, grade를 LLM으로 한 이유, 필터 on/off)
- [ ] 한 줄 후보: 하이브리드가 paraphrase_hard를 얼마나 살렸는지 한 숫자

## 하지 않는 것
- 멀티턴, 리랭커(cross-encoder), 파인튜닝, 평가셋 확장(170행 고정), 가중치 튜닝, Kafka
