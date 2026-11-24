"""Black-box tests, designed with the techniques from the ECNG 2007 lecture
"White Box and Black Box Testing": equivalence partitioning (EP), boundary
value analysis (BVA), decision tables, state transitions and error guessing.

Only the Gateway's inputs and outputs are used: JSON-RPC messages over the
WebSocket, and HTTP for /check-and-simulate. VHDL-LS is replaced by the fake
bridge where the Gateway's own rules are under test (the limits, the
lifecycle), and the real VHDL-LS is used where its answer matters. Test IDs
(TC-B-xx) match the test case tables in docs/test-plan.md.
"""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from conftest import (Env, completion, fx, import_parent_main, initialize, needs_vhdl_ls, notify,
                      open_doc, request, uri)
from gateway.config import Config
from gateway.session import SessionManager

LIMITS = Config()                     # the real limits: 8 sessions, 20 files, 200,000 bytes
FX01 = fx("fx01_valid.vhd")
FX02 = fx("fx02_missing_semicolon.vhd")


def is_diagnostics(msg):
    return msg.get("method") == "textDocument/publishDiagnostics"


def code(msg):
    return msg.get("error", {}).get("code")


def open_raw(ws, name, text):
    notify(ws, "textDocument/didOpen", {"textDocument": {
        "uri": uri(name), "languageId": "vhdl", "version": 1, "text": text}})
    return ws.receive_json()


# =========================================================================
# Equivalence partitioning + boundary value analysis
# =========================================================================

# ---- Files per session. Partitions: 1..20 (valid) | >20 (invalid).
#      Boundaries: 19, 20 accepted; 21 rejected.

def test_ep_bva_files_per_session():
    """TC-B-01 (19 files), TC-B-02 (20 files), TC-B-03 (21st file rejected)."""
    n = LIMITS.max_files
    with Env() as e, e.ws() as ws:
        initialize(ws)
        for i in range(n - 1):
            assert is_diagnostics(open_raw(ws, f"f{i}.vhd", f"-- {i}"))          # TC-B-01
        assert is_diagnostics(open_raw(ws, f"f{n - 1}.vhd", "-- last"))         # TC-B-02
        assert code(open_raw(ws, f"f{n}.vhd", "-- one too many")) == -32602     # TC-B-03
        assert is_diagnostics(open_raw(ws, "f0.vhd", "-- re-opening an old one is fine"))


# ---- File size. Partitions: 0..200,000 bytes (valid) | >200,000 (invalid).
#      Boundaries: 199,999 and 200,000 accepted; 200,001 rejected.

@pytest.mark.parametrize("size, ok", [(LIMITS.max_file_bytes - 1, True),
                                      (LIMITS.max_file_bytes, True),
                                      (LIMITS.max_file_bytes + 1, False)],
                         ids=["TC-B-04_199999", "TC-B-05_200000", "TC-B-06_200001"])
def test_bva_file_size(size, ok):
    with Env() as e, e.ws() as ws:
        initialize(ws)
        msg = open_raw(ws, "big.vhd", "-" * size)
        assert is_diagnostics(msg) == ok
        if not ok:
            assert code(msg) == -32602


# ---- Sessions at once. Partitions: 1..8 (valid) | >8 (invalid).
#      Boundaries: 8th accepted, 9th rejected; a freed slot can be reused.

def test_ep_bva_sessions_at_once():
    """TC-B-07 (8th session accepted), TC-B-08 (9th rejected), TC-B-09 (slot reused)."""
    n = LIMITS.max_sessions
    with Env() as e:
        sockets = [e.ws().__enter__() for _ in range(n + 1)]
        try:
            for ws in sockets[:n]:
                assert "result" in initialize(ws)                               # TC-B-07
            reply = initialize(sockets[n])
            assert code(reply) == -32603                                       # TC-B-08
            assert reply["error"]["data"]["category"] == "session_error"
            request(sockets[0], "shutdown", None, id=50)
            assert "result" in initialize(sockets[n], id=51)                   # TC-B-09
        finally:
            for ws in sockets:
                ws.__exit__(None, None, None)
        assert e.wait_clean()


# ---- File name. Partitions: flat .vhd/.vhdl name of <= 100 characters (valid)
#      | wrong extension | folders / traversal | other URI forms | too long.
#      Boundary: 100 characters accepted, 101 rejected.

@pytest.mark.parametrize("name", ["a" * 96 + ".vhd", "TOP.VHDL", "x_1-2.v.vhd"],
                         ids=["TC-B-10_100_chars", "TC-B-11_upper_case", "TC-B-12_dots_dash"])
