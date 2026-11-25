"""STD joint test cases: P5A + P5B in one backend (parent_main.py).

Needs the Group-5A repository next to this one (or P5A_DIR); otherwise the
whole file is SKIPPED. Tests marked needs_docker / needs_vhdl_ls skip when
Docker (with the p5a-ghdl image) or vhdl_ls is missing.

    python -m pytest tests/test_std_joint.py -v -rA
"""
import re

import pytest

from conftest import initialize, needs_vhdl_ls, open_doc
from gateway.invariants import check_combined_result
from test_std import save
from test_combined import (DESIGN_FILES, RESULT_KEYS, TOP, client, fake_run_job,  # noqa: F401
                           needs_docker, real_client, ref, ref_files, session_request)
from gateway.router import WS_PATH

import parent_main  # noqa: E402  (test_combined already skipped us if P5A is missing)
from app import docker_runner  # noqa: E402  (P5A)

BROKEN_AND_GATE = [{"name": "and_gate.vhd",
                    "content": ref("and_gate.vhd").replace("A and B;", "A and B")}]


# ==================================================== 3.2.2 Integration tests

def test_IT05_p5b_file_set_runs_on_p5a(client, monkeypatch):
    """FR-P5B-07, FR-P5-Joint-01, FR-P5-Joint-03: the files of a P5B session go
    to P5A's /simulate unchanged, in one backend."""
    calls = fake_run_job(monkeypatch)
    body = session_request(client)
    r = client.post("/simulate", json=body)
    sent = {f.name: f.content for f in calls[0]["files"]}
    save("IT-05", {"request_built_from_session": {"files": [f["name"] for f in body["files"]],
                                                  "top": body["top"],
                                                  "stop_time": body["stop_time"]},
                   "p5a_http": r.status_code, "p5a_status": r.json()["status"],
                   "files_identical_to_originals": sent == {n: ref(n) for n in DESIGN_FILES},
                   "p5a_health": client.get("/health").status_code,
                   "p5b_health": client.get("/language-service/health").json()})
    docker_runner.validate_request(body["files"], body["top"], body["stop_time"])
    assert r.status_code == 200 and r.json()["status"] == "success"
    assert sent == {n: ref(n) for n in DESIGN_FILES}


@needs_docker
def test_IT05_real_ghdl_on_session_files(client):
    body = session_request(client)
    result = docker_runner.run_job(files=body["files"], top=body["top"],
                                   stop_time=body["stop_time"])
    save("IT-05_real_ghdl", {"status": result["status"], "message": result["message"],
                             "stages": [(s["stage"], s["exit_code"]) for s in result["stages"]]})
    assert result["status"] == "success" and result["vcd"]


def test_IT06_clean_files_go_to_p5a(client, monkeypatch):
    """FR-P5-Joint-06/07: VHDL-LS checks first; clean files are forwarded to P5A."""
    calls = fake_run_job(monkeypatch)
    r = client.post("/check-and-simulate", json={"files": ref_files(), "top": TOP,
                                                 "stop_time": "100ns"})
    res = r.json()
    save("IT-06_clean", {"http": r.status_code, "result": res, "p5a_called": len(calls)})
    assert r.status_code == 200 and res["status"] == "success" and len(calls) == 1
    assert [c["diagnostics"] for c in res["diagnostics"]] == [[], []]


def test_IT06_diagnostics_errors_block_p5a(client, monkeypatch):
    """FR-P5-Joint-05: errors from VHDL-LS mean no GHDL job at all."""
    calls = fake_run_job(monkeypatch)
    r = client.post("/check-and-simulate", json={"files": BROKEN_AND_GATE, "top": "and_gate"})
    res = r.json()
    save("IT-06_syntax_error", {"http": r.status_code, "result": res, "p5a_called": len(calls)})
    assert calls == []
    assert (res["status"], res["stage"], res["error"], res["job_id"]) == \
           ("failed", "language_service", "analysis_error", None)
    assert res["diagnostics"][0]["diagnostics"][0]["severity"] == 1


def test_IT06_vhdl_ls_unavailable_is_503(client, monkeypatch):
    """FR-P5-Joint-04: VHDL-LS cannot start -> HTTP 503, internal_error, no job."""
    calls = fake_run_job(monkeypatch)
    parent_main.p5b_router.get_manager()._factory().fail_start = True
    r = client.post("/check-and-simulate", json={"files": ref_files(), "top": TOP})
    save("IT-06_vhdl_ls_down", {"http": r.status_code, "result": r.json(),
                                "p5a_called": len(calls)})
    assert r.status_code == 503 and r.json()["error"] == "internal_error" and calls == []


