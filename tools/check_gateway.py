"""Check a running Gateway with the system-test cases, twice.

Uses only the WebSocket (clients/p5b_client.py) and the files in tests/vhdl,
so P5A and Project 6 do not need to be running.

1. Start the Gateway (repo root, venv):  uvicorn gateway.main:app --port 8000
   (or both services:                   uvicorn parent_main:app --port 8000)
2. In a second terminal:                python tools/check_gateway.py
   (another address:                    python tools/check_gateway.py ws://host:8000/ws/language-service)

Prints PASS/FAIL for every case in both runs, says whether the two runs gave
identical replies, and exits with code 1 if anything failed.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clients import checks  # noqa: E402
from clients.p5b_client import DEFAULT_URL, LanguageServiceClient, file_uri  # noqa: E402

FIXTURES = ROOT / "tests" / "vhdl"
DESIGN = "design.vhd"   # every fixture is sent under this name

UNSUPPORTED = [
    ("textDocument/hover", {"position": {"line": 0, "character": 0}}),
    ("textDocument/definition", {"position": {"line": 0, "character": 0}}),
    ("textDocument/rename", {"position": {"line": 0, "character": 0}, "newName": "x"}),
    ("textDocument/formatting", {"options": {"tabSize": 2, "insertSpaces": True}}),
]


def text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def expected(stem):
    return json.loads((FIXTURES / "expected" / f"{stem}.expected.json").read_text(encoding="utf-8"))


def open_and_wait(ls, fixture):
    ls.open_file(DESIGN, text(fixture))
    return ls.wait_for_publish(DESIGN)["params"]


# Each case gets a fresh session and returns (problems, replies). The replies
# are compared between the two runs.

def case_valid(ls):
    p = open_and_wait(ls, "fx01_valid.vhd")
    problems = checks.check_diagnostics(p["diagnostics"], expected("fx01_valid"))
    if p.get("summary") != "No errors present.":
        problems.append(f"summary is {p.get('summary')!r}")
    return problems, [p]


def case_missing_semicolon(ls):
    p = open_and_wait(ls, "fx02_missing_semicolon.vhd")
    return checks.check_diagnostics(p["diagnostics"], expected("fx02_missing_semicolon")), [p]


def case_change_clears(ls):
    first = open_and_wait(ls, "fx02_missing_semicolon.vhd")
    problems = checks.check_diagnostics(first["diagnostics"], expected("fx02_missing_semicolon"))
    ls.change_file(DESIGN, text("fx02b_corrected.vhd"), version=2)
    second = ls.wait_for_publish(DESIGN)["params"]
    problems += checks.check_diagnostics(second["diagnostics"], expected("fx02b_corrected"))
    if second.get("summary") != "No errors present.":
        problems.append(f"summary is {second.get('summary')!r}")
    return problems, [first, second]


def case_completion(ls):
    exp = expected("fxc1_completion")
    open_and_wait(ls, "fxc1_completion.vhd")
    reply = ls.completion(DESIGN, exp["position"]["line"], exp["position"]["character"])
    if not checks.is_success(reply):
        return [f"completion failed: {reply.get('error')}"], [reply]
    return checks.check_completion(reply["result"], exp), [reply]


def case_file_set(ls):
    p = open_and_wait(ls, "fx01_valid.vhd")
    problems = checks.check_diagnostics(p["diagnostics"], expected("fx01_valid"))
    ls.open_file("bad name.txt", text("fx01_valid.vhd"))
    error = ls.wait_for_error()
    problems += checks.check_error(error, -32602)
    return problems, [p, error]


def case_unsupported(ls):
    open_and_wait(ls, "fx01_valid.vhd")
    problems, replies = [], []
    for method, extra in UNSUPPORTED:
        reply = ls.request(method, {"textDocument": {"uri": file_uri(DESIGN)}, **extra})
        problems += [f"{method}: {p}" for p in checks.check_error(reply, -32601, method)]
        replies.append(reply)
    return problems, replies


def case_malformed_then_valid(ls):
    ls.send_raw("{ this is not valid json")
    error = ls.wait_for_error()
    problems = checks.check_error(error, -32700)
    p = open_and_wait(ls, "fx01_valid.vhd")
    problems += checks.check_diagnostics(p["diagnostics"], expected("fx01_valid"))
    return problems, [error, p]


def case_success_vs_failure(ls):
    exp = expected("fxc1_completion")
    open_and_wait(ls, "fxc1_completion.vhd")
    ok = ls.completion(DESIGN, exp["position"]["line"], exp["position"]["character"])
    bad = ls.request("textDocument/hover", {"textDocument": {"uri": file_uri(DESIGN)},
                                            "position": {"line": 0, "character": 0}})
    problems = []
    if not (checks.is_success(ok) and not checks.is_error(ok)):
        problems.append("the completion reply does not look like a success")
    if not (checks.is_error(bad) and not checks.is_success(bad)):
        problems.append("the hover reply does not look like an error")
    return problems, [ok, bad]


def case_error_information(ls):
    reply = ls.request("textDocument/hover", {"textDocument": {"uri": file_uri(DESIGN)},
                                              "position": {"line": 0, "character": 0}})
    problems = checks.check_error(reply, -32601, "textDocument/hover")
    data = reply.get("error", {}).get("data", {})
    if data.get("status") != "failed" or data.get("stage") != "language_service":
        problems.append(f"status/stage are {data.get('status')!r}/{data.get('stage')!r}")
    if data.get("category") != "unsupported_method" or not data.get("detail"):
        problems.append("the error has no category or detail")
    return problems, [reply]


CASES = [
    ("FR-P5B-01  valid design: empty list, 'No errors present.'", case_valid),
    ("FR-P5B-01  missing semicolon: error on the assignment line", case_missing_semicolon),
    ("FR-P5B-01  change with the fix: diagnostics cleared", case_change_clears),
    ("FR-P5B-02  completion after sig_: sig_clock and sig_reset", case_completion),
    ("FR-P5B-03  prescribed file set: valid file used, bad name rejected", case_file_set),
    ("FR-P5B-04  hover, definition, rename, formatting rejected", case_unsupported),
    ("FR-P5B-04  invalid message rejected, next request still works", case_malformed_then_valid),
    ("UR-P5-01   success and failure replies can be told apart", case_success_vs_failure),
    ("UR-P5-02   an error names the stage and the cause", case_error_information),
]


def run_once(url):
    """Run every case. Returns a list of (name, problems, replies)."""
    results = []
    for name, case in CASES:
        try:
            with LanguageServiceClient(url) as ls:
                ls.initialize()
                problems, replies = case(ls)
                ls.shutdown()
        except Exception as exc:  # could not connect, timed out, ...
            problems, replies = [f"{type(exc).__name__}: {exc}"], []
        results.append((name, problems, replies))
    return results


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"Gateway: {url}\n")
    runs = []
    for number in (1, 2):
        print(f"Run {number}")
        results = run_once(url)
        for name, problems, _ in results:
            print(f"  {'PASS' if not problems else 'FAIL'}  {name}"
                  + ("" if not problems else "  <- " + "; ".join(problems)))
        runs.append(results)
        print()

    passed = [sum(not problems for _, problems, _ in r) for r in runs]
    same = [replies for _, _, replies in runs[0]] == [replies for _, _, replies in runs[1]]
    print(f"Run 1: {passed[0]}/{len(CASES)} passed. Run 2: {passed[1]}/{len(CASES)} passed.")
    print(f"FR-P5B-06  both runs gave identical replies: {'YES' if same else 'NO'}")
    sys.exit(0 if same and passed == [len(CASES), len(CASES)] else 1)


if __name__ == "__main__":
    main()
