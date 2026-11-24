"""Turns what vhdl_ls sends into the shapes the Gateway's clients get.

    diagnostics: {uri, version, diagnostics: [{range, severity, code, source, message}], summary}
    completion:  {isIncomplete, items: [{label, kind, detail, insertText}]}

Lists are sorted so the same file always gives exactly the same reply.
Real folder paths are removed from all text sent to the client.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from urllib.parse import quote, unquote

from gateway import invariants
from gateway.session_store import is_allowed_filename

SEVERITY_BY_NAME = {"error": 1, "warning": 2, "information": 3, "info": 3, "hint": 4}
SEVERITY_WORDS = {1: ("error", "errors"), 2: ("warning", "warnings"),
                  3: ("information message", "information messages"), 4: ("hint", "hints")}


def no_change(text):
    return text


def uri_to_name(uri):
    """file:///design.vhd (or just design.vhd) -> "design.vhd". None if not allowed.

    Folders are never accepted, so a client cannot point outside its session.
    """
    if not isinstance(uri, str) or not uri or len(uri) > 1024:
        return None
    rest = uri
    if "://" in rest:
        scheme, _, rest = rest.partition("://")
        if scheme.lower() != "file" or not rest.startswith("/"):
            return None
        rest = rest[1:]
    rest = unquote(rest)
    if "/" in rest or "\\" in rest or "\x00" in rest:
        return None
    return rest if is_allowed_filename(rest) else None


def name_to_uri(name):
    return f"file:///{name}"


def safe_text(value, limit=80):
    """Short, printable copy of something the client sent, for error messages."""
    return "".join(c if " " <= c <= "~" else "?" for c in str(value))[:limit]


def _folder_uri(folder):
    """The file:// URI of a folder, written the way vhdl_ls writes it."""
    p = folder.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        return "file:///" + p[:2] + quote(p[2:])
    if p.startswith("/"):
        return "file://" + quote(p)
    return None


def make_scrubber(session_dir):
    """Function that removes the session's real folder from a piece of text.

    "/tmp/p5b/sessions/<id>/src/design.vhd" and its file:// form (also the
    Windows forms, with backslashes or file:///C:/...) become "design.vhd".
    """
    folder = str(session_dir).rstrip("/\\")
    prefixes = []
    for sep in dict.fromkeys(("/", "\\", os.sep)):
        prefixes += [f"file://{folder}{sep}src{sep}", f"{folder}{sep}src{sep}",
                     f"file://{folder}{sep}", f"{folder}{sep}"]
    uri = _folder_uri(folder)
    if uri:
        prefixes += [f"{uri}/src/", f"{uri}/"]
    prefixes.sort(key=len, reverse=True)  # longest first, so ".../src/" goes before "..."

    def scrub(text):
        for prefix in prefixes:
            text = text.replace(prefix, "")
        return text.replace(folder, "<session>")
    return scrub


def _number(value):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _position(p):
    if not isinstance(p, dict):
        p = {}
    return {"line": _number(p.get("line")), "character": _number(p.get("character"))}


def _severity(value):
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 4:
        return value
    if isinstance(value, str):
        return SEVERITY_BY_NAME.get(value.lower(), 1)
    return 1


def _diagnostic(d, scrub):
    rng = d.get("range") if isinstance(d.get("range"), dict) else {}
    code = d.get("code")
    if isinstance(code, dict):
        code = code.get("value")
    return {"range": {"start": _position(rng.get("start")), "end": _position(rng.get("end"))},
            "severity": _severity(d.get("severity")),
            "code": "" if code is None else str(code),
            "source": str(d.get("source") or "vhdl_ls"),
            "message": scrub(str(d.get("message", "")).strip())}


def _sort_key(d):
    r = d["range"]
    return (r["start"]["line"], r["start"]["character"], r["end"]["line"],
            r["end"]["character"], d["severity"], d["message"])


def without_bom(text):
    """Drop a UTF-8 "BOM" mark at the start of a file. Windows editors (e.g.
    Notepad) add it; it is not VHDL, and VHDL-LS reports it as an error
    ("Found invalid latin-1 character"). Found by error-guessing testing."""
    return text[1:] if isinstance(text, str) and text.startswith("\ufeff") else text


def summarize(diagnostics):
    """One sentence for the client, e.g. "No errors present." or "1 error found."."""
    # Programmer assumption: the mapper only ever gives severities 1-4.
    assert all(d["severity"] in SEVERITY_WORDS for d in diagnostics)
    if not diagnostics:
        return "No errors present."
    counts = Counter(d["severity"] for d in diagnostics)
    parts = []
    for severity, n in sorted(counts.items()):
        singular, plural = SEVERITY_WORDS[severity]
        parts.append(f"{n} {singular if n == 1 else plural}")
    if len(parts) == 1:
        text = parts[0]
    else:
        text = ", ".join(parts[:-1]) + " and " + parts[-1]
    if counts.get(1):
        return f"{text} found."
    return f"No errors present; {text} found."


def to_publish_diagnostics(uri, version, raw, scrub=no_change):
    # Programmer assumption: callers pass the document version as a whole number.
    assert isinstance(version, int) and not isinstance(version, bool), version
    items = [_diagnostic(d, scrub) for d in (raw or []) if isinstance(d, dict)]
    items.sort(key=_sort_key)
    reply = {"uri": uri, "version": version, "diagnostics": items, "summary": summarize(items)}
    invariants.check_publish_diagnostics(reply, check_uri=False)
    return reply


def to_completion(raw, scrub=no_change):
    """None, a plain list or a CompletionList all become {isIncomplete, items}."""
    incomplete = False
    items = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("items") or []
        incomplete = bool(raw.get("isIncomplete", False))

    # LOOP INVARIANT: `seen` holds exactly the keys of the items in `out`,
    # so no item is kept twice.
    out = []
    seen = set()
    invariants.check_dedupe_invariant(out, seen)                  # establishment
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            continue
        label = item["label"]
        edit = item.get("textEdit") if isinstance(item.get("textEdit"), dict) else {}
        insert = str(item.get("insertText") or edit.get("newText") or label)
        kind = item.get("kind")
        if not isinstance(kind, int) or isinstance(kind, bool):
            kind = None
        detail = scrub(str(item["detail"])) if item.get("detail") else ""
        if (label, kind, insert) in seen:
            continue
        seen.add((label, kind, insert))
        out.append({"label": label, "kind": kind, "detail": detail, "insertText": insert})
        invariants.require(len(out) == len(seen),                 # preservation
                           "loop invariant: no completion item kept twice")
    # EXIT: every item was looked at (a for loop over a finite list always
    # ends), so by the invariant `out` holds each distinct item exactly once.
    invariants.check_dedupe_invariant(out, seen)
    out.sort(key=lambda i: (i["label"], i["kind"] or 0, i["insertText"]))
    reply = {"isIncomplete": incomplete, "items": out}
    invariants.check_completion(reply)
    return reply
