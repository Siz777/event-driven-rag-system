
## 세션 시작 시 (매번)
`/clear` 또는 새 세션 직후 반드시 읽을 것: CLAUDE.md, 현재 주차 checklist(docs/weekN_checklist.md), 오늘·어제 일지(docs/log/). 읽기 전에 코드를 건드리지 않는다.

## 운영 규칙 — 메모리·리소스 (MacBook Air 16GB)
- 워커는 한 벌만. 통합 테스트(pytest -m integration)는 `docker compose stop worker slack` 후 호스트 워커로 돌리고, 끝나면 `docker compose start worker slack`(slack 컨테이너도 ai_results를 소비해 결과 검증을 가로챈다 — 2026-11-03 test_c). 컨테이너 워커와 호스트 워커가 같은 큐를 동시에 소비하면 테스트가 깨지고(2026-10-29 test_worker 7개 실패) bge-m3가 두 벌 올라가 메모리가 부족해진다.
- Ollama는 필요할 때만. Step 4 이후 심판은 gpt-5-mini이므로 Ollama 모델이 로드돼 있으면(`ollama ps`) 내린다(`ollama stop <model>`). 7B 로드 시 약 5GB.
- Docker Desktop 제한: 디스크 32GB, 메모리 6GB. 워커 이미지는 CPU torch 고정(2.26GB), GPU torch가 섞이면 빌드가 실패하도록 되어 있다 — 그 안전장치를 풀지 말 것.
- 작업 시작 전 `docker compose ps`로 6개 healthy 확인, 작업 끝나면 `docker compose stop`.

## 평가·기록 규칙 (3주차 추가)
- 결과 표는 human 줄과 코드 질의를 먼저, overall은 그 아래. 주인공 지표는 R@1·MRR(R@5는 벡터 only에서 이미 0.885, 하이브리드로 불변).
- paraphrase_hard(어휘를 아예 안 쓰는 질의)는 임베딩 한계로 두고 개선 대상에서 제외. 숫자는 그대로 보고한다.
- negative는 cosine 임계값 단독으로 분리 불가(positive min 0.535 vs negative max 0.613 겹침) — LLM 판정 노드가 필수. 임계값은 안전선으로만.
- 세션 종료 전 일지에 "다음: Step N — 무엇" 한 줄을 남긴다. 다음 세션의 첫 메시지가 그 줄을 이어받는다.
