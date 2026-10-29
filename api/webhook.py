"""POST /events — Slack Events API 웹훅.

흐름: 원문 바디 읽기 → 서명 검증(401) → JSON 파싱(400)
      → url_verification이면 challenge 그대로 200
      → event_callback → InboundEvent 변환(실패 시 200 ignored + 로그) → publish(503) → 202
      → 처리 대상 아니면 200 ignored (Slack은 2xx가 아니면 재전송하므로 무시도 2xx로 답한다)

publish는 주입(create_app(publish=...)). 기본(None)은 RabbitMQ Publisher(RABBITMQ_URL) — 첫 요청 때 연결, 종료 시 닫음.
실행: uvicorn api.webhook:app --port 8000
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api import slack
from events import InboundEvent
from mq.publisher import Publisher

log = logging.getLogger("api.webhook")

PublishFn = Callable[[InboundEvent], Awaitable[None]]


def create_app(*, signing_secret: str | None = None, publish: PublishFn | None = None,
               rabbitmq_url: str | None = None) -> FastAPI:
    publisher = Publisher(rabbitmq_url) if publish is None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if publisher is not None:
            await publisher.close()

    app = FastAPI(title="event-driven-rag webhook", lifespan=lifespan)
    app.state.signing_secret = signing_secret
    app.state.publish = publish if publish is not None else publisher.publish

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/events")
    async def events(
        request: Request,
        x_slack_signature: str | None = Header(default=None),
        x_slack_request_timestamp: str | None = Header(default=None),
        x_slack_retry_num: str | None = Header(default=None),
    ):
        secret = request.app.state.signing_secret
        if not secret:
            return JSONResponse({"error": "SLACK_SIGNING_SECRET not configured"}, status_code=503)
        body = await request.body()
        why = slack.verify(secret, x_slack_request_timestamp, x_slack_signature, body)
        if why:
            log.warning("rejected: %s", why)
            return JSONResponse({"error": why}, status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        kind = payload.get("type")
        if kind == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        if kind != "event_callback":
            return {"status": "ignored", "reason": f"type={kind}"}

        try:
            ev = slack.to_inbound(payload)
        except ValidationError as e:  # 질문이 못 되는 이벤트(멘션만 등). 422면 Slack이 재전송하므로 200으로 무시
            log.warning("ignored invalid event %s: %s", payload.get("event_id"), e.errors(include_url=False))
            return {"status": "ignored", "reason": "invalid event"}
        if ev is None:
            return {"status": "ignored", "reason": "not a user question"}

        if x_slack_retry_num:
            log.info("slack retry #%s for %s", x_slack_retry_num, ev.event_id)
        try:
            await request.app.state.publish(ev)
        except Exception as e:  # 큐 장애를 삼키지 않는다 — Slack이 재전송하게 503
            log.error("publish failed for %s: %s", ev.event_id, e)
            return JSONResponse({"error": "queue unavailable"}, status_code=503)
        return JSONResponse({"status": "accepted", "event_id": ev.event_id}, status_code=202)

    return app


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app(signing_secret=os.getenv("SLACK_SIGNING_SECRET"), rabbitmq_url=os.getenv("RABBITMQ_URL"))
