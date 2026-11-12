"""Compare Gateway replies with the expected results in tests/vhdl/expected/.

Every check returns a list of problems; an empty list means it passed.
"""
from __future__ import annotations

ERROR = 1    # LSP severity numbers
WARNING = 2


def completion_items(result):
    """Accept a plain list or a {"items": [...]} completion result."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return result.get("items", [])


def check_diagnostics(actual, expected):
    mode = expected.get("expect")
    if mode == "none":
        if actual != []:
            return [f"expected no diagnostics, got {len(actual)}: {actual}"]
        return []
    if mode == "errors":
        errors = [d for d in actual if d.get("severity") == ERROR]
        wanted = expected.get("min_errors", 1)
        if len(errors) < wanted:
            return [f"expected at least {wanted} error(s), got {len(errors)}"]
        problems = []
        lines = expected.get("lines")
        if lines is not None:
            reported = [d.get("range", {}).get("start", {}).get("line") for d in errors]
            if not any(line in lines for line in reported):
                problems.append(f"expected an error on line(s) {lines}, got {reported}")
        needle = expected.get("message_contains")
        if needle and not any(needle.lower() in str(d.get("message", "")).lower() for d in errors):
            problems.append(f"no error message contains {needle!r}")
        return problems
    return [f"unknown expectation {mode!r}"]


def check_completion(result, expected):
    labels = {item.get("label") for item in completion_items(result)}
    missing = [label for label in expected.get("must_include", []) if label not in labels]
    if missing:
        return [f"completion is missing {missing}"]
    return []


def is_success(msg):
    return "result" in msg and "error" not in msg


def is_error(msg):
    return "error" in msg and "result" not in msg


def check_error(msg, code, method=None):
    """A JSON-RPC error with this code and a readable message (naming method if given)."""
    if not is_error(msg):
        return ["the reply is not an error"]
    error = msg["error"]
    problems = []
    if error.get("code") != code:
        problems.append(f"expected error code {code}, got {error.get('code')}")
    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        problems.append("the error message is empty")
    elif method is not None and method not in message:
        problems.append(f"the error message does not name {method!r}")
    return problems
