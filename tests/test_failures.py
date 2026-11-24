"""White-box tests added after reviewing the coverage report (coverage.py,
statement + branch). The lines no test reached were mostly the Gateway's
error handling: the disk or VHDL-LS failing part-way through, and the
combined backend's error replies. Each test makes one of those fail and
checks the client still gets a proper reply and nothing is left behind.
"""
import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from conftest import Env, fx, import_parent_main, initialize, notify, open_doc, uri
from gateway.config import Config
from gateway.fake_bridge import FakeBridge
from gateway.jsonrpc import GatewayError
from gateway.session import SessionManager
from gateway.vhdl_ls_bridge import LanguageServerDied

FX01 = fx("fx01_valid.vhd")


# ------------------------------------------------------------ sessions

def test_session_folder_cannot_be_created(env, monkeypatch):
    def full_disk():
        raise OSError("No space left on device")
    monkeypatch.setattr(env.store, "create_session", full_disk)
    with env.ws() as ws:
        reply = initialize(ws)
        assert reply["error"]["code"] == -32603
        assert reply["error"]["data"]["category"] == "internal_error"


def test_file_cannot_be_stored(env, monkeypatch):
    def fail(*_a):
        raise OSError("disk error")
    with env.ws() as ws:
        initialize(ws)
        monkeypatch.setattr(env.store, "write_file", fail)
        notify(ws, "textDocument/didOpen", {"textDocument": {"uri": uri("a.vhd"), "text": FX01}})
        assert ws.receive_json()["error"]["data"]["category"] == "internal_error"
        monkeypatch.undo()
        assert open_doc(ws, "a.vhd", FX01)["method"] == "textDocument/publishDiagnostics"
    assert env.wait_clean()


def test_vhdl_ls_that_fails_to_close_is_still_cleaned_up(env, monkeypatch):
    async def broken_close(self):
        raise RuntimeError("would not stop")
    monkeypatch.setattr(FakeBridge, "close", broken_close)
    with env.ws() as ws:
        initialize(ws)
    deadline = 50
    while env.sessions() and deadline:
        asyncio.run(asyncio.sleep(0.02))
        deadline -= 1
    assert env.sessions() == [] and env.manager.active_count == 0


def test_folder_that_cannot_be_deleted_does_not_crash(env, monkeypatch):
    def locked(_sid):
        raise PermissionError("in use")
    with env.ws() as ws:
        initialize(ws)
        monkeypatch.setattr(env.store, "delete_session", locked)
    assert env.manager.active_count == 0              # session released anyway


def test_close_all_closes_every_session():
    with Env() as e:
        a, b = e.ws().__enter__(), e.ws().__enter__()
        initialize(a)
        initialize(b)
        assert e.manager.active_count == 2
        asyncio.run(e.manager.close_all())
        assert e.manager.active_count == 0 and e.sessions() == []
        a.__exit__(None, None, None)
        b.__exit__(None, None, None)


def test_check_files_rejects_a_bad_name(tmp_path):
    manager = SessionManager(replace(Config(), bridge="fake", session_root=str(tmp_path)))
    with pytest.raises(GatewayError) as exc:
        asyncio.run(manager.check_files([{"name": "../x.vhd", "content": "x"}]))
    assert exc.value.category == "invalid_params" and manager.active_count == 0


# ------------------------------------------------------------ P5B on its own

def test_standalone_gateway_app():
    from gateway import main
    with TestClient(main.app) as c:
        assert "running" in c.get("/").json()["message"]


# ------------------------------------------------------------ combined backend

@pytest.fixture
def parent(tmp_path, monkeypatch):
    parent_main = import_parent_main()
    from app import docker_runner
    monkeypatch.setattr(docker_runner, "cleanup_stale_jobs", lambda *a, **k: (0, 0))
    manager = SessionManager(replace(Config(), bridge="fake", session_root=str(tmp_path),
                                     watchdog_interval_s=60.0))
    monkeypatch.setattr(parent_main.p5b_router, "manager", manager)
    return parent_main, manager


CLEAN = {"files": [{"name": "a.vhd", "content": "entity a is\nend entity;\n"}], "top": "a"}


@pytest.mark.parametrize("error, status, code", [
    (LanguageServerDied("crashed"), 503, "internal_error"),
    (GatewayError(-32602, "Bad", "invalid_params", "bad name"), 400, "validation_error"),
], ids=["vhdl_ls_dies_while_checking", "gateway_rejects_the_files"])
def test_check_and_simulate_error_replies(parent, monkeypatch, error, status, code):
    parent_main, manager = parent

    async def fail(files):
        raise error
    monkeypatch.setattr(manager, "check_files", fail)
    with TestClient(parent_main.app) as c:
        r = c.post("/check-and-simulate", json=CLEAN)
    assert r.status_code == status and r.json()["error"] == code


def test_check_and_simulate_passes_on_p5as_docker_down_reply(parent, monkeypatch):
    """Files are clean, but Docker is down: P5A's own 503 comes back as is."""
    parent_main, _ = parent
    from app import docker_runner

    def down(*_a, **_k):
        raise docker_runner.DockerException("refused")
    monkeypatch.setattr(docker_runner, "get_client", down)
    with TestClient(parent_main.app) as c:
        r = c.post("/check-and-simulate", json=CLEAN)
    assert r.status_code == 503 and r.json()["error"] == "internal_error"


def test_shutdown_problem_is_logged_not_raised(parent, monkeypatch):
    parent_main, _ = parent

    async def broken():
        raise RuntimeError("could not close")
    monkeypatch.setattr(parent_main, "p5b_shutdown", broken)
    with TestClient(parent_main.app) as c:
        c.get("/health")                              # leaving the block runs shutdown


def test_find_p5a_uses_p5a_dir_and_explains_when_missing(parent, monkeypatch, tmp_path):
    parent_main, _ = parent
    monkeypatch.setenv("P5A_DIR", str(parent_main.P5A_DIR))
    assert parent_main.find_p5a() == parent_main.P5A_DIR
    monkeypatch.setenv("P5A_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="not found"):
        parent_main.find_p5a()
