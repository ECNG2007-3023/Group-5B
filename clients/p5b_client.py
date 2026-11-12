"""Python client for the P5B Language-Service Gateway.

Talks to the Gateway the way an editor would: JSON-RPC 2.0 messages over one
WebSocket. Used by tools/check_gateway.py and the tests. Needs `websockets`.

    from clients.p5b_client import LanguageServiceClient

    with LanguageServiceClient("ws://127.0.0.1:8000/ws/language-service") as ls:
        ls.initialize()
        ls.open_file("design.vhd", open("design.vhd").read())
        print(ls.wait_for_diagnostics())        # [] when there are no problems
        print(ls.completion("design.vhd", 14, 11))
        ls.shutdown()
"""
from __future__ import annotations

import json
import os
import time

from websockets.sync.client import connect

DEFAULT_URL = os.environ.get("P5B_URL", "ws://127.0.0.1:8000/ws/language-service")


def file_uri(name):
    return f"file:///{name}"


class LanguageServiceClient:
    def __init__(self, url=None, timeout=15.0):
        self.url = url or DEFAULT_URL
        self.timeout = timeout
        self._ws = None
        self._next_id = 1
        self._queue = []  # messages received but not used yet

    def __enter__(self):
        self._ws = connect(self.url, open_timeout=self.timeout)
        self._ws.__enter__()   # newer websockets versions expect the connection to be entered
        return self

    def __exit__(self, *exc):
        if self._ws is not None:
            self._ws.__exit__(*exc)
            self._ws = None

    # ------------------------------------------------------------------ sending

    def send_raw(self, text):
        self._ws.send(text)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg))

    def request(self, method, params=None):
        """Send a request and wait for the reply with the same id."""
        request_id = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg))
        return self.wait_for(lambda m: m.get("id") == request_id and ("result" in m or "error" in m))

    # ---------------------------------------------------------------- receiving

    def wait_for(self, matches, timeout=None):
        """The first message (already received or new) for which matches(message) is True."""
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            for i, message in enumerate(self._queue):
                if matches(message):
                    return self._queue.pop(i)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no matching message in time; unmatched: {self._queue!r}")
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError:
                raise TimeoutError(f"no matching message in time; unmatched: {self._queue!r}") from None
            self._queue.append(json.loads(raw))

    def wait_for_publish(self, name=None):
        """The next publishDiagnostics message (for one file if name is given)."""
        def matches(m):
            if m.get("method") != "textDocument/publishDiagnostics":
                return False
            return name is None or m["params"]["uri"] == file_uri(name)
        return self.wait_for(matches)

    def wait_for_diagnostics(self, name=None):
        return self.wait_for_publish(name)["params"]["diagnostics"]

    def wait_for_error(self):
        """An error that does not belong to a request (its id is null)."""
        return self.wait_for(lambda m: "error" in m and m.get("id") is None)

    def clear(self):
        self._queue.clear()

    # ------------------------------------------------------ the approved subset

    def initialize(self):
        response = self.request("initialize", {"processId": None, "rootUri": None,
                                               "capabilities": {}})
        if "error" in response:
            raise RuntimeError(f"the Gateway refused initialize: {response['error']!r}")
        self.notify("initialized", {})
        return response

    def open_file(self, name, text, version=1):
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": file_uri(name), "languageId": "vhdl", "version": version, "text": text}})

    def change_file(self, name, text, version):
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": file_uri(name), "version": version},
            "contentChanges": [{"text": text}]})

    def close_file(self, name):
        self.notify("textDocument/didClose", {"textDocument": {"uri": file_uri(name)}})

    def completion(self, name, line, character):
        return self.request("textDocument/completion", {
            "textDocument": {"uri": file_uri(name)},
            "position": {"line": line, "character": character}})

    def shutdown(self):
        self.request("shutdown")
        self.notify("exit")
