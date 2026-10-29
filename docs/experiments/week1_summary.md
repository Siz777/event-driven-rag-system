# 1주차 실험 요약 (experiments.jsonl 전체 행)

계획 단위: 1주차 / 실제 기간: 2026-10-26 ~ 10-29

k=5, 임베딩 bge-m3(1024), 로컬 Qdrant, 티켓 318건(tickets.clean.jsonl). positive = negative 제외 질의 수.

| # | 실험 | 시각 | 설정 | positive n | R@5 | MRR | 난이도별 R@5 | 비고 |
|---|---|---|---|---|---|---|---|---|
| 1 | 기준선 (첫 실행, R@1/@3 없음) | 2026-10-29T10:30 | vector · doc_text=full · manufacturing_baai_bge_m3_v1 | 109 | **0.853** | 0.613 | exact 1.0 · paraphrase 0.6 · multi_hop 0.967 · human 0.842 | eval 140행. exact 1.0 · paraphrase(=hard) 0.60 · multi_hop 0.967 · human 0.842. 커밋 177c16c |
| 2 | 기준선 벡터 only | 2026-10-29T10:34 | vector · doc_text=full · manufacturing_baai_bge_m3_v1 | 109 | **0.853** | 0.613 | exact 1.0 · paraphrase 0.6 · multi_hop 0.967 · human 0.842 | 위와 동일 설정 재실행(recall@1/@3 추가). R@1 0.716, R@3 0.807. negative top1 0.547/0.625 |
| 3 | 실험 1 · symptom_only 색인 | 2026-10-29T10:35 | vector · doc_text=symptom_only · manufacturing_baai_bge_m3_symptom_only_v1 | 109 | **0.872** | 0.677 | exact 1.0 · paraphrase 0.667 · multi_hop 1.0 · human 0.789 | 조치 기록·설비 라인 제외, 증상만 색인. LLM 질의 0.856→0.889, 사람 초안 0.842→0.789. 기각 |
| 4 | 실험 2 · paraphrase 분리 | 2026-10-29T10:47 | vector · doc_text=full · manufacturing_baai_bge_m3_v1 | 139 | **0.885** | 0.636 | exact 1.0 · paraphrase 1.0 · paraphrase_hard 0.6 · multi_hop 0.967 · human 0.842 | paraphrase_hard(구 paraphrase) 0.60 / 현실적 paraphrase 1.0. eval 170행. 커밋 ce6d1b0. README 반영 |

## README 결과표와 대조

- README 벡터 only = `overall 0.885 / paraphrase 1.0 / paraphrase_hard 0.60` = 4행. 주석에 분리 전 기준선(2행, 0.853, 177c16c) 명시.
- 1행과 2행은 같은 설정(2행은 R@1/@3 집계를 추가한 재실행). 숫자 동일.

## 1주차 결론

- 벡터 only 기준선 R@5 0.885 (현실적 paraphrase 1.0, 수수께끼 paraphrase_hard 0.60, 사람 초안 0.842).
- 실패 16건: 어휘 불일치 11 · 설비 혼동 2 · 코드 1 · 집계형 1 · confusable 1 (docs/log/2026-10-29.md).
- 기각: symptom_only 색인. 보류: negative 점수 임계값(LLM 판정으로 대체 예정).
- 미완: 카탈로그 전공 검토(C04·C14만), 사람이 직접 쓴 질의(현재 19개 전부 assistant_draft).
