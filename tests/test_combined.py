"""P5A and P5B in one backend (parent_main.py): FR-P5-Joint-01, P5-AC4.1, and the
team's integration tests FR-P5-01 and FR-P5-02.

Uses P5A's real code from the Group-5A repository (P5A_DIR, or a Group-5A /
Group-5A-main folder next to this repo). The whole file is SKIPPED if P5A is
not found. Tests that need Docker (with P5A's p5a-ghdl image) or the real
vhdl_ls skip themselves when those are missing.
"""
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from conftest import initialize, needs_vhdl_ls, open_doc
from gateway.config import Config
from gateway.router import WS_PATH
from gateway.session import SessionManager

try:
    import parent_main
except RuntimeError as exc:  # P5A not found
    pytest.skip(str(exc), allow_module_level=True)

from app import config as p5a_config  # noqa: E402  (P5A)
from app import docker_runner  # noqa: E402  (P5A)

P5A_FIXTURES = parent_main.P5A_DIR / "tests" / "ghdl"   # P5A's reference designs
DESIGN_FILES = ["and_gate.vhd", "and_gate_tb.vhd"]
TOP = "and_gate_tb"
RESULT_KEYS = {"job_id", "status", "stage", "error", "message", "stages", "vcd"}


def ref(name):
    return (P5A_FIXTURES / name).read_text(encoding="utf-8")


def ref_files(names=DESIGN_FILES):
    return [{"name": n, "content": ref(n)} for n in names]


def _docker_ready():
    try:
        import docker
        client = docker.from_env()
        client.ping()
        client.images.get(p5a_config.GHDL_IMAGE)
        return True
    except Exception:
        return False


needs_docker = pytest.mark.skipif(not _docker_ready(),
                                  reason="Docker not running or the p5a-ghdl image is not built")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """parent_main's app, with P5B on the fake bridge. P5A's start-up clean-up is
    replaced, as in P5A's own tests."""
    monkeypatch.setattr(docker_runner, "cleanup_stale_jobs", lambda: (0, 0))
    manager = SessionManager(replace(Config(), bridge="fake", session_root=str(tmp_path),
                                     watchdog_interval_s=60.0))
    monkeypatch.setattr(parent_main.p5b_router, "manager", manager)
    with TestClient(parent_main.app) as c:
        yield c


@pytest.fixture
def real_client(tmp_path, monkeypatch):
    """The same app, but P5B starts the real vhdl_ls."""
    monkeypatch.setattr(docker_runner, "cleanup_stale_jobs", lambda: (0, 0))
    manager = SessionManager(replace(Config(), session_root=str(tmp_path),
                                     watchdog_interval_s=60.0))
    monkeypatch.setattr(parent_main.p5b_router, "manager", manager)
    with TestClient(parent_main.app) as c:
        yield c


def fake_run_job(monkeypatch, status="success"):
    calls = []

    def run_job(files, top, stop_time=None):
        calls.append({"files": files, "top": top, "stop_time": stop_time})
        return {"job_id": "abc123", "status": status, "stage": "done", "error": None,
                "message": "Simulation finished.", "stages": [], "vcd": b"$date today $end"}

    monkeypatch.setattr(docker_runner, "run_job", run_job)
    return calls


def session_request(client, names=DESIGN_FILES, top=TOP, stop_time="100ns"):
    """Open the files in a P5B session, then build P5A's request from the same session."""
    with client.websocket_connect(WS_PATH) as ws:
        initialize(ws)
        for name in names:
            msg = open_doc(ws, name, ref(name))
            assert msg["method"] == "textDocument/publishDiagnostics"
        manager = parent_main.p5b_router.get_manager()
        (session_id,) = manager._sessions
        return manager.p5a_request(session_id, top, stop_time)


# ---- one backend, both services --------------------------------------------------------------

def test_one_app_serves_both_services(client):
    assert client.post("/simulate", json={"top": "x"}).status_code == 400       # P5A
    assert client.get("/language-service/health").json()["status"] == "ok"      # P5B
    with client.websocket_connect(WS_PATH) as ws:                               # P5B
        assert "capabilities" in initialize(ws)["result"]


