# Lets "from gateway import ..." work when pytest is run from the repo root.
import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway.config import Config  # noqa: E402
from gateway.fake_bridge import FakeBridgeFactory  # noqa: E402
from gateway.router import WS_PATH, build_router  # noqa: E402
from gateway.session import SessionManager  # noqa: E402
from gateway.session_store import SessionStore  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "vhdl"
DESIGN = "design.vhd"

# The real vhdl_ls, if it is installed (P5B_VHDL_LS_PATH or on PATH).
VHDL_LS = shutil.which(os.environ.get("P5B_VHDL_LS_PATH") or "vhdl_ls")

# Tests marked with this are SKIPPED (not failed) when vhdl_ls is not installed.
needs_vhdl_ls = pytest.mark.skipif(
    VHDL_LS is None, reason="vhdl_ls not found (put it on PATH or set P5B_VHDL_LS_PATH)")


def fx(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def uri(name):
    return f"file:///{name}"


class Env:
    """A Gateway in this process, with a real SessionStore in a temp folder.

    Uses the fake bridge unless real=True (then the real vhdl_ls is started).
    """

    def __init__(self, real=False, **settings):
        defaults = {"watchdog_interval_s": 60.0, "request_timeout_s": 2.0,
                    "diagnostics_timeout_s": 1.0}
        if real:
            defaults = {"watchdog_interval_s": 60.0}
        self.cfg = replace(Config(), **{**defaults, **settings})
        self.root = Path(tempfile.mkdtemp(prefix="p5b-test-"))
        self.store = SessionStore(self.root)
        self.factory = None if real else FakeBridgeFactory()
        self.manager = SessionManager(self.cfg, self.factory, self.store)
        self.app = FastAPI()
        self.app.include_router(build_router(self.manager))

    def __enter__(self):
        self.client = TestClient(self.app).__enter__()
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)
        shutil.rmtree(self.root, ignore_errors=True)

    def ws(self):
        return self.client.websocket_connect(WS_PATH)

    def sessions(self):
        return sorted(p.name for p in (self.root / "sessions").iterdir())

    def wait_clean(self, timeout=5.0):
        """True once every session folder is gone and every bridge is closed."""
        end = time.time() + timeout
        while time.time() < end:
            bridges_closed = self.factory is None or all(b.closed for b in self.factory.instances)
            if not self.sessions() and self.manager.active_count == 0 and bridges_closed:
                return True
            time.sleep(0.02)
        return False


@pytest.fixture
def env():
    with Env() as e:
        yield e


def request(ws, method, params=None, id=1):
    ws.send_json({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})
    return ws.receive_json()


def notify(ws, method, params=None):
    ws.send_json({"jsonrpc": "2.0", "method": method, "params": params or {}})


def initialize(ws, id=1):
    reply = request(ws, "initialize", {"processId": None, "rootUri": None, "capabilities": {}}, id)
    notify(ws, "initialized", {})
    return reply


def open_doc(ws, name, text, version=1):
    notify(ws, "textDocument/didOpen", {"textDocument": {
        "uri": uri(name), "languageId": "vhdl", "version": version, "text": text}})
    return ws.receive_json()


def change_doc(ws, name, text, version):
    notify(ws, "textDocument/didChange", {"textDocument": {"uri": uri(name), "version": version},
                                          "contentChanges": [{"text": text}]})
    return ws.receive_json()


def completion(ws, name, line, character, id=5):
    return request(ws, "textDocument/completion", {
        "textDocument": {"uri": uri(name)}, "position": {"line": line, "character": character}}, id)
