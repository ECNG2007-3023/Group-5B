"""Invariants, assertions and exceptions (gateway/invariants.py).

White-box tests: each check accepts what the Gateway really produces and
rejects each way the rule can be broken, then a broken rule is forced inside
the running Gateway to prove the client gets "Internal error" and the session
carries on. No vhdl_ls needed (fake bridge); the last test needs P5A.
"""
import random
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from conftest import DESIGN, fx, import_parent_main, initialize, open_doc, request
from gateway import invariants as inv
from gateway import mappers
from gateway.config import Config
from gateway.invariants import InvariantError
from gateway.jsonrpc import error_response, notification, result_response
from gateway.session import SessionManager
from gateway.session_store import SessionStore

ERROR = {"range": {"start": {"line": 3, "character": 2}, "end": {"line": 3, "character": 5}},
         "severity": 1, "code": "syntax_error", "source": "vhdl ls", "message": "Expected ';'"}
WARNING = {**ERROR, "severity": 2, "message": "unused signal"}


def diags(*items, uri="file:///a.vhd"):
    return mappers.to_publish_diagnostics(uri, 1, list(items))


def broken(fn, *args):
    """Run a check that must fail; return the rule it names."""
    with pytest.raises(InvariantError) as exc:
        fn(*args)
    return str(exc.value)


# ------------------------------------------------ what the Gateway produces passes

def test_real_messages_pass():
    inv.check_outgoing(result_response(1, {"capabilities": {}}))
    inv.check_outgoing(result_response(None, None))
    inv.check_outgoing(error_response(2, -32601, "Method not supported", "unsupported_method"))
    inv.check_outgoing(notification("textDocument/publishDiagnostics", diags(ERROR, WARNING)))
    inv.check_outgoing(notification("textDocument/publishDiagnostics", diags()))


def test_mapper_output_always_passes_for_any_input():
    """Property test: 500 random, partly nonsense VHDL-LS replies. Whatever comes
    in, the mapper's output must obey the reply rules."""
    rnd = random.Random(2007)
    junk = [None, -1, 3.5, "x", True, [], {}, 10**9, "error", "WARNING", "hint"]
    for _ in range(500):
        raw = []
        for _ in range(rnd.randint(0, 6)):
            pos = lambda: {"line": rnd.choice(junk + [0, 7]), "character": rnd.choice(junk + [1])}
            raw.append(rnd.choice([
                {"range": {"start": pos(), "end": pos()}, "severity": rnd.choice(junk + [1, 2]),
                 "code": rnd.choice(junk + [{"value": "c"}]), "message": rnd.choice(junk)},
                {"message": "no range"}, "not a dict", None]))
        reply = mappers.to_publish_diagnostics("file:///a.vhd", 1, raw)   # checks inside
        inv.check_publish_diagnostics(reply)
        items = [rnd.choice([{"label": "sig"}, {"label": rnd.choice(junk)}, "x",
                             {"label": "a", "kind": rnd.choice(junk)}, {"label": "a"}])
                 for _ in range(rnd.randint(0, 6))]
        inv.check_completion(mappers.to_completion(rnd.choice([items, {"items": items}, None])))


# ------------------------------------------------ each broken rule is caught

@pytest.mark.parametrize("msg, rule", [
    ("not a dict", "JSON object"),
    ({"id": 1, "result": None}, 'jsonrpc "2.0"'),
    ({"jsonrpc": "2.0", "result": None}, "has an id"),
    ({"jsonrpc": "2.0", "id": 1}, "exactly one of result and error"),
    ({"jsonrpc": "2.0", "id": 1, "result": 1, "error": {}}, "exactly one"),
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": "x", "message": "m"}}, "integer"),
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": 1, "message": "m"}}, "has data"),
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": 1, "message": "m", "data": {
        "status": "ok", "stage": "language_service", "category": "c", "detail": "d"}}},
     '"failed"'),
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": 1, "message": "m", "data": {
        "status": "failed", "stage": "simulation", "category": "c", "detail": "d"}}},
     '"language_service"'),
    ({"jsonrpc": "2.0", "id": 1, "method": "x"}, "no id"),
])
def test_bad_messages_are_caught(msg, rule):
    assert rule in broken(inv.check_outgoing, msg)


def test_bad_diagnostics_are_caught():
    good = diags(ERROR, WARNING)
    cases = [
        ({**good, "uri": "file:///../etc/passwd"}, "prescribed file set"),
        ({**good, "diagnostics": [WARNING, ERROR]}, "sorted"),
        ({**good, "summary": "No errors present."}, "summary matches"),
        ({**good, "diagnostics": [{**ERROR, "severity": 7}]}, "severity"),
        ({**good, "diagnostics": [{**ERROR, "range": {"start": {"line": -1, "character": 0},
                                                       "end": {"line": 0, "character": 0}}}]},
         "not negative"),
        ({**good, "diagnostics": [{**ERROR, "message": None}]}, "message is text"),
    ]
    for params, rule in cases:
        assert rule in broken(inv.check_publish_diagnostics, params)


