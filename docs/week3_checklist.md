# 3주차 체크리스트 — "검색을 섞고, 모른다고 말하고, 숫자로 증명한다"

계획 단위: 3주차 / 시작 예정: 2026-11-02

Step은 작업 단계이며 달력 날짜와 무관. 진행 날짜는 docs/log/ 참조.
2주차 산출물(이벤트 경로 전체, 워커·어댑터·멱등 가드)은 그대로 쓴다. 3주차는 워커 안의 `rag/pipeline.answer`만 바꾼다 — 계약·큐·어댑터는 워밍업 1건 외 손대지 않는다.

목표: 금요일에 **코드·약어 질의가 잡히고(BM25), 이력이 없으면 "없다"고 답하며(negative grade), 그 차이가 평가표 한 장에 있다.** README에 데모 GIF.

## Step 0 — 워밍업: 질문 ts → OutboundEvent
- [x] `InboundEvent.ts`를 `OutboundEvent.asked_at`(tz-aware, 선택)으로 복사. `inbound.reply()`가 채움. 계약 version은 그대로(필드 추가 + 기본 None은 호환)
- [x] `adapters/slack_reply.py`의 latency_from_question을 asked_at 기준으로(없으면 reply_to 폴백). 스레드 안 질문도 정확
- [x] 테스트: contracts 왕복 1, reply 지연 로그 1. 2주차 미실험 2개 중 drain 타임아웃 초과만(requeue 통과, 300s 안 재시작은 멱등 가드가 버림 → drain 타임아웃 시 키 release로 수정, 통합 테스트 test_k — 일지 11-02). retry 3단 155s 전 구간은 미실험
- [x] 산출물: 커밋 1개, 일지 1줄

## Step 1 — Kiwi + BM25
- [x] `rag/tokenize.py` + `rag/bm25_index.py`(lexical.py 대신 둘로 나눔): Kiwi 토크나이저(명사·영문·숫자·코드 `E-1301`/`INC-…` 보존) + bm25s 색인. 사용자 사전: 설비명·부품명·알람 코드(카탈로그에서 추출)
- [x] `python -m rag.bm25_index` (별도 명령). 색인 파일은 `domains/<domain>/artifacts/bm25/` 로컬(gitignore)
- [x] 평가: `rag.eval --method bm25`. 확인: 코드 단독 질의 0/5 → 5/5, human 0.842 → 0.947, paraphrase_hard 0.6 → 0.367 (일지 11-02)
- [x] 산출물: BM25 단독 행 1개(experiments.jsonl) + 토크나이저 샘플 10개 기록 (사전 v2: 자동 검출 80개, SN 제거 — docs/experiments/kiwi_dictionary.md)

## Step 2 — 하이브리드 (RRF)
- [x] `rag/retrieve.py`: vector top-20·bm25 top-20 → RRF(k=60) 병합 → top-5. 가중치는 1:1 고정(튜닝 금지 — 평가셋이 170행이라 과적합)
- [x] 평가: `--method hybrid`. 비교표: vector / bm25 / hybrid × 난이도 + 코드 질의 수동 3개 + negative 분포 (일지 11-02). hybrid R@1 0.784·MRR 0.656 최고, paraphrase_hard는 vector 0.6 > hybrid 0.567, 코드 단독은 bm25 5/5 > hybrid 3/5
- [x] 산출물: README 결과 표 3행 채움

## Step 3 — 설비 메타데이터 필터
- [x] `rag/query_parse.py`: 알람 코드(정규화)·설비 ID·설비 타입 키워드 추출(사전 매칭) → 코드면 alarm_code 필터 + BM25 순위만, 타입이면 Qdrant payload filter + BM25 사후 필터 + RRF. 추출 실패·미지 코드면 필터 없음
- [x] 평가: 설비 혼동 2건은 설비명 없는 질의라 원리상 못 잡음. 타입 오탐 0/9, 코드 단독 5/5, R@1 0.791·MRR 0.665 → 켠다. 필터 적용 38/170(22.4%)
- [x] 산출물: 필터 on/off 행 2개(hybrid / hybrid+filter, experiments.jsonl) + filter_hits 집계. negative는 필터로 분리 안 됨(positive도 같이 하락) — Step 4는 LLM 판정

## Step 4 — negative grade 노드 (LangGraph)
- [x] `rag/graph.py`: retrieve → grade → (generate | no_answer). grade = cosine<0.50(보조) + 티켓별 LLM yes/no(gpt-5-mini, 병렬, yes≥2). LangGraph StateGraph 노드 4개 + 생성 후검증(인용 0 → no_answer)
- [x] `pipeline.answer`가 graph를 호출. 계약·워커 변경 없음(워커 warmup에 Kiwi·BM25 로드만 추가)
- [x] 평가: negative no_match 0.968(30/31), positive 오탐 0.014(2/139), 정답 유실 0, 지연 +4.7s — scripts/eval_graph.py, 일지 11-02
- [x] 산출물: 혼동 행렬(negative 30/1, positive 137/2) + 판정 프롬프트 GRADE_SYSTEM(gpt-5 계열: 역할·규칙·출력만, reasoning 없음)

