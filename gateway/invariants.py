"""Invariants: rules that must ALWAYS be true while the Gateway runs.

The checks run inside the Gateway itself (not only in the tests), at the
points where each rule must hold:

    every message sent to a client        check_outgoing()           connection.py
    every diagnostics reply               check_publish_diagnostics() mappers.py
    every completion reply                check_completion()          mappers.py
    number of open sessions               check_session_limit()       session.py
    before a session folder is deleted    check_inside()              session_store.py
    /check-and-simulate                   check_combined_result()     parent_main.py

A broken rule raises InvariantError. That is a bug in the Gateway, never the
client's fault, so the client gets JSON-RPC "Internal error" (-32603) or
HTTP 500 instead of a wrong answer, and the details go to the server log.

A real exception is used instead of Python's `assert`, because `assert`
statements are removed when Python runs with -O, and these checks must stay.
"""
from __future__ import annotations

from pathlib import Path


class InvariantError(RuntimeError):
    """A rule that must always hold was broken: a bug in the Gateway."""


def require(condition, rule):
    """Raise InvariantError naming the rule if the condition is false."""
    if not condition:
        raise InvariantError(rule)


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# ------------------------------------------------------------ JSON-RPC messages

def check_outgoing(msg):
    """Every message to the client is valid JSON-RPC 2.0 and follows the contract:
    a reply has exactly one of result/error; an error has an integer code and
    data with status "failed", stage "language_service", a category and detail."""
    require(isinstance(msg, dict), "message is a JSON object")
    require(msg.get("jsonrpc") == "2.0", 'message has jsonrpc "2.0"')
    if "method" in msg:                                   # notification
        require("id" not in msg, "a notification has no id")
        require(isinstance(msg["method"], str) and msg["method"],
                "a notification names its method")
        if msg["method"] == "textDocument/publishDiagnostics":
            check_publish_diagnostics(msg.get("params"))
        return
    require("id" in msg, "a reply has an id (null allowed)")
    require(("result" in msg) != ("error" in msg),
            "a reply has exactly one of result and error")
    if "error" in msg:
        err = msg["error"]
        require(isinstance(err, dict), "error is an object")
        require(isinstance(err.get("code"), int) and not isinstance(err["code"], bool),
                "error code is an integer")
        require(isinstance(err.get("message"), str) and err["message"],
                "error has a message")
        data = err.get("data")
        require(isinstance(data, dict), "error has data")
        require(data.get("status") == "failed", 'error data status is "failed"')
        require(data.get("stage") == "language_service",
                'error data stage is "language_service"')
        require(isinstance(data.get("category"), str) and data["category"],
                "error data names a category")
        require(isinstance(data.get("detail"), str) and data["detail"],
                "error data gives a detail")


# ------------------------------------------------------------ reply contents

def _check_position(p):
    require(isinstance(p, dict) and _is_count(p.get("line"))
            and _is_count(p.get("character")),
            "a position has whole-number line and character, not negative")


def check_publish_diagnostics(params, check_uri=True):
    """A diagnostics reply: allowed file URI, well-formed items with severity
    1-4, sorted (so the same file always gives the same reply), and a summary
    that matches the items (no errors <=> "No errors present...").

    check_uri=False is for the mapper, which is also used internally with a
    blank URI; the URI is always checked when the message is sent."""
    from gateway.mappers import SEVERITY_WORDS, _sort_key, summarize, uri_to_name

    require(isinstance(params, dict), "diagnostics params is an object")
    if check_uri:
        require(uri_to_name(params.get("uri")) is not None,
                "diagnostics are for a file in the prescribed file set")
    items = params.get("diagnostics")
    require(isinstance(items, list), "diagnostics is a list")
    for d in items:
        require(isinstance(d, dict), "a diagnostic is an object")
        require(isinstance(d.get("range"), dict), "a diagnostic has a range")
        _check_position(d["range"].get("start"))
        _check_position(d["range"].get("end"))
        require(d.get("severity") in SEVERITY_WORDS, "severity is 1, 2, 3 or 4")
        for key in ("code", "source", "message"):
            require(isinstance(d.get(key), str), f"diagnostic {key} is text")
    require(items == sorted(items, key=_sort_key), "diagnostics are sorted")
    if "summary" in params:
        require(params["summary"] == summarize(items), "summary matches the diagnostics")
        has_error = any(d["severity"] == 1 for d in items)
        require(has_error != params["summary"].startswith("No errors present"),
                '"No errors present" exactly when there is no error')


