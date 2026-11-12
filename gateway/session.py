"""Language-service sessions: open -> active -> closed.

A session is a folder from SessionStore plus one vhdl_ls process (a Bridge).
SessionManager keeps track of them and always removes both when a session ends.
"""
from __future__ import annotations

import asyncio
import logging
import time

from gateway import mappers
from gateway.config import Config
from gateway.jsonrpc import INTERNAL_ERROR, INVALID_PARAMS, GatewayError
from gateway.session_store import SessionStore, UnknownSession
from gateway.vhdl_ls_bridge import start_bridge

log = logging.getLogger("p5b.session")


class Session:
    def __init__(self, session_id, store, bridge):
        self.id = session_id
        self.store = store
        self.bridge = bridge
        self.dir = store.session_dir(session_id)
        self.versions = {}    # file name -> version of the text we have
        self.opened = set()   # files currently open in vhdl_ls
        self.published = {}   # file name -> diagnostics last sent to the client
        self.scrub = mappers.make_scrubber(str(self.dir.resolve()))
        self.last_activity = time.monotonic()

    def touch(self):
        self.last_activity = time.monotonic()

    def path_for(self, name):
        return self.store.file_path(self.id, name)

    def resolve_uri(self, uri):
        """The file name in a client URI, or an error if it is not in the prescribed set."""
        name = mappers.uri_to_name(uri)
        if name is None:
            raise GatewayError(
                INVALID_PARAMS, "File not in the prescribed file set", "invalid_params",
                "Files must be flat names (letters, digits, _ . -) of at most 100 "
                "characters ending in .vhd or .vhdl, sent as file:///<name>.")
        return name


class SessionManager:
    def __init__(self, cfg=None, bridge_factory=None, store=None):
        self.cfg = cfg or Config.from_env()
        self._bridge_factory = bridge_factory
        self._store = store
        self._sessions = {}
        self._starting = 0
        self._closing = set()

    @property
    def store(self):
        # Made on first use, so importing the Gateway does not create folders.
        if self._store is None:
            self._store = SessionStore(self.cfg.root_path)
        return self._store

    def _factory(self):
        if self._bridge_factory is None:
            if self.cfg.bridge == "fake":
                from gateway.fake_bridge import FakeBridgeFactory
                self._bridge_factory = FakeBridgeFactory()
            else:
                self._bridge_factory = start_bridge
        return self._bridge_factory

    @property
    def active_count(self):
        return len(self._sessions)

    # ----------------------------------------------------------- hand-off to P5A

    def files_for_p5a(self, session_id):
        """The session's files as P5A's "files" list: [{"name": ..., "content": ...}]."""
        return [{"name": name, "content": self.store.read_file(session_id, name)}
                for name in self.store.list_files(session_id)]

    def p5a_request(self, session_id, top, stop_time=None):
        """Request body for P5A's POST /simulate, made from this session's files.

        P5A also needs the testbench entity (top). Only the client knows which
        entity that is, so it is passed in.
        """
        body = {"files": self.files_for_p5a(session_id), "top": top}
        if stop_time:
            body["stop_time"] = stop_time
        return body

    # ----------------------------------------------------------------- lifecycle

    async def open(self):
        if len(self._sessions) + self._starting >= self.cfg.max_sessions:
            raise GatewayError(INTERNAL_ERROR, "Session limit reached", "session_error",
                               f"At most {self.cfg.max_sessions} sessions may be active.")
        self._starting += 1
        try:
            try:
                session_id = await asyncio.to_thread(self.store.create_session)
            except Exception:
                log.exception("could not create a session folder")
                raise GatewayError(INTERNAL_ERROR, "Could not create session files",
                                   "internal_error")
            try:
                bridge = await asyncio.wait_for(
                    self._factory()(self.store.session_dir(session_id)),
                    self.cfg.request_timeout_s)
            except Exception:
                log.exception("vhdl_ls did not start")
                await self._delete(session_id)
                raise GatewayError(INTERNAL_ERROR, "VHDL-LS process could not be started",
                                   "language_server_failure",
                                   "Check that vhdl_ls is installed (P5B_VHDL_LS_PATH or PATH).")
            session = Session(session_id, self.store, bridge)
            self._sessions[session_id] = session
            return session
        finally:
            self._starting -= 1

    async def write_file(self, session, name, text):
        try:
            await asyncio.to_thread(self.store.write_file, session.id, name, text)
        except Exception:
            log.exception("could not write %s", name)
            raise GatewayError(INTERNAL_ERROR, "Could not store the file", "internal_error")

    async def close(self, session_id):
        """Stop vhdl_ls and delete the folder. Safe to call more than once.

        The clean-up runs as its own task and is shielded, so it still finishes
        if the caller is cancelled (client gone, server stopping).
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        task = asyncio.ensure_future(self._teardown(session))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)
        await asyncio.shield(task)

    async def _teardown(self, session):
        try:
            await asyncio.wait_for(session.bridge.close(), self.cfg.request_timeout_s)
        except Exception:
            log.exception("vhdl_ls did not close cleanly for session %s", session.id)
        finally:
            await self._delete(session.id)

    async def _delete(self, session_id):
        try:
            await asyncio.to_thread(self.store.delete_session, session_id)
        except UnknownSession:
            pass  # already gone
        except Exception:
            log.exception("could not delete session %s", session_id)

    async def close_all(self):
        for session_id in list(self._sessions):
            await self.close(session_id)

    # ------------------------------------------------- one-off check (no WebSocket)

    async def check_files(self, files):
        """Run vhdl_ls once over a list of {"name", "content"} files.

        Returns one publishDiagnostics-style dict per file, in the order given.
        All files are opened before any diagnostics are read, so a testbench
        is checked with its entity present. Used by the parent backend's
        POST /check-and-simulate.
        """
        session = await self.open()
        try:
            for f in files:
                if mappers.uri_to_name(f["name"]) != f["name"]:
                    raise GatewayError(INVALID_PARAMS, "File not in the prescribed file set",
                                       "invalid_params", f"Bad file name: {mappers.safe_text(f['name'])}")
                await self.write_file(session, f["name"], f["content"])
                await session.bridge.open_file(session.path_for(f["name"]), f["content"])
            results = []
            for f in files:
                raw = await session.bridge.get_diagnostics(session.path_for(f["name"]),
                                                           self.cfg.diagnostics_wait_s)
                results.append(mappers.to_publish_diagnostics(
                    mappers.name_to_uri(f["name"]), 1, raw, session.scrub))
            return results
        finally:
            await self.close(session.id)
