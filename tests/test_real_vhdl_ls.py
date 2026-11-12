"""The system-test cases with the REAL vhdl_ls (skipped if it is not installed).

Each test runs the Gateway in this process with a real vhdl_ls per session
and compares its replies with tests/vhdl/expected/*.expected.json.
"""
import asyncio
import json

import pytest

from clients import checks
from conftest import (DESIGN, FIXTURES, Env, change_doc, completion, fx, initialize, needs_vhdl_ls,
                      notify, open_doc, request, uri)

pytestmark = needs_vhdl_ls

TESTBENCH = """\
library ieee;
use ieee.std_logic_1164.all;

entity and_gate_tb is
end entity and_gate_tb;

architecture sim of and_gate_tb is
  signal a, b, y : std_logic := '0';
begin
  uut: entity work.and_gate port map (a => a, b => b, y => y);

  process
  begin
    a <= '1';
    b <= '1';
    wait for 10 ns;
    assert y = '1' report "1 and 1 failed" severity error;
    wait;
  end process;
end architecture sim;
"""


def expected(stem):
    return json.loads((FIXTURES / "expected" / f"{stem}.expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def real():
    with Env(real=True) as e:
        yield e


def test_valid_design(real):
    with real.ws() as ws:
        initialize(ws)
        params = open_doc(ws, DESIGN, fx("fx01_valid.vhd"))["params"]
        assert checks.check_diagnostics(params["diagnostics"], expected("fx01_valid")) == []
        assert params["summary"] == "No errors present."


def test_missing_semicolon(real):
    with real.ws() as ws:
        initialize(ws)
        params = open_doc(ws, DESIGN, fx("fx02_missing_semicolon.vhd"))["params"]
        assert checks.check_diagnostics(params["diagnostics"],
                                        expected("fx02_missing_semicolon")) == [], params
        assert params["summary"].endswith("found.") and "error" in params["summary"]


def test_change_with_the_fix_clears_the_error(real):
    with real.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, fx("fx02_missing_semicolon.vhd"))
        params = change_doc(ws, DESIGN, fx("fx02b_corrected.vhd"), 2)["params"]
        assert checks.check_diagnostics(params["diagnostics"], expected("fx02b_corrected")) == []
        assert params["summary"] == "No errors present."


def test_completion_after_sig(real):
    exp = expected("fxc1_completion")
    with real.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, fx("fxc1_completion.vhd"))
        reply = completion(ws, DESIGN, exp["position"]["line"], exp["position"]["character"])
        assert checks.is_success(reply)
        assert checks.check_completion(reply["result"], exp) == []


def test_file_is_stored_and_used_by_vhdl_ls(real):
    with real.ws() as ws:
        initialize(ws)
        params = open_doc(ws, DESIGN, fx("fx01_valid.vhd"))["params"]
        (sid,) = real.sessions()
        assert real.store.list_files(sid) == [DESIGN]
        assert real.store.read_file(sid, DESIGN) == fx("fx01_valid.vhd")
        assert params["diagnostics"] == []


@pytest.mark.parametrize("method", ["textDocument/hover", "textDocument/definition",
                                    "textDocument/rename", "textDocument/formatting"])
def test_unapproved_requests_never_reach_vhdl_ls(real, method):
    with real.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, fx("fx01_valid.vhd"))
        reply = request(ws, method, {"textDocument": {"uri": uri(DESIGN)},
                                     "position": {"line": 0, "character": 0}}, id=3)
        assert checks.check_error(reply, -32601, method) == []
        assert "result" not in reply


def test_invalid_message_then_valid_request(real):
    with real.ws() as ws:
        initialize(ws)
        ws.send_text("{ not json")
        assert checks.check_error(ws.receive_json(), -32700) == []
        assert open_doc(ws, DESIGN, fx("fx01_valid.vhd"))["params"]["diagnostics"] == []


def test_design_and_testbench_in_one_session(real):
    """The testbench is sent first, so it cannot find work.and_gate yet. When the
    entity file is opened, the Gateway sends the testbench's diagnostics again,
    now empty, so the editor never keeps a stale error."""
    with real.ws() as ws:
        initialize(ws)
        tb = open_doc(ws, "and_gate_tb.vhd", TESTBENCH)["params"]
        assert tb["uri"] == "file:///and_gate_tb.vhd"
        assert any("and_gate" in d["message"] for d in tb["diagnostics"]), tb
        entity = open_doc(ws, "and_gate.vhd", fx("fx01_valid.vhd"))["params"]
        assert entity["uri"] == "file:///and_gate.vhd" and entity["diagnostics"] == []
        again = ws.receive_json()["params"]
        assert again["uri"] == "file:///and_gate_tb.vhd"
        assert again["diagnostics"] == [] and again["summary"] == "No errors present."


def test_vhdl_ls_killed_mid_session_is_reported(real):
    with real.ws() as ws:
        initialize(ws)
        open_doc(ws, DESIGN, fx("fx01_valid.vhd"))
        session = next(iter(real.manager._sessions.values()))
        session.bridge._process._proc.kill()
        session.bridge._process._proc.wait()
        notify(ws, "textDocument/didChange", {"textDocument": {"uri": uri(DESIGN)},
                                              "contentChanges": [{"text": fx("fx01_valid.vhd")}]})
        error = ws.receive_json()
        assert checks.check_error(error, -32603) == []
        assert "terminated" in error["error"]["message"]
    assert real.wait_clean()


def test_check_files_with_design_and_testbench(real):
    async def go():
        return await real.manager.check_files([
            {"name": "and_gate_tb.vhd", "content": TESTBENCH},
            {"name": "and_gate.vhd", "content": fx("fx01_valid.vhd")}])
    results = asyncio.run(go())
    assert [r["diagnostics"] for r in results] == [[], []]
    assert real.wait_clean()


def test_two_runs_give_identical_replies():
    def scenario():
        with Env(real=True) as e, e.ws() as ws:
            initialize(ws)
            replies = [open_doc(ws, DESIGN, fx("fx02_missing_semicolon.vhd")),
                       change_doc(ws, DESIGN, fx("fx02b_corrected.vhd"), 2),
                       request(ws, "textDocument/hover", id=4)]
            open_doc(ws, "other.vhd", fx("fxc1_completion.vhd"))
            replies.append(completion(ws, "other.vhd", 14, 11, 3))
            return replies
    assert scenario() == scenario()