def test_ep_bva_good_names(env, name):
    with env.ws() as ws:
        initialize(ws)
        assert is_diagnostics(open_raw(ws, name, "-- ok"))


@pytest.mark.parametrize("raw_uri", [
    "file:///" + "a" * 97 + ".vhd",       # TC-B-13: 101 characters
    "file:///design.v",                   # TC-B-14: wrong extension
    "file:///sub/design.vhd",             # TC-B-15: folder
    "file:///../design.vhd",              # TC-B-16: traversal
    "file:///%2e%2e/design.vhd",          # TC-B-17: encoded traversal
    "file:///C:/design.vhd",              # TC-B-18: Windows path
    "http://host/design.vhd",             # TC-B-19: not a file URI
], ids=["TC-B-13", "TC-B-14", "TC-B-15", "TC-B-16", "TC-B-17", "TC-B-18", "TC-B-19"])
def test_ep_bva_bad_names_rejected_and_not_written(env, raw_uri):
    with env.ws() as ws:
        initialize(ws)
        notify(ws, "textDocument/didOpen", {"textDocument": {
            "uri": raw_uri, "languageId": "vhdl", "version": 1, "text": "-- x"}})
        assert code(ws.receive_json()) == -32602
        (sid,) = env.sessions()
        assert env.store.list_files(sid) == []                 # nothing reached the disk


# ---- Completion position. Partitions: whole numbers >= 0 (valid)
#      | negative | not a whole number. Boundary: 0 accepted, -1 rejected.

@pytest.mark.parametrize("line, character, ok", [
    (0, 0, True), (-1, 0, False), (0, -1, False), ("1", 0, False), (1.5, 0, False),
], ids=["TC-B-20_zero", "TC-B-21_line_-1", "TC-B-22_char_-1", "TC-B-23_text", "TC-B-24_decimal"])
def test_ep_bva_completion_position(env, line, character, ok):
    with env.ws() as ws:
        initialize(ws)
        open_doc(ws, "a.vhd", FX01)
        reply = completion(ws, "a.vhd", line, character)
        assert ("result" in reply) == ok
        if not ok:
            assert code(reply) == -32602


# ---- Request id. Partitions: string or whole number (valid: echoed back)
#      | true/false, decimal, object (invalid: -32600, id null).

@pytest.mark.parametrize("request_id, ok", [
    (7, True), ("abc", True), (0, True), (True, False), (1.5, False), ({"a": 1}, False),
], ids=["TC-B-25_int", "TC-B-26_string", "TC-B-27_zero", "TC-B-28_bool", "TC-B-29_decimal",
        "TC-B-30_object"])
def test_ep_request_id(env, request_id, ok):
    with env.ws() as ws:
        ws.send_json({"jsonrpc": "2.0", "id": request_id, "method": "initialize",
                      "params": {"capabilities": {}}})
        reply = ws.receive_json()
        if ok:
            assert reply["id"] == request_id and "result" in reply
        else:
            assert reply["id"] is None and code(reply) == -32600


# =========================================================================
# Decision table: POST /check-and-simulate (FR-P5-01, FR-P5-02)
#
#   Rule | valid request | VHDL-LS starts | VHDL-LS error | GHDL result | Action
#   R1   |      N        |       -        |       -       |      -      | 400 validation_error, no job
#   R2   |      Y        |       N        |       -       |      -      | 503 internal_error, no job
#   R3   |      Y        |       Y        |       Y       |      -      | 200 failed, no GHDL job
#   R4   |      Y        |       Y        |       N       |   success   | 200 success + VCD
#   R5   |      Y        |       Y        |       N       |   failed    | 200 failed with GHDL's error
# =========================================================================

@pytest.fixture
def combined(tmp_path, monkeypatch):
    parent_main = import_parent_main()
    from app import docker_runner
    monkeypatch.setattr(docker_runner, "cleanup_stale_jobs", lambda *a, **k: (0, 0))
    manager = SessionManager(replace(Config(), bridge="fake", session_root=str(tmp_path),
                                     watchdog_interval_s=60.0))
    monkeypatch.setattr(parent_main.p5b_router, "manager", manager)
    jobs = []
    real_run_job = docker_runner.run_job

    def fake_run_job(files, top, stop_time=None):
        """P5A with GHDL replaced: real validation, made-up GHDL outcome."""
        try:
            docker_runner.validate_request(files, top, stop_time)
        except docker_runner.JobValidationError:
            return real_run_job(files, top, stop_time)    # P5A's own 400 reply
        jobs.append(top)
        if outcome_box[0] == "success":
            return {"job_id": "j1", "status": "success", "stage": "done", "error": None,
                    "message": "Simulation finished.", "stages": [], "vcd": b"$end"}
        return {"job_id": "j1", "status": "failed", "stage": "simulation",
                "error": "runtime_error", "message": "Simulation failed.", "stages": [],
                "vcd": None}
    outcome_box = ["success"]
    monkeypatch.setattr(docker_runner, "run_job", fake_run_job)
    with TestClient(parent_main.app) as c:
        yield c, manager, jobs, outcome_box