def test_bad_completion_is_caught():
    a = {"label": "a", "kind": 6, "insertText": "a"}
    b = {"label": "b", "kind": 6, "insertText": "b"}
    assert "sorted" in broken(inv.check_completion, {"isIncomplete": False, "items": [b, a]})
    assert "repeated" in broken(inv.check_completion, {"isIncomplete": False, "items": [a, a]})
    assert "label" in broken(inv.check_completion, {"isIncomplete": False, "items": [{"label": ""}]})
    assert "true/false" in broken(inv.check_completion, {"isIncomplete": "no", "items": []})


def test_session_limit_rule():
    inv.check_session_limit(8, 8)
    assert "at most 8" in broken(inv.check_session_limit, 9, 8)


def test_only_session_folders_may_be_deleted(tmp_path):
    root = tmp_path / "sessions"
    inv.check_inside(root / "abc", root)
    for bad in (root, tmp_path / "other", root / "abc" / "deeper", root / ".." / "x"):
        assert "directly inside" in broken(inv.check_inside, bad, root)


def test_combined_rule_no_job_after_vhdl_ls_errors():
    with_error = [diags(ERROR)]
    ok = {"job_id": None, "status": "failed", "stages": [], "diagnostics": with_error}
    inv.check_combined_result(ok, 1)
    job_ran = {**ok, "job_id": "abc", "stages": [{"stage": "analysis"}]}
    assert "no GHDL job" in broken(inv.check_combined_result, job_ran, 1)
    assert "one diagnostics entry per" in broken(inv.check_combined_result, ok, 2)


# ------------------------------------------ broken rule inside the running Gateway

def test_broken_reply_becomes_internal_error_and_session_carries_on(env, monkeypatch):
    """Force a malformed reply (no result). The client must get Internal error
    with the same id, never the malformed message, and the next request works."""
    import gateway.connection as connection
    real = connection.result_response
    monkeypatch.setattr(connection, "result_response",
                        lambda id, result: {"jsonrpc": "2.0", "id": id})
    with env.ws() as ws:
        reply = request(ws, "initialize", {"capabilities": {}}, id=7)
        assert reply["id"] == 7 and reply["error"]["code"] == -32603
        assert reply["error"]["data"]["category"] == "internal_error"
        monkeypatch.setattr(connection, "result_response", real)
        assert "result" in request(ws, "shutdown", None, id=8)
    assert env.wait_clean()


def test_broken_diagnostics_never_reach_the_client(env, monkeypatch):
    """Force the summary to be wrong: the reply rule fails inside the mapper,
    the client gets Internal error, and a later correct request still works."""
    real = mappers.summarize
    monkeypatch.setattr(mappers, "summarize", lambda items: "No errors present.")
    with env.ws() as ws:
        initialize(ws)
        msg = open_doc(ws, DESIGN, fx("fx02_missing_semicolon.vhd"))
        assert msg["error"]["code"] == -32603                 # not the wrong summary
        monkeypatch.setattr(mappers, "summarize", real)
        msg = open_doc(ws, "other.vhd", fx("fx01_valid.vhd"))
        assert msg["method"] == "textDocument/publishDiagnostics"
    assert env.wait_clean()


def test_delete_refuses_a_folder_outside_the_sessions_root(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "root")
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("x")
    sid = store.create_session()
    monkeypatch.setattr(store, "session_dir", lambda _sid: precious)   # simulate a bug
    with pytest.raises(InvariantError):
        store.delete_session(sid)
    assert (precious / "keep.txt").exists()                           # nothing deleted


def test_combined_backend_returns_500_when_its_rule_breaks(tmp_path, monkeypatch):
    parent_main = import_parent_main()
    from app import docker_runner
    monkeypatch.setattr(docker_runner, "cleanup_stale_jobs", lambda: (0, 0))
    monkeypatch.setattr(parent_main.p5b_router, "manager", SessionManager(
        replace(Config(), bridge="fake", session_root=str(tmp_path), watchdog_interval_s=60.0)))

    async def job_despite_errors(request):     # simulate a bug: GHDL ran anyway
        return {"job_id": "abc", "status": "failed", "stage": "analysis", "error": "x",
                "message": "", "stages": [{"stage": "analysis"}], "vcd": None,
                "diagnostics": [diags(ERROR)]}
    monkeypatch.setattr(parent_main, "_check_and_simulate", job_despite_errors)
    with TestClient(parent_main.app) as c:
        r = c.post("/check-and-simulate", json={
            "files": [{"name": "a.vhd", "content": "entity a is end;"}], "top": "a"})
    assert r.status_code == 500 and r.json()["error"] == "internal_error"


# ============================================ class invariants (Session, manager)