def check_completion(result):
    """A completion reply: unique, sorted items, each with a text label."""
    require(isinstance(result, dict), "completion result is an object")
    require(isinstance(result.get("isIncomplete"), bool), "isIncomplete is true/false")
    items = result.get("items")
    require(isinstance(items, list), "completion items is a list")
    keys = []
    for item in items:
        require(isinstance(item, dict) and isinstance(item.get("label"), str)
                and item["label"], "a completion item has a label")
        require(isinstance(item.get("insertText"), str), "a completion item has insertText")
        keys.append((item["label"], item.get("kind") or 0, item["insertText"]))
    require(len(keys) == len(set(keys)), "completion items are not repeated")
    require(keys == sorted(keys), "completion items are sorted")


# ------------------------------------------------------------ class invariants

def check_session(session, max_files):
    """Class invariant of Session, true after creation and after every
    operation on it: files sent back as published are open, open files have a
    version, every stored name is in the prescribed file set, and the session
    never holds more files than allowed."""
    from gateway.session_store import is_allowed_filename
    require(set(session.published) <= session.opened,
            "class invariant: only open files have published diagnostics")
    require(session.opened <= set(session.versions),
            "class invariant: every open file has a version")
    require(all(is_allowed_filename(n) for n in session.versions),
            "class invariant: every stored name is in the prescribed file set")
    require(len(session.versions) <= max_files,
            f"class invariant: a session holds at most {max_files} files")


def check_manager(manager):
    """Class invariant of SessionManager: each active session is stored under
    its own id, and active + starting sessions never exceed the limit."""
    require(all(sid == s.id for sid, s in manager._sessions.items()),
            "class invariant: each session is stored under its own id")
    require(manager._starting >= 0, "class invariant: starting count is not negative")
    check_session_limit(len(manager._sessions), manager.cfg.max_sessions)


# ------------------------------------------------------------ state machine

# The connection's lifecycle as a state machine. Every change of state must be
# one of these transitions (see the state-transition tests).
STATES = {"new", "ready", "failed", "shutdown"}
TRANSITIONS = {
    ("new", "ready"),          # initialize
    ("ready", "failed"),       # VHDL-LS died or hung
    ("ready", "shutdown"),     # shutdown / exit / disconnect
    ("failed", "ready"),       # initialize again: a fresh session
    ("failed", "shutdown"),    # shutdown / exit after a failure
}


def check_transition(old, new):
    require(old in STATES and new in STATES, "state is one of new, ready, failed, shutdown")
    require(old == new or (old, new) in TRANSITIONS,
            f"state machine: {old} -> {new} is an allowed transition")


def check_connection(conn):
    """State invariant of a connection: it has a session exactly when ready."""
    require(conn.state in STATES, "state is one of new, ready, failed, shutdown")
    require((conn.state == "ready") == (conn.session is not None),
            "a connection has a session exactly when it is ready")


# ------------------------------------------------------------ loop invariant

def check_dedupe_invariant(out, seen):
    """Loop invariant of mappers.to_completion(): `seen` holds exactly the keys
    of the items kept so far, so no item has been kept twice."""
    require(len(out) == len(seen), "loop invariant: no completion item kept twice")
    require({(o["label"], o["kind"], o["insertText"]) for o in out} == seen,
            "loop invariant: seen holds exactly the kept items")


# ------------------------------------------------------------ sessions

def check_session_limit(active, limit):
    require(0 <= active <= limit, f"at most {limit} sessions are active")


def check_inside(path, root):
    """Only a folder directly inside the sessions root may be deleted, so a bug
    elsewhere can never make the Gateway delete anything else on the server."""
    path, root = Path(path).resolve(), Path(root).resolve()
    require(path.parent == root and path != root,
            "a deleted session folder is directly inside the sessions folder")


# ------------------------------------------------------------ combined backend

def check_combined_result(result, n_files):
    """/check-and-simulate: one diagnostics entry per file, and a GHDL job only
    ever ran if VHDL-LS found no error (FR-P5-01)."""
    diags = result.get("diagnostics")
    require(isinstance(diags, list) and len(diags) == n_files,
            "one diagnostics entry per submitted file")
    errors = sum(1 for c in diags for d in c["diagnostics"] if d["severity"] == 1)
    if errors:
        require(result.get("job_id") is None and result.get("stages") == [],
                "no GHDL job runs when VHDL-LS found an error")
        require(result.get("status") == "failed", "VHDL-LS errors mean status failed")
