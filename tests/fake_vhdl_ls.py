"""A tiny fake language server for tests/test_bridge.py. It is NOT vhdl_ls.

It speaks just enough LSP over stdin/stdout to test gateway/vhdl_ls_bridge.py.
Like vhdl_ls, it only sends diagnostics when there is something to report or
something to clear. Toy rule: a line like "  y <= a and b" with no ';' is an error.

Environment variables used by the tests:
    FAKE_LS_MODE=hang          never answer completion requests
    FAKE_LS_MODE=die           exit as soon as a completion request arrives
    FAKE_LS_MODE=error         answer completion requests with an error
    FAKE_LS_URI_STYLE=encoded  send diagnostics under a differently spelled URI
                               (first letter of the file name percent-encoded)
"""
import json
import os
import re
import sys

MODE = os.environ.get("FAKE_LS_MODE", "")
URI_STYLE = os.environ.get("FAKE_LS_URI_STYLE", "")
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
texts = {}      # uri -> text
had_diag = {}   # uri -> True if the last list we sent was not empty


def send(obj):
    body = json.dumps(obj).encode("utf-8")
    stdout.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    stdout.flush()


def read():
    length = None
    while True:
        line = stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    body = b""
    while len(body) < length:
        chunk = stdin.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body)


def diagnose(uri):
    out = []
    for n, line in enumerate(texts.get(uri, "").splitlines()):
        s = line.rstrip()
        if re.match(r"^\s*\w+\s*<=\s*[^;]*$", s):
            out.append({"range": {"start": {"line": n, "character": len(s)},
                                  "end": {"line": n, "character": len(s) + 1}},
                        "severity": 1, "message": "fake: expected ';'"})
    return out


def respell(uri):
    """Same file, written differently: the first letter of the name percent-encoded."""
    if URI_STYLE != "encoded":
        return uri
    head, _, name = uri.rpartition("/")
    return head + "/%" + format(ord(name[0]), "02x") + name[1:]


def push(uri):
    diags = diagnose(uri)
    if diags or had_diag.get(uri):
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": respell(uri), "diagnostics": diags}})
    had_diag[uri] = bool(diags)


while True:
    msg = read()
    if msg is None:
        break
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}}})
    elif method == "textDocument/didOpen":
        d = params["textDocument"]
        texts[d["uri"]] = d["text"]
        push(d["uri"])
    elif method == "textDocument/didChange":
        uri = params["textDocument"]["uri"]
        texts[uri] = params["contentChanges"][-1]["text"]
        push(uri)
    elif method == "textDocument/didClose":
        texts.pop(params["textDocument"]["uri"], None)
    elif method == "textDocument/completion":
        if MODE == "die":
            os._exit(3)
        if MODE == "hang":
            continue
        if MODE == "error":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "fake failure"}})
            continue
        text = texts.get(params["textDocument"]["uri"], "")
        names = sorted(set(re.findall(r"signal\s+(\w+)", text)))
        send({"jsonrpc": "2.0", "id": rid,
              "result": {"isIncomplete": False, "items": [{"label": n, "kind": 6} for n in names]}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": rid, "result": None})
    elif method == "exit":
        break