class FakeSession:
    def __init__(self, versions=(), opened=(), published=()):
        self.versions = {n: 1 for n in versions}
        self.opened = set(opened)
        self.published = {n: [] for n in published}


def test_session_class_invariant():
    inv.check_session(FakeSession(["a.vhd", "b.vhd"], ["a.vhd"], ["a.vhd"]), 20)
    cases = [
        (FakeSession(["a.vhd"], ["a.vhd"], ["b.vhd"]), "only open files"),
        (FakeSession(["a.vhd"], ["b.vhd"]), "every open file has a version"),
        (FakeSession(["../x.vhd"]), "prescribed file set"),
        (FakeSession([f"f{i}.vhd" for i in range(21)]), "at most 20 files"),
    ]
    for session, rule in cases:
        with pytest.raises(InvariantError, match=rule):
            inv.check_session(session, 20)


def test_session_class_invariant_holds_through_a_real_session(env):
    """Establishment (new session) and preservation (after open, change,
    close), checked on the real Session object as a client uses it."""
    with env.ws() as ws:
        initialize(ws)
        (session,) = env.manager._sessions.values()
        session.check_invariant(env.cfg.max_files)                   # establishment
        open_doc(ws, "a.vhd", fx("fx01_valid.vhd"))
        open_doc(ws, "b.vhd", fx("fx01_valid.vhd").replace("and_gate", "or_gate"))
        session.check_invariant(env.cfg.max_files)                   # preservation
        request(ws, "textDocument/completion", {"textDocument": {"uri": "file:///a.vhd"},
                                                "position": {"line": 0, "character": 0}}, 3)
        session.check_invariant(env.cfg.max_files)
    assert env.wait_clean()


def test_broken_session_invariant_is_caught_in_the_gateway(env, monkeypatch):
    """Force a bug: a file is marked published without being open. The next
    operation's preservation check stops it with Internal error."""
    with env.ws() as ws:
        initialize(ws)
        (session,) = env.manager._sessions.values()
        session.published["ghost.vhd"] = []
        msg = open_doc(ws, "a.vhd", fx("fx01_valid.vhd"))
        if "method" in msg:                  # diagnostics go out first,
            msg = ws.receive_json()          # then the end-of-operation check fails
        assert msg["error"]["code"] == -32603


def test_manager_class_invariant(env):
    inv.check_manager(env.manager)
    env.manager._starting = -1
    with pytest.raises(InvariantError, match="not negative"):
        inv.check_manager(env.manager)


# ============================================ state machine of a connection

def test_every_allowed_transition_passes_and_others_are_caught():
    for old, new in inv.TRANSITIONS:
        inv.check_transition(old, new)
    for old, new in [("new", "failed"), ("new", "shutdown"), ("shutdown", "ready"),
                     ("shutdown", "failed"), ("ready", "new"), ("ready", "sleeping")]:
        with pytest.raises(InvariantError):
            inv.check_transition(old, new)


def test_illegal_transition_is_refused_by_the_connection(env):
    from gateway.connection import Connection
    conn = Connection(env.manager, send_json=None)
    conn.state = "shutdown"
    with pytest.raises(InvariantError, match="allowed transition"):
        conn._set_state("ready")


def test_connection_state_invariant():
    class C:
        state, session = "ready", None
    with pytest.raises(InvariantError, match="exactly when it is ready"):
        inv.check_connection(C())


# ============================================ loop invariant (completion de-dupe)

def test_dedupe_loop_invariant_trace(monkeypatch):
    """Traced like the lecture's maximum example. Items: a, a (repeat), b.

        iteration | item | kept so far | seen      | invariant
        start     |  -   | []          | {}        | true
        1         |  a   | [a]         | {a}       | true
        2         |  a   | [a]         | {a}       | true (repeat skipped)
        3         |  b   | [a, b]      | {a, b}    | true
        exit      |      | each distinct item exactly once
    """
    snapshots = []
    real = inv.check_dedupe_invariant

    def record(out, seen):
        snapshots.append(sorted(o["label"] for o in out))
        real(out, seen)
    monkeypatch.setattr(inv, "check_dedupe_invariant", record)
    result = mappers.to_completion([{"label": "a"}, {"label": "a"}, {"label": "b"}])
    assert [i["label"] for i in result["items"]] == ["a", "b"]
    assert snapshots == [[], ["a", "b"]]          # establishment and exit


def test_dedupe_loop_invariant_catches_a_repeat():
    item = {"label": "a", "kind": None, "insertText": "a"}
    with pytest.raises(InvariantError, match="kept twice"):
        inv.check_dedupe_invariant([item, item], {("a", None, "a")})


# ============================================ assertions (assert)

def test_assertions_guard_programmer_assumptions():
    """assert statements check the programmer's assumptions; on in normal
    runs and tests, off with python -O (so must-hold rules use InvariantError)."""
    with pytest.raises(AssertionError):
        mappers.summarize([{"severity": 9}])        # mapper only gives 1-4
