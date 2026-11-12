"""The Gateway over a real WebSocket in this process, with the fake bridge.

Checks the messages, sessions, errors and clean-up without vhdl_ls.
test_real_vhdl_ls.py runs the same kind of checks with the real vhdl_ls.
"""
import asyncio
import re
import shutil
import tempfile
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest

from clients import checks
from conftest import (DESIGN, Env, change_doc, completion, fx, initialize, notify, open_doc,
                      request, uri)
from gateway.config import Config
from gateway.fake_bridge import FakeBridgeFactory
from gateway.session import SessionManager
from gateway.session_store import SessionStore

P5A_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}\.(vhd|vhdl)$", re.I)
FX01 = fx("fx01_valid.vhd")
FX02 = fx("fx02_missing_semicolon.vhd")
FX02B = fx("fx02b_corrected.vhd")
FXC1 = fx("fxc1_completion.vhd")
AFTER_SIG = (14, 11)   # just after "sig_" in fxc1_completion.vhd


def check_error(msg, code, category=None, method=None):
    """Every error has the contract's shape; returns msg["error"]."""
    assert checks.check_error(msg, code, method) == [], msg
    data = msg["error"]["data"]
    assert data["status"] == "failed" and data["stage"] == "language_service" and data["detail"]
    if category:
        assert data["category"] == category
    assert "Traceback" not in str(msg)
    return msg["error"]


# ---- FR-P5B-03 sessions, FR-P5B-07 shared file/session model -------------------------------

def test_initialize_creates_the_session_folder_and_starts_vhdl_ls(env):
    with env.ws() as ws:
        reply = initialize(ws)
        assert checks.is_success(reply)
        assert set(reply["result"]["capabilities"]) == {"textDocumentSync", "completionProvider"}
        (sid,) = env.sessions()
        assert reply["result"]["sessionId"] == sid
        folder = env.root / "sessions" / sid
        assert (folder / "src").is_dir() and (folder / "vhdl_ls.toml").is_file()
        assert env.store.list_files(sid) == []
        assert len(env.factory.instances) == 1
        assert env.factory.instances[0].session_dir == folder
    assert env.wait_clean()


def test_submitted_files_are_stored_in_src_and_can_go_to_p5a(env):
    with env.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, FX02)
        (sid,) = env.sessions()
        assert env.store.list_files(sid) == [DESIGN]
        assert env.store.read_file(sid, DESIGN) == FX02
        change_doc(ws, DESIGN, FX02B, 2)
        assert env.store.read_file(sid, DESIGN) == FX02B
        files = env.manager.files_for_p5a(sid)
        assert files == [{"name": DESIGN, "content": FX02B}]
        assert all(P5A_FILE_NAME_RE.match(f["name"]) for f in files)
        body = env.manager.p5a_request(sid, "design_tb", "100ns")
        assert body == {"files": files, "top": "design_tb", "stop_time": "100ns"}
        assert "stop_time" not in env.manager.p5a_request(sid, "design_tb")
    assert env.wait_clean()


@pytest.mark.parametrize("name", ["bad name.txt", "x.v", "../x.vhd", "a b.vhd", ".vhd",
                                  "-x.vhd", "x.vhd.txt", "bad/x.vhd"])
def test_file_outside_the_prescribed_set_is_rejected_and_not_written(env, name):
    with env.ws() as ws:
        initialize(ws)
        notify(ws, "textDocument/didOpen", {"textDocument": {"uri": f"file:///{name}", "text": FX01}})
        error = ws.receive_json()
        assert error["id"] is None
        check_error(error, -32602, "invalid_params")
        (sid,) = env.sessions()
        assert env.store.list_files(sid) == []
        assert request(ws, "textDocument/hover", id=9)["id"] == 9   # the session carries on


def test_second_initialize_is_rejected(env):
    with env.ws() as ws:
        initialize(ws)
        check_error(request(ws, "initialize", id=2), -32600, "session_error")


def test_session_limit():
    with Env(max_sessions=1) as e, e.ws() as a, e.ws() as b:
        assert checks.is_success(initialize(a))
        check_error(initialize(b), -32603, "session_error")
        assert len(e.sessions()) == 1


def test_vhdl_ls_that_cannot_start_leaves_no_folder(env):
    env.factory.fail_start = True
    with env.ws() as ws:
        check_error(initialize(ws), -32603, "language_server_failure")
        assert env.wait_clean()


