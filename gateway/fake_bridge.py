"""A stand-in for vhdl_ls, used by the tests and with P5B_BRIDGE=fake.

It is NOT a VHDL analyser. It only knows two toy rules, so the Gateway's
messages, sessions and error handling can be tested without vhdl_ls:
  * a line like "y <= a and b" with no ';' at the end is an error
  * completion offers the declared signal names that start with the typed prefix
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from gateway.vhdl_ls_bridge import LanguageServerDied, LanguageServerError, LanguageServerTimeout


class FakeBridge:
    def __init__(self, session_dir):
        self.session_dir = Path(session_dir)
        self.is_alive = True
        self.closed = False
        self.texts = {}
        self.hang = False                # tests set this to make it stop answering
        self.refuse_completion = False   # tests set this to get an error answer to completion
        self.completion_style = "completion_list"   # "list", "completion_list" or "none"

    def kill(self):
        self.is_alive = False

    def _check(self):
        if not self.is_alive:
            raise LanguageServerDied("fake vhdl_ls terminated unexpectedly")

    async def open_file(self, path, text):
        self._check()
        self.texts[str(path)] = text

    async def close_file(self, path):
        self._check()
        self.texts.pop(str(path), None)

    def _diagnose(self, path):
        found = []
        for number, line in enumerate(self.texts.get(str(path), "").splitlines()):
            line = line.rstrip()
            if re.match(r"^\s*\w+\s*<=\s*[^;]*$", line):
                found.append({
                    "range": {"start": {"line": number, "character": len(line)},
                              "end": {"line": number, "character": len(line) + 1}},
                    "severity": 1, "code": "syntax", "source": "vhdl_ls",
                    "message": f"Expected ';' after assignment (in {path})"})
        return found

    async def get_diagnostics(self, path, timeout):
        self._check()
        if self.hang:
            await asyncio.sleep(timeout)
            raise LanguageServerTimeout("fake vhdl_ls did not answer")
        return self._diagnose(path)

    def latest_diagnostics(self, path):
        if str(path) not in self.texts:
            return None
        return self._diagnose(path)

    async def completion(self, path, line, col):
        self._check()
        if self.refuse_completion:
            raise LanguageServerError("textDocument/completion failed: {'code': -32603}")
        text = self.texts.get(str(path), "")
        lines = text.splitlines()
        prefix = ""
        if line < len(lines):
            match = re.search(r"(\w+)$", lines[line][:col])
            if match:
                prefix = match.group(1)
        names = re.findall(r"signal\s+(\w+)", text)
        items = [{"label": n, "kind": 6, "detail": "signal"}
                 for n in names if prefix and n.startswith(prefix)]
        if self.completion_style == "none":
            return None
        if self.completion_style == "list":
            return items
        return {"isIncomplete": False, "items": items}

    async def close(self):
        self.closed = True
        self.is_alive = False


class FakeBridgeFactory:
    """Called like start_bridge(session_dir). Keeps every bridge it made so tests can inspect them."""

    def __init__(self):
        self.instances = []
        self.fail_start = False

    async def __call__(self, session_dir):
        if self.fail_start:
            raise LanguageServerDied("fake vhdl_ls could not be started")
        bridge = FakeBridge(session_dir)
        self.instances.append(bridge)
        return bridge