CLEAN = [{"name": "a.vhd", "content": "entity a is\nend entity;\n"}]
SYNTAX_ERROR = [{"name": "a.vhd", "content": "entity a is\nend entity;\nx <= y\n"}]


def check_and_simulate(c, files):
    return c.post("/check-and-simulate", json={"files": files, "top": "a"})


def test_decision_table_r1_invalid_request(combined):
    """TC-B-31."""
    c, manager, jobs, _ = combined
    r = check_and_simulate(c, [{"name": "../a.vhd", "content": "x"}])
    assert r.status_code == 400 and r.json()["error"] == "validation_error" and jobs == []


def test_decision_table_r2_vhdl_ls_cannot_start(combined):
    """TC-B-32."""
    c, manager, jobs, _ = combined
    manager._factory().fail_start = True
    r = check_and_simulate(c, CLEAN)
    assert r.status_code == 503 and r.json()["error"] == "internal_error" and jobs == []


def test_decision_table_r3_syntax_error_blocks_the_job(combined):
    """TC-B-33 (FR-P5-01)."""
    c, manager, jobs, _ = combined
    r = check_and_simulate(c, SYNTAX_ERROR)
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert r.json()["job_id"] is None and jobs == []


@pytest.mark.parametrize("outcome, status, error", [("success", "success", None),
                                                    ("failed", "failed", "runtime_error")],
                         ids=["TC-B-34_R4_ghdl_ok", "TC-B-35_R5_ghdl_fails"])
def test_decision_table_r4_r5_clean_files_run_on_p5a(combined, outcome, status, error):
    c, manager, jobs, box = combined
    box[0] = outcome
    r = check_and_simulate(c, CLEAN)
    assert r.status_code == 200 and jobs == ["a"]
    assert (r.json()["status"], r.json()["error"]) == (status, error)
    assert r.json()["diagnostics"][0]["summary"] == "No errors present."


# =========================================================================
# State transitions: a connection's lifecycle (gateway/invariants.TRANSITIONS)
#
#   State    | event             | reply                         | next state
#   new      | any request       | -32002 not initialized        | new
#   new      | initialize        | capabilities                  | ready
#   ready    | initialize        | -32600 already initialized    | ready
#   ready    | didOpen           | diagnostics                   | ready
#   ready    | VHDL-LS dies      | -32603 language_server_failure| failed
#   failed   | didOpen           | -32603 language_server_failure| failed
#   failed   | initialize        | capabilities (new session)    | ready
#   ready    | shutdown          | null                          | shutdown
#   failed   | shutdown          | null                          | shutdown
#   shutdown | any but exit      | -32600 session shut down      | shutdown
#   shutdown | exit              | connection closed             | (end)
# =========================================================================

def test_state_new_rejects_requests_until_initialize(env):
    """TC-B-36: new + request -> -32002, stays new; TC-B-37: new + initialize -> ready."""
    with env.ws() as ws:
        assert code(completion(ws, "a.vhd", 0, 0)) == -32002
        assert code(completion(ws, "a.vhd", 0, 0, id=6)) == -32002      # still new
        assert "result" in initialize(ws)
        assert is_diagnostics(open_doc(ws, "a.vhd", FX01))             # now ready


def test_state_ready_rejects_second_initialize(env):
    """TC-B-38."""
    with env.ws() as ws:
        initialize(ws)
        assert code(request(ws, "initialize", {"capabilities": {}}, id=2)) == -32600
        assert is_diagnostics(open_doc(ws, "a.vhd", FX01))             # still ready