def test_file_count_and_size_limits():
    with Env(max_files=2, max_file_bytes=50) as e, e.ws() as ws:
        initialize(ws)
        open_doc(ws, "a.vhd", "-- a")
        open_doc(ws, "b.vhd", "-- b")
        notify(ws, "textDocument/didOpen", {"textDocument": {"uri": uri("c.vhd"), "text": "-- c"}})
        check_error(ws.receive_json(), -32602)                       # third file
        notify(ws, "textDocument/didChange", {"textDocument": {"uri": uri("a.vhd")},
                                              "contentChanges": [{"text": "x" * 51}]})
        check_error(ws.receive_json(), -32602)                       # too big
        assert open_doc(ws, "a.vhd", "-- ok again")["method"] == "textDocument/publishDiagnostics"


# ---- FR-P5B-01 diagnostics -----------------------------------------------------------------

def test_valid_design_gives_an_empty_list_and_no_errors_message(env):
    with env.ws() as ws:
        initialize(ws)
        msg = open_doc(ws, DESIGN, FX01)
        assert msg["method"] == "textDocument/publishDiagnostics" and "id" not in msg
        assert msg["params"] == {"uri": "file:///design.vhd", "version": 1, "diagnostics": [],
                                 "summary": "No errors present."}


def test_missing_semicolon_gives_an_error_on_that_line(env):
    with env.ws() as ws:
        initialize(ws)
        params = open_doc(ws, DESIGN, FX02)["params"]
        assert params["uri"] == "file:///design.vhd" and params["summary"] == "1 error found."
        (d,) = params["diagnostics"]
        assert d == {"range": {"start": {"line": 12, "character": 14},
                               "end": {"line": 12, "character": 15}},
                     "severity": 1, "code": "syntax", "source": "vhdl_ls",
                     "message": "Expected ';' after assignment (in design.vhd)"}  # path removed


def test_change_with_the_fix_clears_the_error(env):
    with env.ws() as ws:
        initialize(ws)
        assert open_doc(ws, DESIGN, FX02)["params"]["diagnostics"]
        params = change_doc(ws, DESIGN, FX02B, 2)["params"]
        assert params["diagnostics"] == [] and params["summary"] == "No errors present."
        assert params["version"] == 2


def test_no_host_paths_in_replies(env):
    with env.ws() as ws:
        initialize(ws)
        msg = open_doc(ws, DESIGN, FX02)
        assert str(env.root) not in str(msg) and "sessions" not in str(msg)


def test_other_open_files_are_updated_when_their_diagnostics_change(env):
    with env.ws() as ws:
        initialize(ws)
        tb = open_doc(ws, "tb.vhd", "  y <= a")["params"]           # an error in tb.vhd
        assert tb["uri"] == "file:///tb.vhd" and tb["diagnostics"]
        bridge = env.factory.instances[0]
        bridge.latest_diagnostics = lambda path: [] if Path(path).name == "tb.vhd" else None
        first = open_doc(ws, DESIGN, FX01)["params"]
        assert first["uri"] == "file:///design.vhd"
        second = ws.receive_json()["params"]                          # tb.vhd sent again, now clean
        assert second == {"uri": "file:///tb.vhd", "version": 1, "diagnostics": [],
                          "summary": "No errors present."}
        open_doc(ws, DESIGN, FX01, 2)                                 # no change: nothing extra sent
        assert checks.is_success(request(ws, "initialized", id=7))


def test_ranged_change_is_rejected_and_the_session_carries_on(env):
    with env.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, FX01)
        notify(ws, "textDocument/didChange", {"textDocument": {"uri": uri(DESIGN)}, "contentChanges": [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
             "text": "x"}]})
        error = ws.receive_json()
        assert error["id"] is None
        check_error(error, -32602, "invalid_params")
        assert open_doc(ws, DESIGN, FX02)["params"]["diagnostics"]


# ---- FR-P5B-02 completion ------------------------------------------------------------------

@pytest.mark.parametrize("style", ["list", "completion_list"])
def test_completion_offers_sig_clock_and_sig_reset(env, style):
    with env.ws() as ws:
        initialize(ws)
        env.factory.instances[0].completion_style = style
        open_doc(ws, DESIGN, FXC1)
        reply = completion(ws, DESIGN, *AFTER_SIG)
        assert checks.is_success(reply) and reply["id"] == 5
        assert reply["result"]["isIncomplete"] is False
        assert {i["label"] for i in reply["result"]["items"]} == {"sig_clock", "sig_reset"}


def test_no_completion_gives_an_empty_list(env):
    with env.ws() as ws:
        initialize(ws)
        env.factory.instances[0].completion_style = "none"
        open_doc(ws, DESIGN, FXC1)
        assert completion(ws, DESIGN, *AFTER_SIG)["result"] == {"isIncomplete": False, "items": []}


