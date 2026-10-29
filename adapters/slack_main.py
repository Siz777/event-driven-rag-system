"""Slack 어댑터 프로세스: Socket Mode 수신 + ai_results 답글 소비를 한 프로세스로.

  python -m adapters.slack_main
필요한 env: SLACK_BOT_TOKEN(xoxb-), SLACK_APP_TOKEN(xapp-), RABBITMQ_URL. 선택: REDIS_URL(progress 편집), SLACK_ALLOWED_CHANNELS.
SIGTERM/SIGINT → Socket 연결 닫기(새 질문 중단) → 답글 소비자 정지 → 종료. 답글은 큐에 남아 재시작 뒤 전송된다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import signal

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from adapters.progress import ProgressStore
from adapters.slack_reply import ReplyConsumer, SlackPoster
from adapters.slack_socket import build_app
from mq.publisher import Publisher

log = logging.getLogger("adapters.slack_main")


async def run() -> None:
    app_token = os.getenv("SLACK_APP_TOKEN")
    if not app_token or not os.getenv("SLACK_BOT_TOKEN"):
        raise SystemExit("SLACK_BOT_TOKEN and SLACK_APP_TOKEN are required")

    publisher = Publisher()
    await publisher.connect()                                   # 브로커 없으면 여기서 실패 → 컨테이너 재시작이 낫다
    progress = ProgressStore()                                  # REDIS_URL 없으면 항상 새 답글
    app = build_app(publisher.publish, progress=progress)
    handler = AsyncSocketModeHandler(app, app_token)
    consumer = ReplyConsumer(SlackPoster(), progress=progress)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: (log.info("signal %s received", s.name), stop.set()))

    consumer_task = asyncio.create_task(consumer.run())
    await handler.connect_async()
    await consumer.ready.wait()
    log.info("socket mode connected, reply consumer ready")
    if os.getenv("SLACK_READY_FILE"):
        pathlib.Path(os.environ["SLACK_READY_FILE"]).touch()

    await stop.wait()
    await handler.close_async()
    consumer.request_stop()
    await consumer_task
    await publisher.close()
    await progress.close()
    log.info("bye")


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