def test_p5a_behaves_the_same_in_the_combined_app(client, monkeypatch):
    monkeypatch.setattr(docker_runner, "docker_status", lambda: {"docker": "ok", "image": "present"})
    assert client.get("/health").json() == {"status": "ok", "docker": "ok", "image": "present"}
    assert "running" in client.get("/").json()["message"]
    r = client.post("/simulate", json={"top": "x"})          # P5A's own error handler still answers
    assert r.status_code == 400 and set(r.json()) == RESULT_KEYS
    assert r.json()["status"] == "failed" and r.json()["error"] == "validation_error"


# ---- the same files through both services (FR-P5B-07, UC-B07) ---------------------------------

def test_session_files_pass_p5as_own_validator(client):
    body = session_request(client)
    assert [f["name"] for f in body["files"]] == sorted(DESIGN_FILES)
    assert body["top"] == TOP and body["stop_time"] == "100ns"
    docker_runner.validate_request(body["files"], body["top"], body["stop_time"])  # raises if not


def test_session_files_run_through_p5a_simulate(client, monkeypatch):
    calls = fake_run_job(monkeypatch)
    r = client.post("/simulate", json=session_request(client))
    assert r.status_code == 200
    assert r.json()["status"] == "success" and r.json()["vcd"] == "$date today $end"
    assert calls[0]["top"] == TOP and calls[0]["stop_time"] == "100ns"
    assert {f.name: f.content for f in calls[0]["files"]} == {n: ref(n) for n in DESIGN_FILES}


def test_p5b_keeps_working_when_p5a_has_no_docker(client, monkeypatch):
    monkeypatch.setattr(docker_runner, "run_job", lambda files, top, stop_time=None: {
        "job_id": "abc123", "status": "failed", "stage": "analysis", "error": "internal_error",
        "message": "Docker error", "stages": [], "vcd": None})
    r = client.post("/simulate", json={"files": ref_files(["and_gate.vhd"]), "top": "and_gate"})
    assert r.status_code == 503 and r.json()["error"] == "internal_error"
    with client.websocket_connect(WS_PATH) as ws:
        initialize(ws)
        assert open_doc(ws, "and_gate.vhd", ref("and_gate.vhd"))["params"]["diagnostics"] == []


# ---- POST /check-and-simulate (FR-P5-01, FR-P5-02) ---------------------------------------------

def test_check_and_simulate_syntax_error_creates_no_job(client, monkeypatch):
    calls = fake_run_job(monkeypatch)
    files = [{"name": "and_gate.vhd", "content": ref("and_gate.vhd").replace("A and B;", "A and B")}]
    assert files[0]["content"] != ref("and_gate.vhd")
    r = client.post("/check-and-simulate", json={"files": files, "top": "and_gate"})
    assert r.status_code == 200
    result = r.json()
    assert calls == []                                            # no GHDL job was created
    assert result["status"] == "failed" and result["error"] == "analysis_error"
    assert result["stage"] == "language_service" and result["vcd"] is None
    assert result["job_id"] is None and "no GHDL job" in result["message"]
    (checked,) = result["diagnostics"]
    assert checked["uri"] == "file:///and_gate.vhd" and checked["diagnostics"][0]["severity"] == 1


def test_check_and_simulate_clean_files_run_on_p5a(client, monkeypatch):
    calls = fake_run_job(monkeypatch)
    r = client.post("/check-and-simulate", json={"files": ref_files(), "top": TOP,
                                                 "stop_time": "100ns"})
    assert r.status_code == 200
    result = r.json()
    assert len(calls) == 1 and calls[0]["top"] == TOP
    assert result["status"] == "success" and result["vcd"] == "$date today $end"
    assert [c["diagnostics"] for c in result["diagnostics"]] == [[], []]
    assert {c["summary"] for c in result["diagnostics"]} == {"No errors present."}


def test_check_and_simulate_bad_request_gets_p5as_validation_error(client):
    # P5A's real run_job rejects this before Docker is used, and P5B never opens a session.
    r = client.post("/check-and-simulate", json={"files": [{"name": "../x.vhd", "content": "x"}],
                                                 "top": "x"})
    assert r.status_code == 400 and r.json()["error"] == "validation_error"
    assert set(r.json()) == RESULT_KEYS
    assert parent_main.p5b_router.get_manager()._factory().instances == []


