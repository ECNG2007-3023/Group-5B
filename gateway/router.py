"""The Gateway's FastAPI routes. Used by main.py (P5B alone) and parent_main.py (P5A + P5B).

    WS  /ws/language-service       JSON-RPC 2.0 messages, one session per connection
    GET /language-service/health   {"status": "ok", "sessions": <number open>}
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket

from gateway.connection import Connection
from gateway.jsonrpc import PARSE_ERROR, error_response
from gateway.session import SessionManager

log = logging.getLogger("p5b.router")

WS_PATH = "/ws/language-service"
HEALTH_PATH = "/language-service/health"


def build_router(manager=None):
    """Routes that use `manager`. If none is given, one is made from the
    environment the first time a client connects."""
    router = APIRouter(tags=["P5B language service"])
    router.manager = manager

    def get_manager():
        if router.manager is None:
            router.manager = SessionManager()
        return router.manager

    @router.websocket(WS_PATH)
    async def language_service(ws: WebSocket):
        await ws.accept()

        async def send_json(obj):
            await ws.send_text(json.dumps(obj, separators=(",", ":")))

        async def close_socket():
            try:
                await ws.close()
            except Exception:
                pass

        conn = Connection(get_manager(), send_json, close_socket)
        try:
            while not conn.should_close:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    try:
                        raw = (message.get("bytes") or b"").decode("utf-8")
                    except UnicodeDecodeError:
                        await conn.send(error_response(
                            None, PARSE_ERROR, "Parse error: message is not valid UTF-8",
                            "invalid_request", "Message is not valid UTF-8."))
                        continue
                await conn.handle_raw(raw)
        except Exception:
            # Normally only "client went away" ends up here.
            log.debug("websocket loop ended with an exception", exc_info=True)
        finally:
            # Always runs, so no vhdl_ls process or session folder is left behind.
            await conn.end_session()
            await close_socket()

    @router.get(HEALTH_PATH)
    def health():
        return {"status": "ok", "sessions": get_manager().active_count}

    router.get_manager = get_manager
    return router


router = build_router()


async def shutdown():
    """Close every open session. Called when the server stops."""
    await router.get_manager().close_all()
