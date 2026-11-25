"""STD test cases for the P5B Gateway (Software Testing Description v1.0.0, Group 5).

Every test is named after its STD ID so the pytest output lines up with the
test log in Section 3.4. Each test saves what it saw to tests/evidence/<ID>.json.

    python -m pytest tests/test_std.py tests/test_std_joint.py -v -rA

* Tests without @needs_vhdl_ls use the fake bridge and run anywhere.
* Tests with @needs_vhdl_ls start the real vhdl_ls (on PATH or P5B_VHDL_LS_PATH);
  without it they are SKIPPED, not failed.

The joint cases (IT-05, IT-06, VT-02 joint) are in tests/test_std_joint.py.
The P5A cases are in the Group-5A repository's tests/test_std.py.
"""
import json
import platform
import time
from pathlib import Path

import pytest

from clients import checks
from conftest import (DESIGN, FIXTURES, Env, change_doc, completion, fx, initialize,
                      needs_vhdl_ls, notify, open_doc, request, uri)
from gateway.invariants import check_completion, check_outgoing, check_publish_diagnostics

EVIDENCE = Path(__file__).resolve().parent / "evidence"
FX01 = fx("fx01_valid.vhd")
FX02 = fx("fx02_missing_semicolon.vhd")
FX02B = fx("fx02b_corrected.vhd")
FXC1 = fx("fxc1_completion.vhd")
AFTER_SIG = (14, 11)       # just after "sig_" in fxc1_completion.vhd
IN_ENTITY_NAME = (3, 2)    # inside the word "entity": no signal prefix typed


def expected(name):
    return json.loads((FIXTURES / "expected" / name).read_text(encoding="utf-8"))


def save(test_id, data):
    EVIDENCE.mkdir(exist_ok=True)
    record = {"test_id": test_id, "platform": platform.platform(),
              "time": time.strftime("%Y-%m-%d %H:%M:%S"), "observed": data}
    (EVIDENCE / f"{test_id}.json").write_text(json.dumps(record, indent=2, default=str),
                                              encoding="utf-8")


def is_contract_error(msg, code=None):
    """An error reply in the HDL Service Contract v1 shape."""
    ok = (checks.is_error(msg) and msg["error"]["data"]["status"] == "failed"
          and msg["error"]["data"]["stage"] == "language_service"
          and bool(msg["error"]["message"]) and "Traceback" not in json.dumps(msg))
    return ok and (code is None or msg["error"]["code"] == code)


# =========================================================== 3.2.1 Unit tests

UT08_CASES = {
    "not_json": ("text", "this is not json", -32700),
    "cut_off_json": ("text", '{"jsonrpc":"2.0","id":3,"method":', -32700),
    "missing_method": ("json", {"jsonrpc": "2.0", "id": 7, "params": {}}, None),
    "hover_unsupported": ("request", "textDocument/hover", -32601),
    "rename_unsupported": ("request", "textDocument/rename", -32601),
    "definition_unsupported": ("request", "textDocument/definition", -32601),
    "file_outside_set": ("open", "../secret.txt", -32602),
}


@pytest.mark.parametrize("case", UT08_CASES)
def test_UT08_malformed_and_unsupported_rejected(case):
    """FR-P5B-04, DC-P5B-02: a structured error, nothing sent to VHDL-LS,
    and the session still works afterwards."""
    kind, payload, code = UT08_CASES[case]
    with Env() as e, e.ws() as ws:
        initialize(ws)
        if kind == "text":
            ws.send_text(payload)
            reply = ws.receive_json()
        elif kind == "json":
            ws.send_json(payload)
            reply = ws.receive_json()
        elif kind == "request":
            reply = request(ws, payload, {"textDocument": {"uri": uri(DESIGN)},
                                          "position": {"line": 0, "character": 0}}, id=2)
        else:
            notify(ws, "textDocument/didOpen", {"textDocument": {"uri": f"file:///{payload}",
                                                                 "text": FX01}})
            reply = ws.receive_json()
        forwarded = dict(e.factory.instances[0].texts)
        after = open_doc(ws, DESIGN, FX01)          # a valid message straight after
        save(f"UT-08_{case}", {"sent": payload, "reply": reply,
                               "files_vhdl_ls_received": list(forwarded),
                               "next_valid_message": after["method"]})
        assert is_contract_error(reply, code), reply
        assert forwarded == {}
        assert after["method"] == "textDocument/publishDiagnostics"


def test_UT09_session_opened():
    """FR-P5B-03, FR-P5B-07: session folder in the shared layout + VHDL-LS started."""
    with Env() as e, e.ws() as ws:
        reply = initialize(ws)
        (sid,) = e.sessions()
        folder = e.root / "sessions" / sid
        layout = sorted(p.name for p in folder.iterdir())
        save("UT-09", {"reply": reply, "session_folder_contents": layout,
                       "vhdl_ls_processes": len(e.factory.instances)})
        assert checks.is_success(reply) and reply["result"]["sessionId"] == sid
        assert (folder / "src").is_dir() and (folder / "vhdl_ls.toml").is_file()
        assert len(e.factory.instances) == 1 and e.factory.instances[0].is_alive