## Step 5 — 평가 재실행
- [x] 전 설정 재실행: vector / bm25 / hybrid / hybrid+filter / hybrid+filter+grade — 전부 11-02 같은 색인·평가셋·k=5 (재실행 불필요, 행 그대로)
- [x] 비교표: paraphrase_hard 0.6 → 0.567(안 바뀜, 임베딩 한계로 확정), human R@1 0.632 → 0.789
- [x] 산출물: docs/experiments/week3_summary.md + README 결과 표 최종(human·코드 질의 먼저, R@1·MRR 주인공)

## Step 6 — GitHub 이슈 교차 검증
- [x] docker/compose kind/bug 2600건 → domains/github_issues/ (catalog 없음) → 색인 → 질의 = 중복 이슈 56쌍(시간 필터 뒤 46)
- [x] R@5 방향 동일(hybrid 0.696 ≥ vector 0.63 > bm25 0.543), R@1은 반대(vector 최고). CI 겹침(n=46). README 열 채움
- [x] 산출물: scripts/collect_github_issues.py + scripts/eval_github.py + 행 3개(domains/github_issues/eval/experiments.jsonl)

## Step 7 — 노이즈 주입 실험
- [x] (질의 쪽에 주입 — 티켓 재색인 대신 79행 질의에 오타 1~2곳/10%/30%·약어·둘 다) vector / hybrid+filter / graph + 사전·코드 off. 벡터가 먼저(both30 0.924), 하이브리드 R@5 0.975 유지
- [x] 산출물: noise_results.jsonl 행 35개(7세트 × 5설정) + 한 줄 결론(일지 11-02 Step 7)

## Step 8 — README 최종 · 데모 GIF
- [x] 데모 GIF: Cmd+Shift+5 선택 영역(Slack 창 스레드만), 질문은 미리 입력해 두고 Enter부터 답변까지 20초 내. `ffmpeg -i demo.mov -vf "fps=10,scale=800:-1" -loop 0 docs/demo.gif`
- [x] README: 아키텍처(parse → RRF → grade 분기), 결과 표 숫자 전부(experiments.jsonl 대조 완료), 설계 결정 4개, 기각된 실험·신뢰성 표, 한계 갱신
- [x] 산출물: GIF(docs/demo.gif 26초 · 질문→답변 19.4초) + README ✓ + 3주차 요약 ✓

## Step 9 — 설비 되묻기 (11-03 추가, 사용자 질의 124건 분석에서 도출)
- [x] rag/clarify.py(조건·문구·답 해석) + graph clarify 노드(grade 뒤) + status=clarify + worker ClarifyStore(스레드당 1회, Redis 300s) + Slack 포맷
- [x] 평가(scripts/eval_clarify.py): 발동 44/124, 오라클 답 후 R@5 0.75 → 0.831(상한 0.871), negative 발동 0/39, 미발동 실패 17건 그룹 R@5 0.529
- [x] 슬랙 수동 왕복 1회(되묻기 → 답 → 재검색): 되묻기 6.9s, 답 25.6s(slack 로그) — docs/demo_clarify.gif(51초, README 두 번째 GIF)
- [x] README 결과 표 "되묻기 후" 줄 + 한계 절, 일지

## Step 10 준비 — cross-encoder 판정 (11-04, 실험은 다음)
- [x] scripts/build_pairs.py: 283행 × 상위 20 → train 4,233 / val 1,288 쌍(질의 ID 분리, 사람 10건 val 전용, confusable 열)
- [x] GRADE_MODE=api|per_doc|batch|cross_encoder 분기(cross_encoder는 NotImplemented 자리)
- [x] 질의당 토큰·비용·지연 계측(rag.llm.track → 워커 로그·eval_graph) + LLM_TOKEN_BUDGET 가드
- [x] README "데이터 경계" 절
- [ ] 실험: 제로샷 → val 비교 → 학습(일지 11-04 설계 초안)

## 주차 마무리
- [x] docs/experiments/week3_summary.md, 일지 결정 칸(RRF 가중치 고정 이유, grade를 LLM으로 한 이유, 필터 on/off)
- [x] 한 줄 후보: 하이브리드가 paraphrase_hard를 얼마나 살렸는지 한 숫자

## 하지 않는 것
- 멀티턴, 리랭커(cross-encoder), 파인튜닝, 평가셋 확장(170행 고정), 가중치 튜닝, Kafka