@needs_docker
@needs_vhdl_ls
@pytest.mark.parametrize("case", ["clean", "syntax_error"])
def test_IT06_real_end_to_end(real_client, case):
    files = ref_files() if case == "clean" else [{"name": "invalid_syntax.vhd",
                                                  "content": ref("invalid_syntax.vhd")}]
    top = TOP if case == "clean" else "invalid_syntax"
    r = real_client.post("/check-and-simulate", json={"files": files, "top": top,
                                                      "stop_time": "100ns"})
    res = r.json()
    save(f"IT-06_real_{case}", {"http": r.status_code, "status": res["status"],
                                "stage": res["stage"], "error": res["error"],
                                "job_id": res["job_id"], "diagnostics": res["diagnostics"]})
    if case == "clean":
        assert res["status"] == "success" and res["vcd"]
    else:
        assert res["stage"] == "language_service" and res["job_id"] is None


# ===================================================== 3.2.3 Validation tests

@needs_docker
@needs_vhdl_ls
def test_VT02_same_line_from_p5a_and_p5b(real_client):
    """FR-P5-Joint-02: a syntax error is an analysis error in P5A and an error
    diagnostic in P5B, on the same line (GHDL counts from 1, LSP from 0;
    VHDL-LS may point at the next token)."""
    text = ref("invalid_syntax.vhd")
    with real_client.websocket_connect(WS_PATH) as ws:
        initialize(ws)
        diags = open_doc(ws, "invalid_syntax.vhd", text)["params"]["diagnostics"]
    errors = [d for d in diags if d["severity"] == 1]
    result = docker_runner.run_job(files=[{"name": "invalid_syntax.vhd", "content": text}],
                                   top="invalid_syntax")
    output = " ".join(s["output"] for s in result["stages"])
    found = re.search(r"invalid_syntax\.vhd:(\d+):(\d+)", output)
    p5b_lines = sorted({d["range"]["start"]["line"] + 1 for d in errors})
    save("VT-02_joint", {"p5a_error": result["error"],
                         "p5a_line": int(found.group(1)) if found else None,
                         "p5b_lines_1_based": p5b_lines, "p5b_errors": errors})
    assert result["error"] == "analysis_error" and errors and found
    ghdl_line = int(found.group(1))
    assert set(p5b_lines) & {ghdl_line, ghdl_line + 1}


def test_VT04_responses_for_reviewers(client, monkeypatch):
    """UR-P5-01/02: collects 10 replies (5 success, 5 failure) for the two
    reviewers, and checks the structure alone tells them apart."""
    replies = []
    fake_run_job(monkeypatch)
    for _ in range(3):
        replies.append(("success", client.post("/check-and-simulate",
                                               json={"files": ref_files(), "top": TOP}).json()))
    replies.append(("success", client.post("/simulate", json=session_request(client)).json()))
    replies.append(("success", client.get("/language-service/health").json()))
    replies.append(("failure", client.post("/check-and-simulate",
                                           json={"files": BROKEN_AND_GATE, "top": "and_gate"}).json()))
    replies.append(("failure", client.post("/simulate", json={"top": "x"}).json()))
    replies.append(("failure", client.post("/simulate", json={
        "files": [{"name": "empty.vhd", "content": ""}], "top": "x"}).json()))
    monkeypatch.setattr(docker_runner, "run_job", lambda files, top, stop_time=None: {
        "job_id": "j1", "status": "failed", "stage": "analysis", "error": "internal_error",
        "message": "Docker error: daemon not running", "stages": [], "vcd": None})
    replies.append(("failure", client.post("/simulate", json={"files": ref_files(), "top": TOP}).json()))
    parent_main.p5b_router.get_manager()._factory().fail_start = True
    replies.append(("failure", client.post("/check-and-simulate",
                                           json={"files": ref_files(), "top": TOP}).json()))
    save("VT-04_responses_for_reviewers", [{"n": i + 1, "reply": r} for i, (_, r) in
                                           enumerate(replies)])
    save("VT-04_answer_key", [{"n": i + 1, "expected": k} for i, (k, _) in enumerate(replies)])
    for kind, r in replies:
        if kind == "success":
            assert r.get("status") in ("success", "ok")
        else:
            assert r["status"] == "failed" and r["stage"] and r["error"] and r["message"]


def test_VT05_joint_contract_conformance(client, monkeypatch):
    """Every /check-and-simulate reply with diagnostics passes the combined rule."""
    fake_run_job(monkeypatch)
    clean = client.post("/check-and-simulate", json={"files": ref_files(), "top": TOP}).json()
    broken = client.post("/check-and-simulate",
                         json={"files": BROKEN_AND_GATE, "top": "and_gate"}).json()
    problems = []
    for res, n in ((clean, 2), (broken, 1)):
        try:
            check_combined_result(res, n)
            assert set(RESULT_KEYS) <= set(res)
        except Exception as exc:          # noqa: BLE001
            problems.append(f"{type(exc).__name__}: {exc}")
    save("VT-05_joint", {"replies_checked": 2, "violations": problems})
    assert problems == []