@pytest.mark.parametrize("how", ["explicit_shutdown", "client_disconnects"])
def test_UT10_session_closed_and_cleaned_up(how):
    """UC-B06: no VHDL-LS process and no files left once a session ends."""
    with Env() as e:
        with e.ws() as ws:
            initialize(ws)
            open_doc(ws, DESIGN, FX01)
            during = e.sessions()
            if how == "explicit_shutdown":
                assert request(ws, "shutdown", id=2)["result"] is None
                clean = e.wait_clean()
                notify(ws, "exit")
        if how == "client_disconnects":
            clean = e.wait_clean()
        save(f"UT-10_{how}", {"sessions_during": during, "sessions_after": e.sessions(),
                              "vhdl_ls_closed": [b.closed for b in e.factory.instances]})
        assert during and clean
        assert e.sessions() == [] and all(b.closed for b in e.factory.instances)


def test_UT11_diagnostics_returned():
    """FR-P5B-01: an error on the right line for a broken file, [] for a valid one."""
    with Env() as e, e.ws() as ws:
        initialize(ws)
        bad = open_doc(ws, DESIGN, FX02)["params"]
        good = open_doc(ws, "valid.vhd", FX01)["params"]
        save("UT-11", {"missing_semicolon": bad, "valid": good})
        (d,) = bad["diagnostics"]
        assert d["severity"] == 1 and d["range"]["start"]["line"] == 12
        assert good["diagnostics"] == [] and good["summary"] == "No errors present."


@needs_vhdl_ls
def test_UT11_diagnostics_real_vhdl_ls():
    # Each fixture in its own session: both declare entity and_gate, so opening
    # them together would (correctly) give VHDL-LS "duplicate unit" errors.
    with Env(real=True) as e:
        with e.ws() as ws:
            initialize(ws)
            bad = open_doc(ws, DESIGN, FX02)["params"]["diagnostics"]
        with e.ws() as ws:
            initialize(ws)
            good = open_doc(ws, DESIGN, FX01)["params"]["diagnostics"]
        problems = (checks.check_diagnostics(bad, expected("fx02_missing_semicolon.expected.json"))
                    + checks.check_diagnostics(good, expected("fx01_valid.expected.json")))
        save("UT-11_real_vhdl_ls", {"missing_semicolon": bad, "valid": good, "problems": problems})
        assert problems == []


def test_UT12_diagnostics_follow_edits():
    """UC-B02: diagnostics match the current content after a change."""
    with Env() as e, e.ws() as ws:
        initialize(ws)
        before = open_doc(ws, DESIGN, FX02)["params"]
        after = change_doc(ws, DESIGN, FX02B, 2)["params"]
        save("UT-12", {"before_fix": before, "after_fix": after})
        assert before["diagnostics"]
        assert after["diagnostics"] == [] and after["version"] == 2


def test_UT13_code_completion_returned():
    """FR-P5B-02: items after 'sig_', an empty list where nothing fits, session unchanged."""
    with Env() as e, e.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, FXC1)
        sid_before = e.sessions()
        hit = completion(ws, DESIGN, *AFTER_SIG, id=5)
        miss = completion(ws, DESIGN, *IN_ENTITY_NAME, id=6)
        save("UT-13", {"after_sig_": hit, "no_prefix": miss})
        assert checks.check_completion(hit["result"], expected("fxc1_completion.expected.json")) == []
        assert miss["result"]["items"] == []
        assert e.sessions() == sid_before


@needs_vhdl_ls
def test_UT13_completion_real_vhdl_ls():
    with Env(real=True) as e, e.ws() as ws:
        initialize(ws)
        open_doc(ws, "fxc1_completion.vhd", FXC1)
        hit = completion(ws, "fxc1_completion.vhd", *AFTER_SIG, id=5)
        labels = [i["label"] for i in checks.completion_items(hit["result"])]
        save("UT-13_real_vhdl_ls", {"labels": labels[:50]})
        assert checks.check_completion(hit["result"], expected("fxc1_completion.expected.json")) == []


def _scenario():
    out = []
    with Env() as e, e.ws() as ws:
        out.append(initialize(ws))
        out.append(open_doc(ws, DESIGN, FX02))
        out.append(change_doc(ws, DESIGN, FX02B, 2))
        open_doc(ws, "c.vhd", FXC1)
        out.append(completion(ws, "c.vhd", *AFTER_SIG, 3))
        out.append(request(ws, "textDocument/hover", id=4))
    out[0]["result"]["sessionId"] = "<id>"
    return out


def test_UT14_independent_and_deterministic():
    """FR-P5B-06: the Gateway alone (no P5A, no Project 6) gives identical
    replies on two runs over every fixture type."""
    import sys
    first, second = _scenario(), _scenario()
    save("UT-14", {"run_1": first, "identical_on_run_2": first == second,
                   "p5a_imported": "app.docker_runner" in sys.modules})
    assert first == second


# ===================================================== 3.2.3 Validation tests

