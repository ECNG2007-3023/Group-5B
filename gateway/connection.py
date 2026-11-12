"""One WebSocket connection: handles its messages and watches its vhdl_ls process."""
from __future__ import annotations

import asyncio
import logging
import time

from gateway.jsonrpc import (INTERNAL_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, NOT_INITIALIZED,
                             GatewayError, error_from, error_response, parse_message,
                             result_response)
from gateway.mappers import safe_text
from gateway.methods import ALLOWED
from gateway.vhdl_ls_bridge import LanguageServerDied, LanguageServerError, LanguageServerTimeout

log = logging.getLogger("p5b.connection")

LS_DIED = "VHDL-LS process terminated unexpectedly"
LS_HUNG = "VHDL-LS stopped responding and the process was terminated"
NEW_SESSION = "Send initialize to start a new session."


class Connection:
    def __init__(self, manager, send_json, close_socket=None):
        self.manager = manager
        self.cfg = manager.cfg
        self._send_json = send_json
        self._close_socket = close_socket
        self._send_lock = asyncio.Lock()
        self.session = None
        self.state = "new"      # new, ready, failed or shutdown
        self.should_close = False
        self._watchdog = None

    async def send(self, obj):
        async with self._send_lock:
            try:
                await self._send_json(obj)
            except Exception:
                log.debug("could not send, the client has probably gone")

    # ------------------------------------------------------------ state checks

    def reject_if_shutdown(self):
        if self.state == "shutdown":
            raise GatewayError(INVALID_REQUEST, "Session shut down", "session_error",
                               "The session has been shut down; only exit is accepted.")

    def require_ready(self):
        self.reject_if_shutdown()
        if self.state == "failed":
            raise GatewayError(INTERNAL_ERROR, LS_DIED, "language_server_failure",
                               "The VHDL-LS process is no longer running. " + NEW_SESSION)
        if self.state != "ready" or self.session is None:
            raise GatewayError(NOT_INITIALIZED, "Server not initialized", "session_error",
                               "Send initialize first.")
        return self.session

    # --------------------------------------------------------------- lifecycle

    async def open_session(self):
        self.session = await self.manager.open()
        self.state = "ready"
        self._stop_watchdog()
        self._watchdog = asyncio.create_task(self._watch())
        return self.session

    async def end_session(self):
        """Normal end (shutdown, exit or disconnect). Safe to call more than once."""
        self._stop_watchdog()
        session = self.session
        self.session = None
        if session is not None:
            await self.manager.close(session.id)
        if self.state in ("ready", "failed"):
            self.state = "shutdown"

    async def fail(self, message, notify=False):
        """vhdl_ls died or hung: release the session and optionally tell the client."""
        session = self.session
        if session is None:
            return
        self.session = None
        self.state = "failed"
        self._stop_watchdog()
        await self.manager.close(session.id)
        if notify:
            await self.send(error_response(None, INTERNAL_ERROR, message,
                                           "language_server_failure", NEW_SESSION))

    def _stop_watchdog(self):
        if self._watchdog is not None and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None

    async def _watch(self):
        """Notices a dead vhdl_ls even when the client is not sending anything,
        and closes sessions that have been idle too long."""
        while self.session is not None:
            await asyncio.sleep(self.cfg.watchdog_interval_s)
            session = self.session
            if session is None:
                return
            if not session.bridge.is_alive:
                await self.fail(LS_DIED, notify=True)
                return
            if time.monotonic() - session.last_activity > self.cfg.idle_timeout_s:
                await self.end_session()
                await self.send(error_response(None, INTERNAL_ERROR,
                                               "Session closed after being idle",
                                               "session_error", NEW_SESSION))
                self.should_close = True
                if self._close_socket is not None:
                    await self._close_socket()
                return

    async def call_bridge(self, call):
        """Await a vhdl_ls call and turn its failures into contract errors."""
        try:
            return await asyncio.wait_for(call, self.cfg.request_timeout_s)
        except (LanguageServerDied, LanguageServerTimeout, asyncio.TimeoutError) as exc:
            message = LS_DIED if isinstance(exc, LanguageServerDied) else LS_HUNG
            await self.fail(message)
            raise GatewayError(INTERNAL_ERROR, message, "language_server_failure", NEW_SESSION)
        except LanguageServerError as exc:
            # vhdl_ls is fine but refused this one request: the session carries on.
            raise GatewayError(INTERNAL_ERROR, "VHDL-LS could not answer the request",
                               "language_server_failure",
                               self.session.scrub(str(exc)) if self.session else "")

    # ---------------------------------------------------------------- messages

    async def handle_raw(self, raw):
        try:
            msg = parse_message(raw, self.cfg.max_message_bytes)
        except GatewayError as exc:
            await self.send(error_from(exc))
            return
        if self.session is not None:
            self.session.touch()

        handler = ALLOWED.get(msg.method)
        if handler is None and msg.is_notification and msg.method.startswith("$/"):
            return  # optional LSP notifications such as $/cancelRequest are ignored
        try:
            if handler is None:
                raise GatewayError(METHOD_NOT_FOUND,
                                   f"Method not supported: {safe_text(msg.method)}",
                                   "unsupported_method",
                                   "Only the approved diagnostics/completion subset is supported.")
            result = await handler(self, msg.params)
            if not msg.is_notification:
                await self.send(result_response(msg.id, result))
        except GatewayError as exc:
            # A notification has no id to answer, so its error goes out with id null.
            await self.send(error_from(exc, None if msg.is_notification else msg.id))
        except Exception:
            log.exception("unexpected error while handling %s", safe_text(msg.method))
            await self.send(error_response(None if msg.is_notification else msg.id,
                                           INTERNAL_ERROR, "Internal error", "internal_error",
                                           "The gateway hit an unexpected error."))
