# 데모 GIF 촬영 절차 (3주차 Step 8)

목표: Slack 스레드에서 질문 → "검색 중…" → 근거 티켓이 붙은 답변까지 20초 안. 파일 `docs/demo.gif`, README 첫 화면에 넣는다.

## 0. 준비 (촬영 전 5분)
```bash
export PATH="$HOME/.docker/bin:$PATH"
docker compose up -d                      # 6개 healthy 확인: docker compose ps
docker compose logs -f slack | grep -E "inbound|updated|posted" &   # 옆 터미널: 지연 로그 확인용
ollama ps                                 # 모델 올라와 있으면 ollama stop <model> (메모리)
```
- Slack 데스크톱 앱, `#incident-test` 채널. 봇이 채널에 있는지(`/invite @incident-bot`).
- 워커가 따뜻한지: 미리 한 번 질문해 두고(첫 질문은 모델 로드로 느릴 수 있음) 그 스레드는 지운다.
- 창 크기: 스레드 패널만 보이게 Slack 창을 좁힌다(800px 폭 정도). 다크/라이트는 README와 맞춘다.

## 1. 질문 미리 입력
입력창에 아래를 타이핑해 두고 Enter만 남긴다(타이핑 장면은 GIF에 넣지 않는다 — 길이 절약).
```
@incident-bot PECVD-03에서 E-1101 알람 뜨고 펌프다운이 평소보다 오래 걸리는데, 전에 비슷한 일 있었어?
```
코드·설비가 든 질문을 쓰는 이유: 메타데이터 라우팅(코드 → BM25) 경로가 타고 근거 티켓이 PECVD로 모인다. 대체 질문(없음 경로): `@incident-bot 공장 WiFi SSID가 바뀌어 접속 안됨` → "유사 이력 없음 + 참고 티켓 + 신규 티켓 안내".

## 2. 녹화
1. `Cmd+Shift+5` → "선택 영역 기록" → 영역을 Slack 스레드 패널에 맞춘다 → 기록.
2. Enter. 즉시 스레드에 "검색 중… 유사 이력을 찾아 답을 정리하고 있습니다." 가 붙는다.
3. 15~25초 뒤 그 메시지가 답변으로 바뀐다(chat.update). `근거 티켓: INC-…` 줄까지 보이면 2초 기다렸다가 메뉴바의 정지 버튼.
4. 바탕화면에 `화면 기록 ….mov`가 생긴다 → `mv ~/Desktop/화면\ 기록*.mov docs/demo.mov`.

실패 시: 답이 30초 넘게 안 오면 `docker compose logs worker --tail 20`으로 `processing`/`done` 확인. 큐가 밀려 있으면 RabbitMQ 관리 UI(http://localhost:15672, guest/guest)에서 ai_tasks purge 후 재촬영.

## 3. 변환
```bash
ffmpeg -i docs/demo.mov -vf "fps=10,scale=800:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer" -loop 0 docs/demo.gif
ls -la docs/demo.gif            # 목표 3MB 이하. 크면 fps=8 또는 scale=640
```
palettegen/paletteuse를 쓰는 이유: 기본 변환은 256색 전역 팔레트라 Slack 글자가 뭉개진다. 20초·800px·10fps면 보통 2~3MB.

## 4. README 반영
`<!-- TODO(3주차): 데모 GIF … -->` 자리를 `![demo](docs/demo.gif)`로 바꾸고, 아래에 한 줄: "질문 → 검색 중… → 근거 티켓 5건 답변, 21초(asked_at 기준 지연 로그)". 로그의 `latency_from_question=…s` 값을 그대로 쓴다.

## 5. 뒷정리
```bash
rm docs/demo.mov                          # mov는 커밋하지 않는다(.gitignore에 *.mov)
docker compose stop
```