def test_VT03_editing_workflow():
    """UC-B01..B03, UC-B06: a realistic edit session from open to close."""
    log = []
    with Env() as e:
        with e.ws() as ws:
            log.append(("open session", initialize(ws)))
            log.append(("open valid file", open_doc(ws, DESIGN, FX01)["params"]["summary"]))
            log.append(("introduce error", change_doc(ws, DESIGN, FX02, 2)["params"]["summary"]))
            log.append(("fix error", change_doc(ws, DESIGN, FX02B, 3)["params"]["summary"]))
            open_doc(ws, "c.vhd", FXC1)
            items = checks.completion_items(completion(ws, "c.vhd", *AFTER_SIG, id=7)["result"])
            log.append(("completion after sig_", sorted(i["label"] for i in items)))
            log.append(("close", request(ws, "shutdown", id=8)))
            clean = e.wait_clean()
            notify(ws, "exit")
        save("VT-03", {"steps": log, "nothing_left_after_close": clean})
        assert [s for _, s in log[1:4]] == ["No errors present.", "1 error found.",
                                            "No errors present."]
        assert log[4][1] == ["sig_clock", "sig_reset"]
        assert clean


def test_VT05_contract_conformance():
    """Every message from the Gateway passes the contract checks in gateway/invariants.py."""
    replies = _scenario()
    with Env() as e, e.ws() as ws:
        ws.send_text("not json")
        replies.append(ws.receive_json())
    problems = []
    for msg in replies:
        try:
            check_outgoing(msg)
            if msg.get("method") == "textDocument/publishDiagnostics":
                check_publish_diagnostics(msg["params"])
            if "result" in msg and isinstance(msg["result"], dict) and "items" in msg["result"]:
                check_completion(msg["result"])
        except Exception as exc:          # noqa: BLE001 - recorded as evidence
            problems.append(f"{type(exc).__name__}: {exc}")
    save("VT-05_p5b", {"messages_checked": len(replies), "violations": problems})
    assert problems == []


# ===================================================== 3.2.4 High-order tests

def test_HT02_concurrent_sessions_kept_apart():
    """Five sessions at once: separate folders, separate VHDL-LS, no cross-talk."""
    with Env() as e:
        conns = [e.ws() for _ in range(5)]
        sockets = [c.__enter__() for c in conns]
        try:
            for ws in sockets:
                initialize(ws)
            results = [open_doc(ws, DESIGN, FX02 if i % 2 else FX01)["params"]["summary"]
                       for i, ws in enumerate(sockets)]
            sessions = e.sessions()
        finally:
            for c in conns:
                c.__exit__(None, None, None)
        save("HT-02_sessions", {"sessions": len(sessions), "summaries": results,
                                "cleaned_up": e.wait_clean()})
        assert len(sessions) == 5 and len(e.factory.instances) == 5
        assert results == ["No errors present.", "1 error found."] * 2 + ["No errors present."]


def test_HT02_session_limit_and_big_message():
    with Env(max_sessions=2) as e, e.ws() as a, e.ws() as b, e.ws() as c:
        initialize(a)
        initialize(b)
        third = initialize(c)
        too_big = None
        with Env(max_message_bytes=500) as e2, e2.ws() as ws:
            ws.send_text('{"jsonrpc":"2.0","id":1,"method":"x","params":{"a":"' + "y" * 600 + '"}}')
            too_big = ws.receive_json()
        save("HT-02_limits", {"third_session_over_limit": third, "oversized_message": too_big})
        assert is_contract_error(third) and is_contract_error(too_big, -32600)


def test_HT03_vhdl_ls_crash_reported_others_unaffected():
    """FR-P5B-05, UC-B05: one VHDL-LS dies; its client is told, the other session carries on."""
    with Env() as e, e.ws() as a, e.ws() as b:
        initialize(a)
        initialize(b)
        e.factory.instances[0].kill()
        notify(a, "textDocument/didOpen", {"textDocument": {"uri": uri(DESIGN), "text": FX01}})
        error = a.receive_json()
        other = open_doc(b, DESIGN, FX01)["params"]
        restart = initialize(a, id=9)
        save("HT-03_vhdl_ls_crash", {"error_to_affected_client": error,
                                     "other_session": other, "affected_client_restarts": restart})
        assert is_contract_error(error, -32603)
        assert error["error"]["data"]["category"] == "language_server_failure"
        assert other["diagnostics"] == []
        assert checks.is_success(restart)


@needs_vhdl_ls
def test_HT03_real_vhdl_ls_killed():
    with Env(real=True) as e:
        with e.ws() as ws:
            initialize(ws)
            open_doc(ws, DESIGN, FX01)
            session = next(iter(e.manager._sessions.values()))
            session.bridge._process._proc.kill()
            session.bridge._process._proc.wait()
            notify(ws, "textDocument/didChange", {"textDocument": {"uri": uri(DESIGN)},
                                                  "contentChanges": [{"text": FX01}]})
            reply = ws.receive_json()
        save("HT-03_real_vhdl_ls_killed", {"reply": reply, "cleaned_up": e.wait_clean()})
        assert is_contract_error(reply, -32603)
        assert "terminated" in reply["error"]["message"]