def test_state_ready_to_failed_to_ready(env):
    """TC-B-39: VHDL-LS dies -> failed; TC-B-40: failed rejects work;
    TC-B-41: initialize from failed -> ready with a new session."""
    with env.ws() as ws:
        first = initialize(ws)["result"]["sessionId"]
        env.factory.instances[0].kill()
        msg = open_raw(ws, "a.vhd", FX01)
        assert code(msg) == -32603 and msg["error"]["data"]["category"] == "language_server_failure"
        assert code(open_raw(ws, "a.vhd", FX01)) == -32603              # still failed
        second = initialize(ws, id=3)["result"]["sessionId"]
        assert second != first and is_diagnostics(open_doc(ws, "a.vhd", FX01))


@pytest.mark.parametrize("from_failed", [False, True], ids=["TC-B-42_ready", "TC-B-43_failed"])
def test_state_shutdown_then_only_exit(env, from_failed):
    """ready/failed + shutdown -> shutdown; then everything but exit is
    refused (TC-B-44) and exit closes the connection (TC-B-45)."""
    with env.ws() as ws:
        initialize(ws)
        if from_failed:
            env.factory.instances[0].kill()
            open_raw(ws, "a.vhd", FX01)
        assert request(ws, "shutdown", None, id=4)["result"] is None
        assert code(request(ws, "initialize", {"capabilities": {}}, id=5)) == -32600
        assert code(open_raw(ws, "a.vhd", FX01)) == -32600
        notify(ws, "exit")
        assert ws.receive()["type"] == "websocket.close"
    assert env.wait_clean()


# =========================================================================
# Error guessing
# =========================================================================

@pytest.mark.parametrize("raw, expected", [
    ("hello", -32700),                                          # TC-B-46 not JSON
    ("[]", -32600),                                             # TC-B-47 JSON array (batch)
    ('{"jsonrpc": "1.0", "id": 1, "method": "initialize"}', -32600),  # TC-B-48 wrong version
    ('{"jsonrpc": "2.0", "id": 1, "method": "nope"}', -32601),  # TC-B-49 unknown method
    ('{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": "x"}', -32600),  # TC-B-50
], ids=["TC-B-46", "TC-B-47", "TC-B-48", "TC-B-49", "TC-B-50"])
def test_error_guessing_bad_messages(env, raw, expected):
    with env.ws() as ws:
        ws.send_text(raw)
        reply = ws.receive_json()
        assert code(reply) == expected and "Traceback" not in str(reply)
        assert "result" in initialize(ws)                       # the connection still works


def test_error_guessing_odd_file_contents_do_not_crash(env):
    """TC-B-51: empty text, a NUL byte, one very long line."""
    with env.ws() as ws:
        initialize(ws)
        assert is_diagnostics(open_raw(ws, "empty.vhd", ""))
        assert is_diagnostics(open_raw(ws, "nul.vhd", "entity \x00 is"))
        assert is_diagnostics(open_raw(ws, "long.vhd", "-- " + "x" * 150_000))


def test_error_guessing_client_vanishes_mid_session(env):
    """TC-B-52: environmental disruption: the socket drops without shutdown.
    The session folder and the VHDL-LS process must still be cleaned up."""
    ws = env.ws().__enter__()
    initialize(ws)
    open_doc(ws, "a.vhd", FX01)
    ws.__exit__(None, None, None)                               # no shutdown, no exit
    assert env.wait_clean()


@needs_vhdl_ls
@pytest.mark.parametrize("change", [
    lambda t: "\ufeff" + t,                     # TC-B-53 UTF-8 BOM (Windows Notepad)
    lambda t: t.replace("\n", "\r\n"),          # TC-B-54 Windows line endings
    lambda t: t.replace("  ", "\t"),            # TC-B-55 tabs
], ids=["TC-B-53_bom", "TC-B-54_crlf", "TC-B-55_tabs"])
def test_error_guessing_windows_style_files(change):
    """A correct file saved by a Windows editor must give no errors, and a
    wrong one must give the error on the same line as the plain version.
    TC-B-53 FAILED before the fix: VHDL-LS reported the BOM as an error."""
    with Env(real=True) as e, e.ws() as ws:
        initialize(ws)
        assert open_doc(ws, "ok.vhd", change(FX01))["params"]["summary"] == "No errors present."
    with Env(real=True) as e, e.ws() as ws:
        initialize(ws)
        plain = open_doc(ws, "bad.vhd", FX02)["params"]["diagnostics"]
    with Env(real=True) as e, e.ws() as ws:
        initialize(ws)
        changed = open_doc(ws, "bad.vhd", change(FX02))["params"]["diagnostics"]
    lines = lambda ds: [d["range"]["start"]["line"] for d in ds if d["severity"] == 1]
    assert lines(changed) == lines(plain)