@pytest.mark.parametrize("params", [
    {"textDocument": {"uri": uri(DESIGN)}, "position": {"line": -1, "character": 0}},
    {"textDocument": {"uri": uri(DESIGN)}, "position": {"line": "1", "character": 0}},
    {"textDocument": {"uri": uri(DESIGN)}},
    {"textDocument": {"uri": uri("other name.vhd")}, "position": {"line": 0, "character": 0}},
    {"textDocument": {"uri": "../../etc/passwd"}, "position": {"line": 0, "character": 0}}])
def test_completion_with_bad_params(env, params):
    with env.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, FXC1)
        check_error(request(ws, "textDocument/completion", params), -32602, "invalid_params")


def test_completion_needs_an_open_file(env):
    with env.ws() as ws:
        initialize(ws)
        check_error(completion(ws, DESIGN, 0, 0), -32602)
        open_doc(ws, DESIGN, FXC1)
        assert checks.is_success(completion(ws, DESIGN, *AFTER_SIG))
        notify(ws, "textDocument/didClose", {"textDocument": {"uri": uri(DESIGN)}})
        check_error(completion(ws, DESIGN, *AFTER_SIG), -32602)


def test_error_answer_from_vhdl_ls_does_not_end_the_session(env):
    with env.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, FXC1)
        env.factory.instances[0].refuse_completion = True
        check_error(completion(ws, DESIGN, *AFTER_SIG), -32603, "language_server_failure")
        env.factory.instances[0].refuse_completion = False
        assert checks.is_success(completion(ws, DESIGN, *AFTER_SIG, id=6))
        assert len(env.sessions()) == 1


# ---- FR-P5B-04 only the approved subset ----------------------------------------------------

@pytest.mark.parametrize("method", ["textDocument/hover", "textDocument/definition",
                                    "textDocument/rename", "textDocument/formatting",
                                    "workspace/executeCommand"])
def test_unapproved_requests_are_rejected_and_named(env, method):
    with env.ws() as ws:
        initialize(ws)
        reply = request(ws, method, {"textDocument": {"uri": uri(DESIGN)}})
        check_error(reply, -32601, "unsupported_method", method)


def test_unapproved_request_is_rejected_before_initialize_too(env):
    with env.ws() as ws:
        check_error(request(ws, "textDocument/hover"), -32601, method="textDocument/hover")


def test_unsupported_notification_gets_an_error_but_dollar_notifications_are_ignored(env):
    with env.ws() as ws:
        initialize(ws)
        notify(ws, "$/cancelRequest", {"id": 1})
        notify(ws, "$/setTrace", {"value": "off"})
        notify(ws, "textDocument/didSave", {})
        error = ws.receive_json()                  # the first reply is for didSave
        assert error["id"] is None
        check_error(error, -32601, "unsupported_method", "textDocument/didSave")


def test_invalid_messages_then_a_valid_request(env):
    with env.ws() as ws:
        ws.send_text("this is not json")
        check_error(ws.receive_json(), -32700, "invalid_request")
        ws.send_text('{"jsonrpc":"2.0","id":3,"method":')
        check_error(ws.receive_json(), -32700)
        ws.send_bytes(b"\xff\xfe")
        check_error(ws.receive_json(), -32700)
        reply = initialize(ws, id=4)
        assert reply["id"] == 4 and checks.is_success(reply)


def test_requests_before_initialize_are_rejected(env):
    with env.ws() as ws:
        check_error(completion(ws, DESIGN, 0, 0), -32002, "session_error")
        notify(ws, "textDocument/didOpen", {"textDocument": {"uri": uri(DESIGN), "text": FX01}})
        error = ws.receive_json()
        assert error["id"] is None
        check_error(error, -32002)
        assert env.sessions() == []


def test_success_and_failure_differ_in_structure(env):
    with env.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, FXC1)
        ok = completion(ws, DESIGN, *AFTER_SIG)
        bad = request(ws, "textDocument/hover", id=2)
        assert checks.is_success(ok) and not checks.is_error(ok)
        assert checks.is_error(bad) and not checks.is_success(bad)


def test_message_size_limit():
    with Env(max_message_bytes=200) as e, e.ws() as ws:
        ws.send_text('{"jsonrpc":"2.0","id":1,"method":"x","params":{"a":"' + "y" * 300 + '"}}')
        check_error(ws.receive_json(), -32600)


# ---- FR-P5B-05 VHDL-LS failures, and clean-up ------------------------------------------------