def test_check_and_simulate_without_vhdl_ls_is_a_service_error(client, monkeypatch):
    calls = fake_run_job(monkeypatch)
    parent_main.p5b_router.get_manager()._factory().fail_start = True
    r = client.post("/check-and-simulate", json={"files": ref_files(), "top": TOP})
    assert r.status_code == 503 and r.json()["error"] == "internal_error"
    assert r.json()["stage"] == "language_service" and calls == []


# ---- with real GHDL and the real vhdl_ls ------------------------------------------------------

@needs_docker
def test_real_ghdl_job_on_the_session_files(client):
    body = session_request(client)
    result = docker_runner.run_job(files=body["files"], top=body["top"], stop_time=body["stop_time"])
    assert result["status"] == "success", result.get("message")
    assert result["vcd"]


@needs_docker
@needs_vhdl_ls
def test_syntax_error_is_found_by_both_services_on_the_same_line(real_client):
    """GHDL counts lines from 1 and LSP from 0; VHDL-LS may point at the next token."""
    text = ref("invalid_syntax.vhd")
    with real_client.websocket_connect(WS_PATH) as ws:
        initialize(ws)
        diagnostics = open_doc(ws, "invalid_syntax.vhd", text)["params"]["diagnostics"]
    errors = [d for d in diagnostics if d["severity"] == 1]
    assert errors, "P5B found no error in a file with a missing semicolon"

    result = docker_runner.run_job(files=[{"name": "invalid_syntax.vhd", "content": text}],
                                   top="invalid_syntax")
    assert result["status"] == "failed" and result["error"] == "analysis_error"
    output = " ".join(stage["output"] for stage in result["stages"])
    found = re.search(r"invalid_syntax\.vhd:(\d+):(\d+)", output)
    assert found, output
    ghdl_line = int(found.group(1)) - 1
    assert {d["range"]["start"]["line"] for d in errors} & {ghdl_line, ghdl_line + 1}


@needs_docker
@needs_vhdl_ls
def test_fr_p5_01_syntax_error_end_to_end(real_client):
    """FR-P5-01: the Gateway reports the syntax error, so no GHDL job is created."""
    text = ref("invalid_syntax.vhd")
    r = real_client.post("/check-and-simulate",
                         json={"files": [{"name": "invalid_syntax.vhd", "content": text}],
                               "top": "invalid_syntax"})
    result = r.json()
    assert r.status_code == 200 and result["status"] == "failed"
    assert result["stage"] == "language_service" and result["job_id"] is None
    assert result["diagnostics"][0]["diagnostics"][0]["severity"] == 1


@needs_docker
@needs_vhdl_ls
def test_fr_p5_02_valid_design_end_to_end(real_client):
    """FR-P5-02: no diagnostics from the Gateway, and P5A returns success with a VCD."""
    r = real_client.post("/check-and-simulate",
                         json={"files": ref_files(), "top": TOP, "stop_time": "100ns"})
    result = r.json()
    assert r.status_code == 200 and result["status"] == "success", result.get("message")
    assert result["vcd"] and result["vcd"].startswith("$")
    assert [c["summary"] for c in result["diagnostics"]] == ["No errors present."] * 2


STD2008_ONLY = """\
entity std08 is
  port (a : in bit; y : out bit);
end entity std08;

architecture rtl of std08 is
begin
  process (all) begin
    y <= a;
  end process;
end architecture rtl;
"""


@needs_docker
@needs_vhdl_ls
def test_vhdl_2008_code_gets_the_same_verdict_from_both(real_client):
    """process (all) is VHDL-2008 only. Both services must agree; parent_main
    sets P5A's GHDL to 2008 unless P5A_GHDL_STD says otherwise."""
    if p5a_config.GHDL_STD not in ("08", "2008"):
        pytest.skip("P5A_GHDL_STD is set to another standard")
    with real_client.websocket_connect(WS_PATH) as ws:
        initialize(ws)
        diagnostics = open_doc(ws, "std08.vhd", STD2008_ONLY)["params"]["diagnostics"]
    assert [d for d in diagnostics if d["severity"] == 1] == []
    result = docker_runner.run_job(files=[{"name": "std08.vhd", "content": STD2008_ONLY}],
                                   top="std08")
    assert result.get("error") != "analysis_error", result.get("message")