def test_vhdl_ls_dying_is_reported_and_other_sessions_carry_on(env):
    with env.ws() as a, env.ws() as b:
        initialize(a)
        initialize(b)
        env.factory.instances[0].kill()
        notify(a, "textDocument/didOpen", {"textDocument": {"uri": uri(DESIGN), "text": FX01}})
        error = a.receive_json()
        assert error["id"] is None
        assert "terminated" in check_error(error, -32603, "language_server_failure")["message"]
        assert env.factory.instances[0].closed and len(env.sessions()) == 1
        check_error(completion(a, DESIGN, 0, 0, 8), -32603, "language_server_failure")
        assert open_doc(b, DESIGN, FX01)["params"]["diagnostics"] == []   # b is not affected
        assert checks.is_success(initialize(a, id=9))                     # a can start again


def test_hung_vhdl_ls_is_reported(env):
    with env.ws() as ws:
        initialize(ws)
        env.factory.instances[0].hang = True
        notify(ws, "textDocument/didOpen", {"textDocument": {"uri": uri(DESIGN), "text": FX01}})
        check_error(ws.receive_json(), -32603, "language_server_failure")
        assert env.wait_clean()


def test_death_is_reported_even_without_a_request():
    with Env(watchdog_interval_s=0.05) as e, e.ws() as ws:
        initialize(ws)
        e.factory.instances[0].kill()
        msg = ws.receive_json()
        assert msg["id"] is None
        assert "terminated" in check_error(msg, -32603, "language_server_failure")["message"]
        assert e.wait_clean()


def test_idle_session_is_closed():
    with Env(watchdog_interval_s=0.05, idle_timeout_s=0.2) as e, e.ws() as ws:
        initialize(ws)
        check_error(ws.receive_json(), -32603, "session_error")
        assert e.wait_clean()
        message = ws.receive()
        assert message["type"] == "websocket.close"                    # the server hung up


def test_disconnect_leaves_no_process_or_folder(env):
    with env.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, FX01)
    assert env.wait_clean()


def test_shutdown_deletes_the_session(env):
    with env.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, FX01)
        assert request(ws, "shutdown", id=2) == {"jsonrpc": "2.0", "id": 2, "result": None}
        assert env.wait_clean()
        check_error(completion(ws, DESIGN, 0, 0, 3), -32600, "session_error")
        notify(ws, "exit")


def test_two_sessions_are_kept_apart(env):
    with env.ws() as a, env.ws() as b:
        initialize(a)
        initialize(b)
        sa, sb = env.sessions()
        assert open_doc(a, DESIGN, FX02)["params"]["diagnostics"]
        assert not open_doc(b, DESIGN, FX01)["params"]["diagnostics"]
        assert env.store.read_file(sa, DESIGN) != env.store.read_file(sb, DESIGN)


def test_clean_up_finishes_even_if_the_caller_is_cancelled():
    async def run():
        root = Path(tempfile.mkdtemp(prefix="p5b-cancel-"))
        store = SessionStore(root)
        factory = FakeBridgeFactory()
        manager = SessionManager(replace(Config(), request_timeout_s=2.0), factory, store)
        session = await manager.open()
        task = asyncio.create_task(manager.close(session.id))
        await asyncio.sleep(0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not list((root / "sessions").iterdir()):
                break
            await asyncio.sleep(0.02)
        assert not list((root / "sessions").iterdir())
        assert factory.instances[0].closed
        shutil.rmtree(root, ignore_errors=True)

    asyncio.run(run())


# ---- FR-P5B-06 same results every time ---------------------------------------------------------

def _scenario():
    replies = []
    with Env() as e, e.ws() as ws:
        replies.append(initialize(ws))
        replies.append(open_doc(ws, DESIGN, FX02))
        replies.append(change_doc(ws, DESIGN, FX02B, 2))
        open_doc(ws, "other.vhd", FXC1)
        replies.append(completion(ws, "other.vhd", *AFTER_SIG, 3))
        replies.append(request(ws, "textDocument/hover", id=4))
    replies[0]["result"]["sessionId"] = "<id>"
    return replies


def test_two_runs_give_identical_replies():
    assert _scenario() == _scenario()


# ---- check_files (used by the parent backend's /check-and-simulate) ------------------------------

def test_check_files_returns_one_result_per_file_and_cleans_up(env):
    async def run():
        return await env.manager.check_files([{"name": "a.vhd", "content": FX01},
                                              {"name": "b.vhd", "content": FX02}])
    results = asyncio.run(run())
    assert [r["uri"] for r in results] == ["file:///a.vhd", "file:///b.vhd"]
    assert results[0]["diagnostics"] == [] and results[0]["summary"] == "No errors present."
    assert results[1]["diagnostics"][0]["range"]["start"]["line"] == 12
    assert env.wait_clean()
